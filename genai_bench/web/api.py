"""Flask API blueprint for the genai-bench 单人版 WebUI.

One service, one port, relative /api paths only. All state lives on disk
(Store) + in the serial RunQueue; the API key lives only in the request and
the child's whitelisted env -- never on disk, in argv, in logs, or in any
response (guardrail #10).

Endpoints (see docs / spec):
    GET    /api/templates
    POST   /api/preflight
    POST   /api/benchmark/start
    GET    /api/benchmark/queue
    POST   /api/benchmark/<run_id>/stop
    GET    /api/experiments
    GET    /api/experiments/chart
    GET    /api/experiments/export
    GET    /api/experiments/<run_id>
    GET    /api/experiments/<run_id>/params
    PATCH  /api/experiments/<run_id>
    DELETE /api/experiments/<run_id>
    POST   /api/experiments/<run_id>/rerun
    POST   /api/params/parse-cli
    GET    /api/health
    GET    /api/tokenizers
    POST   /api/tokenizers          (multipart upload of local tokenizer files)
    DELETE /api/tokenizers/<id>
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, current_app, request

from genai_bench.web import export, mapping, metrics, naming, tokenizers
from genai_bench.web.config import WebConfig
from genai_bench.web.runner import RunQueue
from genai_bench.web.store import Store
from genai_bench.web.templates import Templates
from genai_bench.web.tokenizers import TokenizerStore


def content_disposition(filename: str) -> str:
    """Build an RFC 6266 / RFC 5987 ``Content-Disposition`` header value.

    HTTP header values are latin-1 only, and gevent's WSGI server refuses
    anything else with ``UnicodeError: Non-latin1 header``. The multi-run export
    name is ``genai-bench_{N}条_{stamp}.zip`` -- the ``条`` used to turn every
    multi-run ZIP download into a 500. So: keep a pure-ASCII fallback in
    ``filename=`` (for old clients) and carry the real name in the UTF-8
    ``filename*=`` parameter, which every current browser honours.
    """
    name = str(filename or "genai-bench_export.zip")
    ascii_name = name.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.replace('"', "").replace("\\", "").strip()
    if not ascii_name or ascii_name.startswith("."):
        ascii_name = "genai-bench_export" + os.path.splitext(name)[1]
    return 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (
        ascii_name,
        quote(name, safe=""),
    )


@dataclass
class Services:
    """Shared, in-process singletons for one WebUI process."""

    config: WebConfig
    templates: Templates
    store: Store
    queue: RunQueue
    tokenizers: TokenizerStore | None = field(default=None)


def _services() -> Services:
    return current_app.extensions["webui"]  # type: ignore[return-value]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(data: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(data, ensure_ascii=False, default=str),
        status=status,
        mimetype="application/json; charset=utf-8",
    )


def _err(message: str, status: int = 400, **extra: Any) -> Response:
    body = {"ok": False, "error": message}
    body.update(extra)
    return _json(body, status)


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _dashboard_url(run_id: str) -> str:
    # Relative SPA route (guardrail #3: no absolute host/port in responses).
    return f"/dashboard/{run_id}"


def _check_optional_backend(resolved: Any) -> str | None:
    """Return an error message when the backend needs an uninstalled extra.

    The OCI SDK is an optional 400+ MB extra. The child process dies with a
    helpful ClickException, but the WebUI runs it with stdout/stderr on
    /dev/null (guardrail: no console spam), so that message would never reach
    the page -- check here instead and answer with the same hint as a 400.
    """
    backend = str(getattr(resolved, "engine", {}).get("api_backend") or "")
    if not backend.startswith("oci-"):
        return None
    module = "oci-openai" if backend == "oci-openai" else "oci"
    try:
        if importlib.util.find_spec(module) is not None:
            return None
    except (ImportError, ValueError):
        return None
    return (
        f"API 后端 '{backend}' 需要可选的 {module} SDK（基础安装里没有）。\n"
        f"在装了本页面服务的那个 Python 环境里执行："
        f"pip install 'genai-bench[oci]'，然后重启 genai-bench webui。\n"
        f"只用本地 / OpenAI 兼容服务的话，把『API 后端』改成 openai / vllm / sglang 即可。"
    )


def _split_secrets_and_values(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Pull secret form keys OUT of values so they are never persisted.

    The frontend may send secrets either in a dedicated ``secrets`` object or
    inline in ``values``; both are collected into the secrets dict, and every
    sensitive key is removed from ``values`` before it reaches resolve() /
    params.json (guardrail #10).
    """
    secrets: dict[str, str] = {}
    raw_secrets = body.get("secrets") or {}
    if isinstance(raw_secrets, dict):
        for k, v in raw_secrets.items():
            if v not in (None, ""):
                secrets[str(k)] = str(v)
    values = dict(body.get("values") or {})
    for key in set(mapping.SECRET_ENV_MAP) | set(mapping.SENSITIVE_KEYS):
        if key in values:
            inline = values.pop(key)
            if inline not in (None, "") and key not in secrets:
                secrets[key] = str(inline)
    return values, secrets


