"""On-disk experiment state (guardrail #4: no database).

Each experiment is a single folder named by its immutable ``run_id``:

    <base>/<run_id>/
        params.json   full sanitized form state (no API key) -- 反显 / rerun source
        meta.json     decoration (title/slug/tags/starred/group) + status + timestamps
        note.txt      free-text note
        *.json        engine result files (ORIGINAL names, never renamed/edited #5)
        experiment_metadata.json / genai_bench.log   written by the engine

Listing = scan directories -> in-memory sort -> slice (no index, no DB).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from genai_bench.web.config import WebConfig
from genai_bench.web.naming import is_valid_run_id

META_FILE = "meta.json"
PARAMS_FILE = "params.json"
NOTE_FILE = "note.txt"

# result JSONs written by the engine's aggregated_metrics_collector.save()
RESULT_RE = re.compile(r"^.+_.+_(?:concurrency|batch_size)_\d+_time_\d+s\.json$")

_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    tmp.replace(path)


class Store:
    """Filesystem-backed experiment store."""

    def __init__(self, config: WebConfig):
        self.config = config
        self.base: Path = config.base_dir
        self.base.mkdir(parents=True, exist_ok=True)

    def folder(self, run_id: str) -> Path:
        if not is_valid_run_id(run_id):
            raise ValueError(f"Invalid run_id: {run_id!r}")
        return self.base / run_id

    def exists(self, run_id: str) -> bool:
        return is_valid_run_id(run_id) and self.folder(run_id).is_dir()

    # -- meta / params / note ----------------------------------------------
    def init(self, run_id: str, meta: dict[str, Any], params: dict[str, Any]) -> None:
        folder = self.folder(run_id)
        with _LOCK:
            folder.mkdir(parents=True, exist_ok=True)
            _write_json(folder / META_FILE, meta)
            _write_json(folder / PARAMS_FILE, params)
            (folder / NOTE_FILE).write_text("", encoding="utf-8")

    def read_meta(self, run_id: str) -> dict[str, Any]:
        return _read_json(self.folder(run_id) / META_FILE, {})

    def read_params(self, run_id: str) -> dict[str, Any]:
        return _read_json(self.folder(run_id) / PARAMS_FILE, {})

    def read_note(self, run_id: str) -> str:
        try:
            return (self.folder(run_id) / NOTE_FILE).read_text(encoding="utf-8")
        except OSError:
            return ""

    def patch(self, run_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """PATCH /api/experiments/{run_id}: decoration + status only.

        Never writes run_id, folder name, or dashboard_url.
        """
        allowed = {"title", "note", "tags", "starred", "group", "status", "end", "error"}
        with _LOCK:
            meta = self.read_meta(run_id)
            for key, value in changes.items():
                if key == "note":
                    (self.folder(run_id) / NOTE_FILE).write_text(
                        str(value), encoding="utf-8"
                    )
                elif key in allowed:
                    meta[key] = value
            meta["updated"] = _now_iso()
            _write_json(self.folder(run_id) / META_FILE, meta)
            return meta

    def set_status(self, run_id: str, status: str, *, error: str | None = None) -> None:
        self.patch(
            run_id,
            {"status": status, "end": _now_iso() if status in ("done", "failed", "stopped") else None, "error": error},
        )

    # -- result files -------------------------------------------------------
    def result_files(self, run_id: str) -> list[Path]:
        folder = self.folder(run_id)
        if not folder.is_dir():
            return []
        return sorted(
            p for p in folder.glob("*.json")
            if RESULT_RE.match(p.name) and p.name not in (META_FILE, PARAMS_FILE)
        )

    # -- listing (scan -> sort -> slice) ------------------------------------
    def _all_metas(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        with _LOCK:
            for folder in self.base.iterdir():
                if not folder.is_dir() or not is_valid_run_id(folder.name):
                    continue
                meta = _read_json(folder / META_FILE, {})
                meta.setdefault("run_id", folder.name)
                meta.setdefault("title", folder.name)
                meta.setdefault("status", "unknown")
                meta.setdefault("starred", False)
                meta.setdefault("tags", [])
                meta.setdefault("group", "")
                # derive start from folder mtime if missing
                meta.setdefault(
                    "start",
                    datetime.fromtimestamp(folder.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                )
                meta["_folder"] = str(folder)
                items.append(meta)
        return items

    def list(
        self,
        *,
        page: int = 1,
        size: int = 20,
        task: str = "",
        model: str = "",
        status: str = "",
        keyword: str = "",
        group: str = "",
        starred: str = "",
        sort: str = "start_desc",
        ids: list[str] | None = None,
    ) -> dict[str, Any]:
        items = self._all_metas()
        # filters
        if ids is not None:
            wanted = set(ids)
            items = [m for m in items if m["run_id"] in wanted]
        if task:
            items = [m for m in items if task in (m.get("task") or "")]
        if model:
            items = [m for m in items if model.lower() in (m.get("model") or "").lower()]
        if status:
            items = [m for m in items if m.get("status") == status]
        if group:
            items = [m for m in items if m.get("group") == group]
        if starred in ("1", "true", "yes"):
            items = [m for m in items if m.get("starred")]
        if keyword:
            kw = keyword.lower()
            items = [
                m for m in items
                if kw in (m.get("title") or "").lower()
                or kw in (m.get("run_id") or "").lower()
                or kw in (m.get("slug") or "").lower()
                or kw in " ".join(m.get("tags") or []).lower()
                or kw in (m.get("model") or "").lower()
            ]
        # sort (starred pinned across pages, then configured order)
        if sort == "start_asc":
            items.sort(key=lambda m: m.get("start") or "")
        elif sort == "title_asc":
            items.sort(key=lambda m: (m.get("title") or "").lower())
        else:  # start_desc
            items.sort(key=lambda m: m.get("start") or "", reverse=True)
        items.sort(key=lambda m: (not m.get("starred"),))  # stable: starred first
        total = len(items)
        page = max(1, int(page))
        size = int(size) if int(size) in (20, 50, 100) else 20
        start = (page - 1) * size
        page_items = items[start : start + size]
        for m in page_items:
            m.pop("_folder", None)
        return {"total": total, "page": page, "size": size, "items": page_items}
