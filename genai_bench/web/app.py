"""Flask app factory + single-process/single-port hosting.

Guardrails handled here:
  #1/#8  one Flask process served by gevent.pywsgi.WSGIServer on ONE port;
         the explicit Dashboard/monitor routes are registered BEFORE the SPA
         fallback, unmatched /api/* returns JSON 404 (never HTML), and any
         other frontend route falls back to index.html (refresh never 404s).
  #3     relative /api paths only; host/port come from CLI/env.
  #4/#12 static assets (incl. vendored offline chart lib) are shipped inside
         the Python package under genai_bench/web/static/, no CDN/Node at runtime.

There is NO existing HTTP Dashboard in upstream genai-bench: ui/dashboard.py
is a Rich *terminal* Live panel inside the benchmark child process. The WebUI
therefore hosts a read-only in-app live monitor SPA route keyed by run_id;
it reads persisted engine result files (it never recomputes a metric).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Flask, Response, request, send_from_directory

from genai_bench.web.api import Services, build_api
from genai_bench.web.config import WebConfig
from genai_bench.web.runner import RunQueue
from genai_bench.web.store import Store
from genai_bench.web.templates import Templates
from genai_bench.web.tokenizers import TokenizerStore


def create_services(config: WebConfig | None = None, *, queue: RunQueue | None = None) -> Services:
    """Build the shared in-process singletons (also used directly by tests)."""
    cfg = config or WebConfig.from_env()
    cfg.ensure_dirs()
    templates = Templates.load(cfg)
    store = Store(cfg)
    # cwd for the child subprocess: repo root / site-packages parent so that
    # ``python -m genai_bench.cli.cli`` resolves in both a source checkout and
    # an installed wheel. build_child_env also puts PROJECT_ROOT on PYTHONPATH.
    from genai_bench.web.config import PROJECT_ROOT

    cwd = str(cfg.child_cwd or PROJECT_ROOT)
    run_queue = queue or RunQueue(store, cwd=cwd)
    tokenizer_store = TokenizerStore(cfg.base_dir)
    return Services(
        config=cfg,
        templates=templates,
        store=store,
        queue=run_queue,
        tokenizers=tokenizer_store,
    )


def create_app(config: WebConfig | None = None, *, services: Services | None = None) -> Flask:
    """Create the one Flask app for the whole WebUI."""
    cfg = config or WebConfig.from_env()
    cfg.ensure_dirs()
    svc = services or create_services(cfg)

    app = Flask(__name__, static_folder=None)  # we serve static ourselves
    app.extensions["webui"] = svc
    app.config["WEBUI"] = svc

    # -- API (all relative /api paths) -------------------------------------
    app.register_blueprint(build_api(svc))

    # -- unmatched /api -> JSON 404 (guardrail #8) -------------------------
    @app.route("/api/<path:subpath>", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
    def api_not_found(subpath: str):
        return _api_404(subpath)

    # -- static assets (vendored chart lib lives here, offline) ------------
    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(str(cfg.static_dir), filename)

    # -- Dashboard / live monitor routes registered BEFORE SPA fallback -----
    # There is no upstream HTTP dashboard, so this is the in-app read-only
    # monitor keyed by the existing run_id primary key (no invented locator).
    @app.get("/dashboard")
    @app.get("/dashboard/<run_id>")
    def dashboard(run_id: str | None = None):
        return _index(cfg)

    # -- SPA routes (refresh never 404s) -----------------------------------
    @app.get("/")
    def root():
        return _index(cfg)

    @app.get("/<path:path>")
    def spa_fallback(path: str):
        # Defense in depth: a stray /api/* that dodged the catch-all above.
        if path.rstrip("/").startswith("api"):
            return _api_404(path)
        if path == "favicon.ico":
            return Response(status=204)
        return _index(cfg)

    @app.errorhandler(404)
    def handle_404(_error):  # pragma: no cover - exercised via /api/*
        if request.path.startswith("/api"):
            return _api_404(request.path)
        return _index(cfg)

    return app


def _api_404(path: str) -> Response:
    return Response(
        json.dumps({"ok": False, "error": "Not Found", "path": f"/api/{path.lstrip('/')}"}, ensure_ascii=False),
        status=404,
        mimetype="application/json; charset=utf-8",
    )


def _index(config: WebConfig) -> Response:
    index = Path(config.static_dir) / "index.html"
    if not index.is_file():
        return Response(
            "<h1>genai-bench WebUI</h1><p>static/index.html 缺失: 请先构建前端产物 "
            "(python -m genai_bench.web.build 或 Dockerfile 的 frontend 构建阶段)。</p>",
            status=503,
            mimetype="text/html; charset=utf-8",
        )
    return Response(index.read_text(encoding="utf-8"), mimetype="text/html; charset=utf-8")


def serve(config: WebConfig | None = None, *, app: Flask | None = None) -> None:
    """Serve the app on ONE port with gevent.pywsgi (single process)."""
    cfg = config or WebConfig.from_env()
    flask_app = app or create_app(cfg)
    try:
        from gevent.pywsgi import WSGIServer
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("gevent 未安装: 无法启动单进程 WSGI 服务") from exc
    server = WSGIServer((cfg.host, cfg.port), flask_app)
    print(
        f"genai-bench WebUI 已启动: http://{cfg.host}:{cfg.port} "
        f"(experiments: {cfg.base_dir})",
        flush=True,
    )
    server.serve_forever()
