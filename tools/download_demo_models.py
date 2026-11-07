"""Download the three tiny demo models into ./local_models via the HF mirror."""

import os
import sys
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from huggingface_hub import snapshot_download  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "local_models"
OUT.mkdir(exist_ok=True)

MODELS = {
    "chat": "Qwen/Qwen2.5-0.5B-Instruct",
    "embedding": "BAAI/bge-small-zh-v1.5",
    "rerank": "cross-encoder/ms-marco-MiniLM-L-6-v2",
}

ALLOW = [
    "*.json",
    "*.txt",
    "*.model",
    "*.safetensors",
    "*.bin",
    "*.merges",
    "*.vocab",
]

for name, repo in MODELS.items():
    dest = OUT / name
    if dest.exists() and any(dest.glob("*.safetensors")):
        print(f"[skip] {name} already at {dest}", flush=True)
        continue
    print(f"[get ] {name} <- {repo}", flush=True)
    path = snapshot_download(
        repo_id=repo,
        local_dir=str(dest),
        allow_patterns=ALLOW,
        max_workers=4,
    )
    size = sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())
    print(f"[done] {name} -> {path} ({size / 1e6:.1f} MB)", flush=True)

print("ALL DONE", flush=True)
sys.exit(0)
