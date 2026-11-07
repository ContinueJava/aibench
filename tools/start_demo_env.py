"""Start (or stop) the demo environment: 3 local model servers + the WebUI.

Detached processes so they survive this shell / the agent session. Logs go to
tools/logs/. Usage::

    python tools/start_demo_env.py          # start everything, wait for health
    python tools/start_demo_env.py --stop   # stop everything
    python tools/start_demo_env.py --status # just print status
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "tools" / "logs"
PIDFILE = LOGS / "pids.json"
PY = sys.executable

MODEL_SERVERS = [
    ("chat", 8101),
    ("embedding", 8102),
    ("rerank", 8103),
]
WEBUI_PORT = 8000

DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def _health(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(400).decode("utf-8", "replace")
            return resp.status == 200, body
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def _port_busy(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _spawn(name: str, argv: list[str], env: dict[str, str]) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = (LOGS / f"{name}.log").open("ab", buffering=0)
    proc = subprocess.Popen(
        argv,
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=DETACHED,
        close_fds=True,
    )
    return proc.pid


def start() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["ENABLE_UI"] = "false"

    pids: dict[str, int] = {}
    if PIDFILE.exists():
        try:
            pids.update(json.loads(PIDFILE.read_text("utf-8")))
        except (json.JSONDecodeError, OSError):
            pass

    for task, port in MODEL_SERVERS:
        ok, _ = _health(f"http://127.0.0.1:{port}/health")
        if ok:
            print(f"[skip] {task} already healthy on :{port}")
            continue
        if _port_busy(port):
            print(f"[warn] :{port} busy but not healthy ({task}) - not touching it")
            continue
        argv = [
            PY,
            str(ROOT / "tools" / "light_model_server.py"),
            "--task", task,
            "--port", str(port),
            "--model", str(ROOT / "tools" / "local_models" / task),
            "--tokenizer", str(ROOT / "tools" / "local_models" / task),
            "--threads", "6",
        ]
        pids[f"model_{task}"] = _spawn(f"model_{task}", argv, env)
        print(f"[start] {task} :{port} pid={pids[f'model_{task}']}")

    ok, _ = _health(f"http://127.0.0.1:{WEBUI_PORT}/api/health")
    if ok:
        print(f"[skip] webui already healthy on :{WEBUI_PORT}")
    elif _port_busy(WEBUI_PORT):
        print(f"[warn] :{WEBUI_PORT} busy but not healthy - not touching it")
    else:
        argv = [
            PY, "-m", "genai_bench.cli.cli", "webui",
            "--host", "127.0.0.1", "--port", str(WEBUI_PORT),
        ]
        pids["webui"] = _spawn("webui", argv, env)
        print(f"[start] webui :{WEBUI_PORT} pid={pids['webui']}")

    PIDFILE.write_text(json.dumps(pids, indent=2), "utf-8")

    print("\nwaiting for health ...")
    deadline = time.time() + 90
    targets = [(f"model_{task}", f"http://127.0.0.1:{port}/health")
               for task, port in MODEL_SERVERS]
    targets.append(("webui", f"http://127.0.0.1:{WEBUI_PORT}/api/health"))
    pending = [name for name, _ in targets]
    while pending and time.time() < deadline:
        for name, url in targets:
            if name in pending and _health(url)[0]:
                pending.remove(name)
                print(f"  [ok] {name}")
        if pending:
            time.sleep(1.0)

    if pending:
        print(f"\n[FAILED] still unhealthy: {pending} -- see tools/logs/*.log")
        return 1
    print("\nall services healthy")
    return 0


def stop() -> int:
    if not PIDFILE.exists():
        print("no pid file")
        return 0
    try:
        pids = json.loads(PIDFILE.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        print("pid file unreadable")
        return 1
    for name, pid in pids.items():
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False)
            print(f"[stop] {name} pid={pid}")
        except OSError as exc:
            print(f"[warn] {name}: {exc}")
    PIDFILE.unlink(missing_ok=True)
    return 0


def status() -> int:
    rows = [(f"{t}(:{p})", f"http://127.0.0.1:{p}/health") for t, p in MODEL_SERVERS]
    rows.append((f"webui(:{WEBUI_PORT})", f"http://127.0.0.1:{WEBUI_PORT}/api/health"))
    bad = 0
    for name, url in rows:
        ok, body = _health(url)
        bad += 0 if ok else 1
        print(f"{'UP  ' if ok else 'DOWN'} {name:18s} {body[:110]}")
    return 0 if bad == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.stop:
        return stop()
    if args.status:
        return status()
    return start()


if __name__ == "__main__":
    raise SystemExit(main())