def _templates_payload(s: Services) -> dict[str, Any]:
    """templates.public() plus the live list of uploaded local tokenizers."""
    data = s.templates.public()
    data["tokenizers"] = s.tokenizers.list() if s.tokenizers else []
    return data


def _resolve_tokenizer_ref(s: Services, resolved: Any) -> str | None:
    """Swap a portable ``tokenizer:<id>`` reference for an absolute dir path.

    Called AFTER resolve() and BEFORE validate()/build_argv(). Only the
    ``engine`` copy is mutated; ``resolved.values`` (and therefore the
    sanitized params.json / 反显 / rerun) keeps the portable reference, so a
    rerun re-resolves against whatever bundles exist at that time. No engine
    rewrite is needed: validate_tokenizer() already loads a local path with
    AutoTokenizer.from_pretrained(path) and needs NO HF_TOKEN / network.
    """
    ref = str(resolved.engine.get("model_tokenizer") or "")
    if not ref.startswith(tokenizers.REF_PREFIX):
        return None  # plain HF id or a raw local path: pass through untouched
    tid = ref[len(tokenizers.REF_PREFIX):]
    if not s.tokenizers:
        return f"选中了一个本地 Tokenizer({tid})，但服务端未启用 Tokenizer 存储"
    path = s.tokenizers.path_for(tid)
    if not path:
        return f"选中的本地 Tokenizer({tid}) 不存在或已被删除，请重新上传/选择"
    resolved.engine["model_tokenizer"] = path
    return None


def _meta_for(
    s: Services,
    run_id: str,
    resolved: Any,
    *,
    title: str,
    slug: str,
    group: str = "",
    status: str = "queued",
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "title": title,
        "slug": slug,
        "task": resolved.task,
        "task_id": resolved.task_id,
        "model": resolved.model,
        "start": _now().isoformat(timespec="seconds"),
        "end": None,
        "status": status,
        "tags": [],
        "starred": False,
        "group": group,
        "template_version": s.templates.version,
        "dashboard_url": _dashboard_url(run_id),
    }


def _enrich(s: Services, item: dict[str, Any]) -> dict[str, Any]:
    """Add dashboard_url + key metrics to a list/detail item (read-only)."""
    run_id = item.get("run_id", "")
    item.setdefault("dashboard_url", _dashboard_url(run_id))
    try:
        summ = metrics.summary(s.store, s.templates, run_id)
        item["metrics"] = summ.get("metrics", {})
        item["scenario"] = summ.get("scenario", "")
        item["num_concurrency"] = summ.get("num_concurrency")
        item["batch_size"] = summ.get("batch_size")
        item["has_results"] = summ.get("has_results", False)
        item["metrics_unit"] = summ.get("metrics_unit", "s")
        # keep headline fields consistent with the engine result if meta is thin
        if summ.get("task") and not item.get("task"):
            item["task"] = summ["task"]
        if summ.get("model") and not item.get("model"):
            item["model"] = summ["model"]
    except (OSError, ValueError):
        item.setdefault("metrics", {})
    return item


