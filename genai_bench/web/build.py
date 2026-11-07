"""Pure-Python fallback frontend builder (no Node required).

Copies frontend/{index.html,app.css,app.js,chart.js} into the shipped
genai_bench/web/static/ directory. The Dockerfile's Node build stage runs
frontend/build.mjs; this module lets a source checkout / `pip install .`
produce the same static assets without Node, so the WebUI always opens.

Usage:  python -m genai_bench.web.build
"""

from __future__ import annotations

import shutil
from pathlib import Path

_FILES = ("index.html", "app.css", "app.js", "chart.js")


def build(root: Path | None = None) -> Path:
    root = (root or Path(__file__).resolve().parents[2]).resolve()
    src = root / "frontend"
    out = Path(__file__).resolve().parent / "static"
    out.mkdir(parents=True, exist_ok=True)
    if not src.is_dir():
        # Installed wheel: static/ is already shipped, nothing to build.
        return out
    for name in _FILES:
        shutil.copyfile(src / name, out / name)
    return out


if __name__ == "__main__":
    dest = build()
    print(f"Built static assets -> {dest}")
