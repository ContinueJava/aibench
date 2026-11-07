"""Tiny OpenAI-compatible model server for the genai-bench WebUI demo.

Serves exactly the three shapes genai-bench's OpenAI vendor sends:

    POST /v1/chat/completions   (SSE stream, needs stream_options.include_usage)
    POST /v1/embeddings         (needs usage.prompt_tokens in the response)
    POST /v1/rerank             (vLLM/SGLang style: results[].relevance_score)

Nothing here is meant to be fast or production grade: it loads a small model
on CPU so the dashboard can be exercised end to end on a laptop.

Usage:
    python tools/light_model_server.py --task chat      --port 8101
    python tools/light_model_server.py --task embedding --port 8102
    python tools/light_model_server.py --task rerank    --port 8103
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from typing import Any

import torch
from flask import Flask, Response, jsonify, request
from transformers import (
    AutoModel,
    AutoModelForCausalLM,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    StoppingCriteria,
    StoppingCriteriaList,
    TextIteratorStreamer,
)

DEFAULT_MODELS = {
    "chat": "local_models/chat",
    "embedding": "local_models/embedding",
    "rerank": "local_models/rerank",
}


class _TokenCounter(StoppingCriteria):
    """Counts generated steps so we can report exact completion_tokens.

    ``input_ids.shape[-1]`` is always the FULL sequence (prompt + generated), so
    the prompt length must be subtracted -- otherwise the reported
    ``completion_tokens`` is inflated by however long the prompt happened to be,
    which makes the throughput numbers genai-bench records wrong.
    """

    def __init__(self, prompt_len: int) -> None:
        self.prompt_len = max(0, int(prompt_len))
        self.count = 0

    def __call__(self, input_ids, scores, **kwargs) -> bool:  # noqa: D102
        self.count = max(0, int(input_ids.shape[-1]) - self.prompt_len)
        return False


def _sse(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def build_app(task: str, model_path: str, tokenizer_path: str, max_model_len: int):
    app = Flask("light-model-server")

    tok = AutoTokenizer.from_pretrained(tokenizer_path, trust_remote_code=True)

    if task == "chat":
        model = AutoModelForCausalLM.from_pretrained(
            model_path, torch_dtype=torch.float32, trust_remote_code=True
        )
    elif task == "embedding":
        model = AutoModel.from_pretrained(
            model_path, torch_dtype=torch.float32, trust_remote_code=True
        )
    elif task == "rerank":
        model = AutoModelForSequenceClassification.from_pretrained(
            model_path, torch_dtype=torch.float32, trust_remote_code=True
        )
    else:  # pragma: no cover - argparse restricts this
        raise SystemExit(f"unknown task {task!r}")

    model.eval()
    served_name = model_path.replace("\\", "/").rstrip("/").split("/")[-1]
    print(f"[ready] task={task} model={served_name} threads={torch.get_num_threads()}")

    @app.get("/health")
    def health():  # noqa: D103
        return jsonify({"ok": True, "task": task, "model": served_name})

    @app.get("/v1/models")
    def models():  # noqa: D103
        return jsonify(
            {
                "object": "list",
                "data": [{"id": served_name, "object": "model", "owned_by": "local"}],
            }
        )

    @app.get("/models")
    def models_alt():  # noqa: D103
        return models()

    # ---------------------------------------------------------------- chat
    if task == "chat":

        @app.post("/v1/chat/completions")
        def chat_completions():  # noqa: D103
            body = request.get_json(force=True, silent=True) or {}
            messages = body.get("messages") or []
            max_tokens = int(body.get("max_tokens") or 64)
            max_tokens = max(1, min(max_tokens, max_model_len))

            prompt = tok.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            inputs = tok(prompt, return_tensors="pt", truncation=True, max_length=4096)
            prompt_tokens = int(inputs["input_ids"].shape[1])

            streamer = TextIteratorStreamer(
                tok, skip_prompt=True, skip_special_tokens=True
            )
            counter = _TokenCounter(prompt_tokens)
            kwargs = dict(
                **inputs,
                max_new_tokens=max_tokens,
                do_sample=False,
                streamer=streamer,
                stopping_criteria=StoppingCriteriaList([counter]),
                pad_token_id=tok.eos_token_id,
            )
            thread = threading.Thread(target=model.generate, kwargs=kwargs, daemon=True)
            thread.start()

            created = int(time.time())
            chunk_id = f"chatcmpl-{created}-{threading.get_ident()}"

            def stream():  # noqa: D103
                yield _sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": served_name,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": ""},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                for piece in streamer:
                    if not piece:
                        continue
                    yield _sse(
                        {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": served_name,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": piece},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                yield _sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": served_name,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
                yield _sse(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": served_name,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": counter.count,
                            "total_tokens": prompt_tokens + counter.count,
                        },
                    }
                )
                yield "data: [DONE]\n\n"

            return Response(stream(), mimetype="text/event-stream")

    # ----------------------------------------------------------- embedding
    if task == "embedding":
        use_cls = "bge" in served_name.lower()

        @app.post("/v1/embeddings")
        def embeddings():  # noqa: D103
            body = request.get_json(force=True, silent=True) or {}
            raw = body.get("input")
            texts = [raw] if isinstance(raw, str) else list(raw or [])
            if not texts:
                return jsonify({"error": {"message": "empty input"}}), 400

            enc = tok(
                texts,
                padding=True,
                truncation=True,
                max_length=max_model_len,
                return_tensors="pt",
            )
            with torch.no_grad():
                hidden = model(**enc).last_hidden_state
            if use_cls:
                vecs = hidden[:, 0]
            else:
                mask = enc["attention_mask"].unsqueeze(-1).float()
                vecs = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-6)
            vecs = torch.nn.functional.normalize(vecs, p=2, dim=1)

            tokens = int(enc["attention_mask"].sum())
            return jsonify(
                {
                    "object": "list",
                    "model": served_name,
                    "data": [
                        {
                            "object": "embedding",
                            "index": i,
                            "embedding": vecs[i].tolist(),
                        }
                        for i in range(len(texts))
                    ],
                    "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
                }
            )

    # -------------------------------------------------------------- rerank
    if task == "rerank":

        @app.post("/v1/rerank")
        def rerank():  # noqa: D103
            body = request.get_json(force=True, silent=True) or {}
            query = body.get("query") or ""
            docs = list(body.get("documents") or [])
            if not docs:
                return jsonify({"error": {"message": "empty documents"}}), 400

            scores: list[float] = []
            total_tokens = 0
            chunk = 32
            for start in range(0, len(docs), chunk):
                batch = docs[start : start + chunk]
                enc = tok(
                    [(query, d) for d in batch],
                    padding=True,
                    truncation=True,
                    max_length=max_model_len,
                    return_tensors="pt",
                )
                total_tokens += int(enc["attention_mask"].sum())
                with torch.no_grad():
                    logits = model(**enc).logits.view(-1)
                scores.extend(torch.sigmoid(logits).tolist())

            order = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
            return jsonify(
                {
                    "id": f"rerank-{int(time.time())}",
                    "model": served_name,
                    "results": [
                        {"index": i, "relevance_score": float(scores[i])} for i in order
                    ],
                    "usage": {"prompt_tokens": total_tokens, "total_tokens": total_tokens},
                }
            )

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=["chat", "embedding", "rerank"], required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--model", default=None, help="override the model directory")
    ap.add_argument("--tokenizer", default=None, help="override the tokenizer directory")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--max-model-len", type=int, default=512)
    args = ap.parse_args()

    torch.set_num_threads(args.threads)
    model_path = args.model or DEFAULT_MODELS[args.task]
    tokenizer_path = args.tokenizer or model_path

    app = build_app(args.task, model_path, tokenizer_path, args.max_model_len)
    app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