# ---------------------------------------------------------------------------
# Preflight: ONE minimal, in-memory request. Never persisted, never logged.
# ---------------------------------------------------------------------------
def _preflight(api_base: str, api_key: str, model: str) -> dict[str, Any]:
    base = (api_base or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "status": "unreachable", "message": "缺少 api_base"}
    url = base + ("/models" if base.endswith("/v1") else "/v1/models")
    if base.endswith("/models"):
        url = base
    req = urllib.request.Request(url, method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
            raw = resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "status": "401", "message": "鉴权失败(401/403): API Key 无效或缺失"}
        if exc.code == 404:
            return {
                "ok": True,
                "status": "ok",
                "message": "服务可达, 但 /v1/models 未实现(404); 无法校验模型名, 其余探活通过",
                "note": "models_unavailable",
            }
        return {"ok": False, "status": str(exc.code), "message": f"服务返回 HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = str(getattr(exc, "reason", exc))
        if "timed out" in reason.lower() or "timeout" in reason.lower():
            return {"ok": False, "status": "timeout", "message": "连接超时(5s)"}
        return {"ok": False, "status": "unreachable", "message": f"无法连接: {reason}"}

    # 200: optionally verify the model name against the advertised model list.
    ids: list[str] = []
    try:
        payload = json.loads(raw)
        for entry in (payload.get("data") or []) if isinstance(payload, dict) else []:
            if isinstance(entry, dict) and entry.get("id"):
                ids.append(str(entry["id"]))
    except (ValueError, TypeError):
        ids = []
    if model and ids and model not in ids:
        return {
            "ok": False,
            "status": "model_not_found",
            "message": f"服务端未找到模型 {model!r}",
            "available": ids[:20],
        }
    return {
        "ok": True,
        "status": "ok",
        "message": "探活成功: 服务可达且鉴权通过",
        "model_verified": bool(model and model in ids),
    }


def build_api(services: Services) -> Blueprint:
    bp = Blueprint("webui_api", __name__, url_prefix="/api")

    @bp.get("/health")
    def health():
        return _json({"ok": True, "version": services.templates.version, "service": "genai-bench-webui"})

    @bp.get("/templates")
    def get_templates():
        return _json(_templates_payload(services))

    # -- local tokenizer bundles (offline, no DB, no secrets) --------------------
    @bp.get("/tokenizers")
    def list_tokenizers():
        s = _services()
        return _json({"ok": True, "items": s.tokenizers.list() if s.tokenizers else []})

    @bp.post("/tokenizers")
    def upload_tokenizer():
        """Multipart upload: field ``name`` + one-or-more ``files``."""
        s = _services()
        if not s.tokenizers:
            return _err("服务端未启用 Tokenizer 存储", 500)
        name = (request.form.get("name") or "").strip()
        streams = [(f.filename or "", f.stream) for f in request.files.getlist("files")]
        try:
            item = s.tokenizers.add(name, streams)
        except ValueError as exc:
            return _err(f"上传失败: {exc}", 400)
        except OSError as exc:
            return _err(f"写入 Tokenizer 目录失败: {exc}", 500)
        return _json({"ok": True, "item": item}, 201)

    @bp.delete("/tokenizers/<tid>")
    def delete_tokenizer(tid: str):
        s = _services()
        if not s.tokenizers:
            return _err("服务端未启用 Tokenizer 存储", 500)
        try:
            deleted = s.tokenizers.delete(tid)
        except ValueError as exc:
            return _err(str(exc), 400)
        if not deleted:
            return _err("Tokenizer 不存在", 404)
        return _json({"ok": True, "id": tid, "deleted": True})

    @bp.post("/preflight")
    def preflight():
        body = _body()
        values, secrets = _split_secrets_and_values(body)
        api_base = str(body.get("api_base") or values.get("api_base") or "")
        api_model_name = str(body.get("api_model_name") or values.get("api_model_name") or "")
        api_key = secrets.get("api_key") or secrets.get("model_api_key") or ""
        result = _preflight(api_base, api_key, api_model_name)
        return _json(result, 200 if result["ok"] else 200)

    @bp.post("/benchmark/start")
    def start():
        s = _services()
        body = _body()
        task_id = str(body.get("task_id") or "")
        if task_id not in s.templates.task_ids():
            return _err(f"未知 task_id: {task_id!r} (可选: {', '.join(s.templates.task_ids())})")
        values, secrets = _split_secrets_and_values(body)

        # optional custom title / group / sweep metadata (decoration only)
        custom_title = str(body.get("title") or "").strip()
        group = str(body.get("group") or "").strip()
        sweep_key = body.get("sweep_key")
        sweep_values = body.get("sweep_values")

        try:
            resolved = s.templates.resolve(task_id, values)
        except (KeyError, ValueError, TypeError) as exc:
            return _err(f"参数解析失败: {exc}")
        ref_error = _resolve_tokenizer_ref(s, resolved)
        if ref_error:
            return _err(ref_error, 400)
        errors = mapping.validate(resolved)
        if errors:
            return _err("参数校验失败", errors=errors)
        backend_error = _check_optional_backend(resolved)
        if backend_error:
            return _err(backend_error, 400)

        now = _now()
        run_id = naming.new_run_id(now, s.templates.naming.get("timezone_label", "Asia/Shanghai"))
        title = custom_title or naming.make_title(
            s.templates, resolved, now,
            sweep_key=str(sweep_key) if sweep_key else None,
            sweep_values=list(sweep_values) if isinstance(sweep_values, list) else None,
        )
        slug = naming.make_slug(s.templates, resolved, run_id, now)

        # Persist ONLY sanitized state (guardrail #10/#11).
        meta = _meta_for(s, run_id, resolved, title=title, slug=slug, group=group)
        params = mapping.sanitize(resolved.friendly())
        try:
            s.store.init(run_id, meta, params)
        except (OSError, ValueError) as exc:
            return _err(f"写入实验目录失败: {exc}", 500)

        argv = mapping.build_argv(s.config, resolved, run_id)
        env = mapping.build_child_env(secrets)  # key ONLY here, in memory
        position = s.queue.enqueue(run_id, argv, env, title=title, group=group)
        return _json(
            {
                "ok": True,
                "run_id": run_id,
                "title": title,
                "slug": slug,
                "task_id": task_id,
                "dashboard_url": _dashboard_url(run_id),
                "queue_position": position,
                "argv": argv,  # no secrets by construction; handy for debugging
            },
            201,
        )

    @bp.get("/benchmark/queue")
    def queue_status():
        return _json(_services().queue.status())

    @bp.post("/benchmark/<run_id>/stop")
    def stop(run_id: str):
        result = _services().queue.stop(run_id)
        return _json(result, 200 if result.get("ok") else 404)

    # -- experiments: literal rules are registered before <run_id> -------------
    @bp.get("/experiments/chart")
    def chart():
        s = _services()
        ids = [x for x in (request.args.get("ids") or "").split(",") if x]
        ids = [i for i in ids if s.store.exists(i)]
        metric = request.args.get("metric", "p99")
        x = request.args.get("x", "auto")
        return _json(metrics.chart_data(s.store, s.templates, ids, metric=metric, x=x))

    @bp.get("/experiments/export")
    def export_runs():
        s = _services()
        ids = [x for x in (request.args.get("ids") or "").split(",") if x]
        fmt = (request.args.get("format") or "zip").lower()
        include_charts = request.args.get("include_charts", "1") not in ("0", "false", "no")
        all_flag = request.args.get("all", "") in ("1", "true", "yes")

        if all_flag:
            filters = _export_filters(s)
            ids = _all_ids(s, filters)
        elif not ids:
            return _err("没有选择任何实验(ids 为空且未指定 all=1)")
        ids = [i for i in ids if s.store.exists(i)]
        if not ids:
            return _err("所选实验不存在或已被删除", 404)

        if fmt == "csv":
            data, fname = export.build_csv(s.store, s.templates, ids)
            mimetype = "text/csv; charset=utf-8"
        else:
            data, fname = export.build_zip(s.store, s.templates, ids, include_charts=include_charts)
            mimetype = "application/zip"
        return Response(
            data,
            mimetype=mimetype,
            headers={"Content-Disposition": content_disposition(fname),
                     "Content-Length": str(len(data))},
        )

    @bp.get("/experiments")
    def list_experiments():
        s = _services()
        args = request.args
        result = s.store.list(
            page=_int_arg("page", 1),
            size=_int_arg("size", 20),
            task=args.get("task", ""),
            model=args.get("model", ""),
            status=args.get("status", ""),
            keyword=args.get("keyword", ""),
            group=args.get("group", ""),
            starred=args.get("starred", ""),
            sort=args.get("sort", "start_desc"),
        )
        result["items"] = [_enrich(s, item) for item in result["items"]]
        return _json(result)

    @bp.get("/experiments/<run_id>")
    def get_experiment(run_id: str):
        s = _services()
        if not s.store.exists(run_id):
            return _err("实验不存在", 404, run_id=run_id)
        return _json(_enrich(s, dict(s.store.read_meta(run_id))))

    @bp.get("/experiments/<run_id>/params")
    def get_params(run_id: str):
        """Full sanitized form state for 反显 (incl. collapsed + fixed values)."""
        s = _services()
        if not s.store.exists(run_id):
            return _err("实验不存在或参数快照已丢失", 404, run_id=run_id)
        params = mapping.sanitize(s.store.read_params(run_id))
        task_id = params.get("task_id", "")
        public = s.templates.public()
        fields: list[dict[str, Any]] = []
        common_fields: list[dict[str, Any]] = []
        defaults: dict[str, Any] = {}
        for task in public["tasks"]:
            if task["id"] == task_id:
                fields = task["fields"]
                common_fields = task["common_fields"]
                break
        try:
            defaults = s.templates.defaults(task_id)
        except KeyError:
            defaults = {}
        return _json(
            {
                "run_id": run_id,
                "task_id": task_id,
                "task": params.get("task", ""),
                "template_version": params.get("template_version") or s.templates.version,
                "values": params.get("values", {}),
                "fixed": params.get("fixed", {}),
                "fields": fields,
                "common_fields": common_fields,
                "defaults": defaults,
                "note": s.store.read_note(run_id),
                "meta": s.store.read_meta(run_id),
            }
        )

    @bp.patch("/experiments/<run_id>")
    def patch_experiment(run_id: str):
        s = _services()
        if not s.store.exists(run_id):
            return _err("实验不存在", 404, run_id=run_id)
        changes = {k: v for k, v in _body().items()
                   if k in {"title", "note", "tags", "starred", "group"}}
        meta = s.store.patch(run_id, changes)
        return _json({"ok": True, "run_id": run_id, "meta": meta, "dashboard_url": _dashboard_url(run_id)})

    @bp.delete("/experiments/<run_id>")
    def delete_experiment(run_id: str):
        s = _services()
        if not s.store.exists(run_id):
            return _err("实验不存在", 404, run_id=run_id)
        q = s.queue.status()
        if q.get("running") == run_id or run_id in q.get("queued", []):
            return _err("实验正在运行或排队中, 请先停止", 409)
        folder = s.store.folder(run_id)
        try:
            shutil.rmtree(folder)
        except OSError as exc:
            return _err(f"删除失败: {exc}", 500)
        return _json({"ok": True, "run_id": run_id, "deleted": True})

    @bp.post("/experiments/<run_id>/rerun")
    def rerun(run_id: str):
        """Re-launch from WebUI's OWN sanitized params.json (not engine cmd)."""
        s = _services()
        if not s.store.exists(run_id):
            return _err("实验不存在或参数快照已丢失(无法重跑)", 404, run_id=run_id)
        params = s.store.read_params(run_id)
        task_id = params.get("task_id", "")
        if task_id not in s.templates.task_ids():
            return _err("参数快照里的 task_id 无效, 无法重跑", 404, run_id=run_id)
        _, secrets = _split_secrets_and_values(_body())  # key re-injected by frontend
        try:
            resolved = s.templates.resolve(task_id, params.get("values") or {})
        except (KeyError, ValueError, TypeError) as exc:
            return _err(f"参数快照解析失败: {exc}")
        ref_error = _resolve_tokenizer_ref(s, resolved)
        if ref_error:
            return _err(ref_error, 404)
        errors = mapping.validate(resolved)
        if errors:
            return _err("参数校验失败", errors=errors)
        backend_error = _check_optional_backend(resolved)
        if backend_error:
            return _err(backend_error, 400, run_id=run_id)

        now = _now()
        new_id = naming.new_run_id(now, s.templates.naming.get("timezone_label", "Asia/Shanghai"))
        old_meta = s.store.read_meta(run_id)
        title = naming.make_title(s.templates, resolved, now)
        slug = naming.make_slug(s.templates, resolved, new_id, now)
        meta = _meta_for(s, new_id, resolved, title=title, slug=slug,
                         group=str(old_meta.get("group") or ""))
        meta["tags"] = list(old_meta.get("tags") or [])
        meta["rerun_of"] = run_id
        s.store.init(new_id, meta, mapping.sanitize(resolved.friendly()))
        argv = mapping.build_argv(s.config, resolved, new_id)
        env = mapping.build_child_env(secrets)
        position = s.queue.enqueue(new_id, argv, env, title=title, group=meta["group"])
        return _json(
            {
                "ok": True,
                "run_id": new_id,
                "rerun_of": run_id,
                "title": title,
                "slug": slug,
                "dashboard_url": _dashboard_url(new_id),
                "queue_position": position,
                "key_reinjected": bool(secrets),
            },
            201,
        )

    @bp.post("/params/parse-cli")
    def parse_cli():
        """可选: 粘贴 CLI → 反显用 values (best-effort reverse mapping)."""
        s = _services()
        text = str(_body().get("cli") or "")
        return _json(_parse_cli(s, text))

    return bp


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _export_filters(s: Services) -> dict[str, Any]:
    """Filter spec for all=1 export: explicit JSON `filter` or loose query args."""
    raw = request.args.get("filter") or ""
    filters: dict[str, Any] = {}
    if raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                filters = {k: v for k, v in parsed.items()
                           if k in {"task", "model", "status", "keyword", "group", "starred", "sort"}}
        except ValueError:
            filters = {}
    for key in ("task", "model", "status", "keyword", "group", "starred", "sort"):
        if key in request.args and key not in filters:
            filters[key] = request.args.get(key, "")
    filters.setdefault("sort", "start_desc")
    return filters


def _all_ids(s: Services, filters: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    page = 1
    while True:
        batch = s.store.list(page=page, size=100, **filters)
        ids.extend(m["run_id"] for m in batch["items"])
        if len(ids) >= batch["total"] or not batch["items"]:
            break
        page += 1
    return ids


def _parse_cli(s: Services, text: str) -> dict[str, Any]:
    """Reverse a pasted `genai-bench benchmark ...` command into friendly values."""
    tokens = text.strip().split()
    flags: dict[str, list[str]] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:]
            vals: list[str] = []
            i += 1
            while i < len(tokens) and not tokens[i].startswith("--"):
                vals.append(tokens[i])
                i += 1
            flags.setdefault(key, []).extend(vals)
        else:
            i += 1

    def first(name: str, default: str = "") -> str:
        v = flags.get(name)
        return v[0] if v else default

    task_flag = first("task")
    task_id = ""
    for tid in s.templates.task_ids():
        if s.templates.task(tid)["task"] == task_flag:
            task_id = tid
            break
    if not task_id:
        return {"ok": False, "error": "无法识别 --task", "flags": flags}

    values: dict[str, Any] = {}
    # connection fields
    for key in ("api_backend", "api_base", "api_model_name", "model_tokenizer"):
        if key in flags:
            values[key] = first(key)
    scenario = first("traffic-scenario")
    fields = s.templates.task_fields(task_id)
    for f in fields:
        maps_to = f.get("maps_to")
        if maps_to == "traffic_scenario" and scenario:
            # scenario strings are the option's `scenario`, not `value` -> find by scenario
            for o in f.get("options", []):
                if o.get("scenario") == scenario:
                    values[f["key"]] = o["value"]
                    break
        elif maps_to in ("num_concurrency", "batch_size", "max_time_per_run"):
            flag = {  # friendly CLI flag names
                "num_concurrency": "num-concurrency",
                "batch_size": "batch-size",
                "max_time_per_run": "max-time-per-run",
            }[maps_to]
            if flag in flags:
                try:
                    values[f["key"]] = int(first(flag))
                except ValueError:
                    pass
    defaults = s.templates.defaults(task_id)
    merged = dict(defaults)
    merged.update(values)
    return {
        "ok": True,
        "task_id": task_id,
        "task": task_flag,
        "values": merged,
        "defaults": defaults,
        "parsed_flags": flags,
        "unmatched_flags": [k for k in flags if k not in {
            "task", "api-backend", "api-base", "api-model-name", "model-tokenizer",
            "traffic-scenario", "num-concurrency", "batch-size", "max-time-per-run",
            "max-requests-per-run", "iteration-type", "model", "experiment-base-dir",
            "experiment-folder-name", "log-dir",
        }],
    }
