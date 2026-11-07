"""Strictly-serial benchmark run queue (guardrails #5, #9, #10).

Launching is the *formal* shape: one ``python -m genai_bench.cli.cli
benchmark`` subprocess per run, reusing the existing CLI entry unchanged.
Only ONE experiment ever runs at a time -- multi-select / batch rerun / sweep
/ refilled runs all funnel into this one queue (two concurrent loads on the
same GPU make the latency/throughput curves unreadable).

The API key is injected ONLY into the child's whitelisted environment (see
:mod:`genai_bench.web.mapping.build_child_env`). It is never placed in argv,
never written to disk, never logged, and never returned. The queue holds it in
process memory only until the child is spawned, then drops it.

The worker is a single cooperative greenlet when gevent is active (the WebUI
process runs under ``monkey.patch_all()`` / ``gevent.pywsgi``); a plain daemon
thread is the fallback so the module also works in an unpatched test process.
A ``launcher`` callable can be injected for tests.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from genai_bench.web.store import Store

try:  # the WebUI process is gevent-monkey-patched
    import gevent

    _HAS_GEVENT = True
except ImportError:  # pragma: no cover - plain unit-test process
    gevent = None  # type: ignore[assignment]
    _HAS_GEVENT = False


def _spawn(fn: Callable[[], None]) -> Any:
    if _HAS_GEVENT:
        return gevent.spawn(fn)  # type: ignore[union-attr]
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()
    return thread


def _sleep(seconds: float) -> None:
    if _HAS_GEVENT:
        gevent.sleep(seconds)  # type: ignore[union-attr]
    else:  # pragma: no cover
        import time

        time.sleep(seconds)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_launcher(argv: list[str], env: dict[str, str], cwd: str) -> Any:
    """Spawn the real CLI as a headless subprocess with no captured output.

    stdout/stderr go to DEVNULL (the engine writes its own masked
    ``genai_bench.log`` into ``--log-dir``), so no secret can catch a ride.
    """
    return subprocess.Popen(
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@dataclass
class RunJob:
    run_id: str
    argv: list[str]
    title: str = ""
    group: str = ""
    status: str = "queued"  # queued | running | done | failed | stopped
    queued_at: str = field(default_factory=_now)
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    proc: Any = None
    env: dict[str, str] | None = None  # holds the secret until spawn, then dropped


class RunQueue:
    """One in-process FIFO queue; at most one child process runs at a time."""

    def __init__(
        self,
        store: Store,
        *,
        launcher: Callable[[list[str], dict[str, str], str], Any] | None = None,
        poll_interval: float = 0.4,
        cwd: str = ".",
    ):
        self.store = store
        self.launcher = launcher or _default_launcher
        self.poll_interval = poll_interval
        self.cwd = cwd
        self._lock = threading.RLock()
        self._jobs: dict[str, RunJob] = {}
        self._pending: list[str] = []
        self._current: str | None = None
        self._paused = False  # set by stop(): do NOT auto-advance the queue
        self._closed = False
        self._worker: Any = None
        self._started = False
        self._last_error: str | None = None

    # -- public API ---------------------------------------------------------
    def enqueue(
        self,
        run_id: str,
        argv: list[str],
        env: dict[str, str],
        *,
        title: str = "",
        group: str = "",
    ) -> int:
        """Add one run to the serial queue; return its 1-based queue position."""
        with self._lock:
            job = RunJob(run_id=run_id, argv=argv, env=dict(env), title=title, group=group)
            self._jobs[run_id] = job
            self._pending.append(run_id)
            # A fresh explicit start resumes a queue that was paused by stop().
            self._paused = False
            self._ensure_worker_locked()
            return len(self._pending)

    def stop(self, run_id: str) -> dict[str, Any]:
        """Terminate the running child and halt the rest of the batch.

        Guardrail #9 / spec: "stop 立即终止且后续不自动开跑". Stopping cancels
        the whole serial batch (the queued siblings of a multi-select / sweep)
        so nothing else auto-starts until the user explicitly starts again.
        """
        with self._lock:
            job = self._jobs.get(run_id)
            if job is None:
                return {"ok": False, "message": f"未知或未入队的实验: {run_id}"}
            # Cancel every queued run in this batch.
            for pending_id in list(self._pending):
                self._pending.remove(pending_id)
                pending_job = self._jobs.get(pending_id)
                if pending_job and pending_job.status == "queued":
                    pending_job.status = "stopped"
                    pending_job.ended_at = _now()
                    self.store.set_status(pending_id, "stopped")
            self._paused = True
            if job.status == "running" and job.proc is not None:
                self._terminate(job.proc)
                job.proc = None
                job.status = "stopped"
                job.ended_at = _now()
                self.store.set_status(run_id, "stopped")
                if self._current == run_id:
                    self._current = None
            elif job.status == "queued":
                job.status = "stopped"
                job.ended_at = _now()
                self.store.set_status(run_id, "stopped")
            return {
                "ok": True,
                "run_id": run_id,
                "status": job.status,
                "paused": True,
                "remaining": len(self._pending),
            }

    def status(self) -> dict[str, Any]:
        """Snapshot for GET /api/benchmark/queue (队列可见)."""
        with self._lock:
            jobs = []
            for run_id, job in self._jobs.items():
                jobs.append(
                    {
                        "run_id": run_id,
                        "title": job.title,
                        "group": job.group,
                        "status": job.status,
                        "queued_at": job.queued_at,
                        "started_at": job.started_at,
                        "ended_at": job.ended_at,
                        "error": job.error,
                        "position": self._pending.index(run_id) + 1 if run_id in self._pending else None,
                    }
                )
            return {
                "running": self._current,
                "paused": self._paused,
                "queued": list(self._pending),
                "jobs": jobs,
            }

    def close(self) -> None:
        self._closed = True

    # -- internals ----------------------------------------------------------
    def _ensure_worker_locked(self) -> None:
        if not self._started:
            self._started = True
            self._worker = _spawn(self._loop)

    def _loop(self) -> None:  # pragma: no cover - exercised via integration
        while not self._closed:
            _sleep(self.poll_interval)
            try:
                self._tick()
            except Exception as exc:  # keep the worker alive on any single failure
                self._last_error = str(exc)

    def _tick(self) -> None:
        with self._lock:
            # 1) harvest a finished child
            if self._current is not None:
                job = self._jobs.get(self._current)
                if job is None:
                    self._current = None
                    return
                if job.proc is None or job.proc.poll() is None:
                    return  # still running
                code = job.proc.poll()
                job.proc = None
                job.status = "done" if code == 0 else "failed"
                job.ended_at = _now()
                if code not in (0, None):
                    job.error = f"child exit code {code}"
                self.store.set_status(job.run_id, job.status, error=job.error)
                self._current = None

            # 2) start the next queued run (unless stop() paused the batch)
            if self._paused or self._current is not None or not self._pending:
                return
            run_id = self._pending.pop(0)
            job = self._jobs.get(run_id)
            if job is None or job.status != "queued":
                return
            try:
                proc = self.launcher(job.argv, job.env or {}, self.cwd)
            except Exception as exc:
                job.status = "failed"
                job.ended_at = _now()
                job.error = f"launch failed: {exc}"
                self.store.set_status(run_id, "failed", error=job.error)
                return
            job.proc = proc
            job.env = None  # secret no longer needed in memory
            job.status = "running"
            job.started_at = _now()
            self.store.set_status(run_id, "running")
            self._current = run_id

    @staticmethod
    def _terminate(proc: Any) -> None:
        """Best-effort graceful then hard termination of the child."""
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
