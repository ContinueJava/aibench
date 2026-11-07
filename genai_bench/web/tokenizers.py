"""Local tokenizer bundles for the genai-bench 单人版 WebUI (offline, no DB).

The user uploads the files of a HuggingFace-style tokenizer directory once
(tokenizer.json / tokenizer_config.json / vocab.json / merges.txt /
tokenizer.model / spiece.model / ...), then SELECTS one from a dropdown. At
launch time the portable ``tokenizer:<id>`` reference is swapped for the
absolute on-disk directory, which the engine's ``validate_tokenizer`` loads
with ``AutoTokenizer.from_pretrained(path)`` -- NO HF_TOKEN, NO network, and
NO change to the engine (guardrail #5).

State lives only on disk: ``<base_dir>/_tokenizers/<id>/manifest.json`` plus
the uploaded files. ``_tokenizers`` is not a valid run_id, so the experiment
``Store`` never mistakes it for an experiment. Tokenizer files contain no API
keys and are deliberately NOT copied into export zips. No database, no CDN,
no runtime network.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable

# Public id shape: safe as a directory name and as a URL path segment.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Uploaded names are basenames only (no separators, no traversal, data files).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ALLOWED_EXTS = {
    ".json",
    ".txt",
    ".model",
    ".jinja",
    ".jinja2",
    ".spm",
    ".bpe",
    ".vocab",
    ".yaml",
    ".yml",
    ".py",
}
# Extensionless names that some tokenizers ship (e.g. ``merges``).
_ALLOWED_NAMES = {
    "vocab",
    "merges",
    "special_tokens_map",
    "tokenizer",
    "added_tokens",
}
# At least one of these means the bundle can actually be loaded offline.
_CORE_FILES = {
    "tokenizer.json",
    "tokenizer_config.json",
    "tokenizer.model",
    "spiece.model",
    "vocab.json",
    "merges.txt",
    "vocab.txt",
    "config.json",
}

MAX_FILE_BYTES = 64 * 1024 * 1024  # 64 MiB per tokenizer file
MAX_TOTAL_BYTES = 256 * 1024 * 1024  # 256 MiB per upload
_CHUNK = 64 * 1024

_MANIFEST = "manifest.json"
REF_PREFIX = "tokenizer:"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _make_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:32]
    return f"{slug or 'tok'}-{uuid.uuid4().hex[:6]}"


def _safe_name(filename: str) -> str:
    """Return a validated basename, or raise ValueError (blocks path traversal)."""
    name = str(filename or "").strip().split("\x00")[0]
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"非法文件名(含路径分隔符或 ..): {filename!r}")
    if not _NAME_RE.match(name):
        raise ValueError(f"非法文件名(只允许字母数字 . _ -): {filename!r}")
    suffix = Path(name).suffix.lower()
    if suffix not in _ALLOWED_EXTS and name not in _ALLOWED_NAMES and suffix != "":
        raise ValueError(f"不支持的文件类型: {filename!r}")
    if suffix == "" and name not in _ALLOWED_NAMES:
        raise ValueError(f"不支持的无扩展名文件: {filename!r}")
    return name


def _read_capped(stream: BinaryIO) -> bytes:
    """Read a stream into memory with a hard per-file cap."""
    buf = bytearray()
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_FILE_BYTES:
            raise ValueError(
                f"单个文件超过上限 {MAX_FILE_BYTES // (1024 * 1024)} MiB，"
                "请检查是否误选了模型权重文件(加载 tokenizer 不需要权重)"
            )
    if not buf:
        raise ValueError("空文件")
    return bytes(buf)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default


class TokenizerStore:
    """Manages ``<base_dir>/_tokenizers/<id>/`` bundles on local disk."""

    def __init__(self, base_dir: Path | str):
        self.root = Path(base_dir) / "_tokenizers"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # -- helpers ------------------------------------------------------------
    def _folder(self, tid: str) -> Path:
        if not _ID_RE.match(tid or ""):
            raise ValueError(f"非法 Tokenizer id: {tid!r}")
        return self.root / tid

    @staticmethod
    def _public(manifest: dict[str, Any]) -> dict[str, Any]:
        files = [f for f in manifest.get("files", []) if isinstance(f, dict)]
        names = {str(f.get("name", "")) for f in files}
        return {
            "id": manifest.get("id", ""),
            "name": manifest.get("name", ""),
            "created": manifest.get("created", ""),
            "files": files,
            "file_count": len(files),
            "size": sum(int(f.get("size", 0)) for f in files),
            "ready": bool(names & _CORE_FILES),
        }

    # -- read ---------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            items: list[dict[str, Any]] = []
            if not self.root.is_dir():
                return items
            for folder in self.root.iterdir():
                if not folder.is_dir() or not _ID_RE.match(folder.name):
                    continue
                manifest = _read_json(folder / _MANIFEST, {})
                if not manifest or manifest.get("id") != folder.name:
                    continue
                items.append(self._public(manifest))
            items.sort(key=lambda x: x.get("created", ""), reverse=True)
            return items

    def get(self, tid: str) -> dict[str, Any] | None:
        folder = self._folder(tid)
        manifest = _read_json(folder / _MANIFEST, {})
        if not manifest or manifest.get("id") != tid:
            return None
        return self._public(manifest)

    def path_for(self, tid: str) -> str | None:
        """Absolute directory path for the engine, or None if unknown/empty.

        This is what a ``tokenizer:<id>`` form reference resolves to just
        before ``build_argv``; ``params.json`` keeps the portable reference so
        rerun / 反显 keep working even if the volume is remounted.
        """
        folder = self._folder(tid)
        manifest = _read_json(folder / _MANIFEST, {})
        if not manifest or manifest.get("id") != tid:
            return None
        if not folder.is_dir() or not any(folder.iterdir()):
            return None
        return str(folder.resolve())

    # -- write --------------------------------------------------------------
    def add(self, name: str, files: Iterable[tuple[str, BinaryIO]]) -> dict[str, Any]:
        """Validate + store one bundle atomically; return its public dict."""
        clean_name = (str(name or "").strip() or "tokenizer")[:80]
        prepared: list[tuple[str, BinaryIO]] = []
        for filename, stream in files:
            prepared.append((_safe_name(filename), stream))
        if not prepared:
            raise ValueError("没有选择任何文件")

        tid = _make_id(clean_name)
        folder = self._folder(tid)
        folder.mkdir(parents=True, exist_ok=False)
        written: list[dict[str, Any]] = []
        try:
            total = 0
            for safe, stream in prepared:
                data = _read_capped(stream)
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise ValueError(
                        f"本次上传总大小超过上限 {MAX_TOTAL_BYTES // (1024 * 1024)} MiB"
                    )
                (folder / safe).write_bytes(data)
                written.append({"name": safe, "size": len(data)})
            manifest = {
                "id": tid,
                "name": clean_name,
                "created": _now_iso(),
                "files": written,
                "version": 1,
            }
            _write_json(folder / _MANIFEST, manifest)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return self._public(manifest)

    def delete(self, tid: str) -> bool:
        folder = self._folder(tid)
        with self._lock:
            if not folder.is_dir():
                return False
            shutil.rmtree(folder)
            return True
