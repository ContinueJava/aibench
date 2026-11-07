"""Tests for the genai-bench 单人版 WebUI.

These are the executable form of the spec's 自测清单. They use Flask's
``test_client`` and an injectable fake process launcher, so no real benchmark,
no network, and no GPU is needed. A temporary directory is the experiment
store (no database), and the bundled gevent/flask deps are already present in
the project's test environment.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from genai_bench.web.app import create_app, create_services
from genai_bench.web.config import WebConfig
from genai_bench.web.runner import RunQueue
from genai_bench.web.store import Store
from genai_bench.web.templates import Templates

SECRET = "sk-SUPER-SECRET-DO-NOT-LEAK-1234"


class FakeProc:
    """Minimal Popen stand-in controlled by the test."""

    def __init__(self):
        self._code = None  # None => still running
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._code

    def terminate(self):
        self.terminated = True
        self._code = -15

    def wait(self, timeout=None):
        return self._code

    def kill(self):
        self.killed = True
        self._code = -9

    def finish(self, code=0):
        self._code = code


@pytest.fixture()
def env(tmp_path: Path):
    base = tmp_path / "experiments"
    cfg = WebConfig(base_dir=base)
    launched: dict[str, FakeProc] = {}
    calls: list[tuple[list[str], dict, str]] = []

    def launcher(argv, child_env, cwd):
        proc = FakeProc()
        run_id = argv[argv.index("--experiment-folder-name") + 1]
        launched[run_id] = proc
        calls.append((argv, child_env, cwd))
        return proc

    queue = RunQueue(Store(cfg), launcher=launcher, poll_interval=0.01)
    app = create_app(cfg, services=create_services(cfg, queue=queue))
    client = app.test_client()
    yield {
        "cfg": cfg,
        "app": app,
        "client": client,
        "queue": queue,
        "store": Store(cfg),
        "launched": launched,
        "calls": calls,
        "base": base,
    }
    queue.close()


def start(client, task_id="chat", *, values=None, secrets=None, **extra):
    body = {
        "task_id": task_id,
        "values": values
        or {
            "api_base": "http://localhost:8000/v1",
            "api_model_name": "Qwen2.5-7B-Instruct",
            "model_tokenizer": "Qwen/Qwen2.5-7B-Instruct",
        },
        "secrets": secrets if secrets is not None else {"api_key": SECRET},
    }
    body.update(extra)
    return client.post("/api/benchmark/start", json=body)


def advance(env, run_id, code=0):
    """Let the queue worker tick, then finish the fake child, then tick again."""
    import time

    q = env["queue"]
    q._tick()  # start the queued run
    time.sleep(0.03)
    proc = env["launched"][run_id]
    proc.finish(code)
    q._tick()  # harvest


def write_result(store, run_id, *, num_concurrency=32, p99=0.9, rps=12.3, errors=0.01, completed=100):
    """Write an engine-shaped result JSON with ORIGINAL naming convention."""
    folder = store.folder(run_id)
    name = f"D1024_512_text-to-text_num_concurrency_{num_concurrency}_time_600s.json"
    data = {
        "aggregated_metrics": {
            "scenario": "D(1024,512)",
            "num_concurrency": num_concurrency,
            "batch_size": 1,
            "iteration_type": "num_concurrency",
            "run_duration": 600,
            "requests_per_second": rps,
            "mean_total_tokens_throughput_tokens_per_s": 4500.0,
            "error_rate": errors,
            "num_completed_requests": completed,
            "stats": {
                "e2e_latency": {"p50": 0.4, "p99": p99},
                "ttft": {"p50": 0.05, "p99": 0.12},
            },
        },
        "individual_request_metrics": [
            {"e2e_latency": 0.3 + (i % 7) * 0.1, "ttft": 0.04, "error_code": 0}
            for i in range(40)
        ],
        "_time_unit": "s",
    }
    (folder / name).write_text(json.dumps(data), encoding="utf-8")
    return name


def write_batch_result(store, run_id, *, batch_size, num_concurrency, p99=0.9):
    """Engine-shaped result for a batch_size-iteration run (embedding / rerank).

    Filename follows the engine's own convention, so the reader must accept a
    ``batch_size`` iteration token just like ``num_concurrency``.
    """
    folder = store.folder(run_id)
    name = f"E512_text-to-embeddings_batch_size_{batch_size}_time_600s.json"
    data = {
        "aggregated_metrics": {
            "scenario": "E(512)",
            "num_concurrency": num_concurrency,
            "batch_size": batch_size,
            "iteration_type": "batch_size",
            "run_duration": 600,
            "requests_per_second": 12.3,
            "mean_total_tokens_throughput_tokens_per_s": 4500.0,
            "error_rate": 0.0,
            "num_completed_requests": 100,
            "stats": {"e2e_latency": {"p50": 0.4, "p99": p99}},
        },
        "individual_request_metrics": [
            {"e2e_latency": 0.3 + (i % 5) * 0.1, "error_code": 0} for i in range(20)
        ],
        "_time_unit": "s",
    }
    (folder / name).write_text(json.dumps(data), encoding="utf-8")
    return name


# ---------------------------------------------------------------------------
# 1. App factory / routing / static (guardrails #1, #3, #8, #12)
# ---------------------------------------------------------------------------
def test_health_and_templates(env):
    c = env["client"]
    assert c.get("/api/health").get_json()["ok"] is True
    payload = c.get("/api/templates").get_json()
    assert payload["version"] == "3.0"
    assert {t["id"] for t in payload["tasks"]} == {"chat", "embedding", "rerank"}
    # stress-tier defaults are exposed from the ONE config file
    chat = next(t for t in payload["tasks"] if t["id"] == "chat")
    assert any(o["value"] == 32 and o.get("short") == "c32"
               for f in chat["fields"] for o in f["options"] if f["key"] == "concurrency")
    assert payload["max_requests_per_run"] == 2000000000  # 时长唯一真实限制
    # embedding / rerank concurrency is now EDITABLE (user m02525): batch x
    # concurrency must be expressible, so it is a selectable fixed worker count
    # rather than a readonly 1.
    for tid in ("embedding", "rerank"):
        task = next(t for t in payload["tasks"] if t["id"] == tid)
        conc = next(f for f in task["fields"] if f["key"] == "concurrency")
        assert conc["type"] == "select"
        assert conc["default"] == 1  # historical default preserved
        assert conc.get("sweepable") is True
        values = [o["value"] for o in conc["options"]]
        assert 1 in values and 50 not in values  # ladder, plus free entry


def test_gpu_type_is_free_text_with_chinese_vendors(env):
    """GPU type is annotation-only; the list must not be an NVIDIA/AMD-only enum.

    Upstream used to hard-code a click.Choice of 8 SKUs, so Huawei Ascend and
    Alibaba Zhenwu could not even be recorded (user m03135).
    """
    payload = env["client"].get("/api/templates").get_json()
    for task in payload["tasks"]:
        f = next(x for x in task["common_fields"] if x["key"] == "server_gpu_type")
        values = {o["value"] for o in f["options"]}
        # Huawei Ascend: 910 (B/B3/B4/C) and the 2026 950 series, plus Atlas forms.
        assert {
            "Ascend-310P",
            "Ascend-910B",
            "Ascend-910B3",
            "Ascend-910B4",
            "Ascend-910C",
            "Ascend-950PR",
            "Ascend-950DT",
            "Atlas-300I-Duo",
            "Atlas-800T-A2",
        } <= values
        # Alibaba: Zhenwu PPU series (810E / M890 / V900) and the older Hanguang.
        assert {"Zhenwu-810E", "Zhenwu-M890", "Zhenwu-V900", "Hanguang-800"} <= values
        # Other domestic vendors still selectable.
        assert {"Hygon-DCU", "MLU-590", "Kunlunxin-P800"} <= values
        assert f["allow_custom"] is True
        assert f["custom_type"] == "text"


def test_api_404_is_json_not_html(env):
    r = env["client"].get("/api/whatever/deep/path")
    assert r.status_code == 404
    assert r.is_json
    assert r.get_json()["ok"] is False


def test_spa_routes_never_404_and_static_served(env):
    c = env["client"]
    for path in ("/", "/history", "/new", "/dashboard", "/dashboard/abc", "/anything/else"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.content_type
        assert b"genai-bench" in r.data
    for asset in ("app.css", "app.js", "chart.js"):
        r = c.get(f"/static/{asset}")
        assert r.status_code == 200
        assert len(r.data) > 0
    # offline chart lib: no CDN/runtime network references (only comments may
    # mention the word "cdn"; check for actual remote URLs / script srcs).
    chart_js = c.get("/static/chart.js").data.decode("utf-8")
    assert "https://" not in chart_js and "http://" not in chart_js
    assert "<script" not in chart_js and "src=" not in chart_js


def test_inline_handlers_never_emit_double_quotes():
    """Inline handlers sit inside a double-quoted HTML attribute.

    A ``JSON.stringify`` argument there emits ``"`` and terminates the attribute,
    which turns the button into dead markup (this really happened: the 批大小
    quick-pick chips could not be clicked). Guard the frontend source.
    """
    src_path = Path(__file__).resolve().parents[2] / "frontend" / "app.js"
    if not src_path.is_file():  # wheel install: frontend/ is build-only
        pytest.skip("frontend source not present")
    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(src_path.read_text(encoding="utf-8").splitlines(), 1)
        if 'onclick="GB.' in line and "JSON.stringify" in line
    ]
    assert not offenders, "double quotes inside an inline handler:\n" + "\n".join(offenders)


def test_relative_dashboard_url(env):
    r = start(env["client"])
    assert r.status_code == 201
    assert r.get_json()["dashboard_url"].startswith("/dashboard/")
    assert "127.0.0.1" not in r.get_json()["dashboard_url"]
    assert "http" not in r.get_json()["dashboard_url"]


# ---------------------------------------------------------------------------
# 2. Start / naming / key handling (guardrails #10, #11)
# ---------------------------------------------------------------------------
def test_start_returns_run_id_title_slug_and_no_secret_in_argv(env):
    r = start(env["client"])
    assert r.status_code == 201
    body = r.get_json()
    run_id = body["run_id"]
    assert body["ok"] is True
    assert body["title"] and body["slug"] and body["task_id"] == "chat"
    assert "c32" in body["title"] or "32" in body["title"]
    assert "Qwen" in body["title"]
    # argv present but MUST NOT contain the key (key is env-only)
    argv = body["argv"]
    assert all(SECRET not in str(a) for a in argv)
    assert "--experiment-folder-name" in argv
    assert argv[argv.index("--experiment-folder-name") + 1] == run_id
    assert str(env["base"].resolve()) in argv  # --experiment-base-dir
    # only the child env (held in the queue job until spawn) holds the secret
    with env["queue"]._lock:
        job = env["queue"]._jobs[run_id]
        assert SECRET not in " ".join(job.argv)
        assert job.env.get("MODEL_API_KEY") == SECRET


def test_embedding_custom_batch_x_concurrency_reaches_argv(env):
    # user m02525: 批大小10 × 并发10  vs  批大小2 × 并发50 must be a REAL load,
    # i.e. the explicit concurrency must survive upstream's batch-mode forcing
    # and reach the engine as --num-concurrency N (not silently become 1).
    totals = []
    for batch, conc in ((10, 10), (2, 50)):
        r = start(env["client"], task_id="embedding", values={
            "api_base": "http://localhost:8000/v1",
            "api_model_name": "bge-m3",
            "model_tokenizer": "BAAI/bge-m3",
            "batch": batch,
            "concurrency": conc,
        })
        assert r.status_code == 201, r.get_json()
        body = r.get_json()
        argv = body["argv"]
        assert argv[argv.index("--iteration-type") + 1] == "batch_size"
        assert argv[argv.index("--batch-size") + 1] == str(batch)
        assert argv[argv.index("--num-concurrency") + 1] == str(conc)
        # readable title/slug carry both knobs so the two runs are distinguishable
        assert f"b{batch}" in body["title"] and f"c{conc}" in body["title"]
        totals.append(batch * conc)
    assert totals == [100, 100]  # same documents in flight, different splitting


def test_api_base_trailing_v1_is_not_doubled(env):
    # the OpenAI-compatible vendors append "/v1/chat/completions" to the host
    # themselves, so a vLLM-style "http://host:8000/v1" base would otherwise
    # become "/v1/v1/..."; other backends build their own URLs and keep the base.
    r = start(env["client"], task_id="chat", values={
        "api_base": "http://127.0.0.1:8101/v1",
        "api_model_name": "Qwen2.5-0.5B-Instruct",
        "model_tokenizer": "C:/models/chat",
    })
    assert r.status_code == 201, r.get_json()
    argv = r.get_json()["argv"]
    assert argv[argv.index("--api-base") + 1] == "http://127.0.0.1:8101"

    resolved = Templates.load(env["cfg"]).resolve(
        "chat", {"api_backend": "azure-openai", "api_base": "https://x.openai.azure.com/v1"}
    )
    assert resolved.engine["api_base"] == "https://x.openai.azure.com/v1"


def test_oci_backend_without_extra_is_a_clear_400(env, monkeypatch):
    # The OCI SDK is an optional extra. The child would die with a ClickException,
    # but the WebUI runs it with stdout/stderr on /dev/null, so start() has to
    # answer with the same install hint instead of a silent "child exit code".
    import importlib.util as ilu

    from genai_bench.web import api as web_api

    real_find_spec = ilu.find_spec
    monkeypatch.setattr(
        web_api.importlib.util,
        "find_spec",
        lambda name, *a, **k: None if name in ("oci", "oci-openai") else real_find_spec(name, *a, **k),
    )
    r = start(env["client"], task_id="chat", values={
        "api_backend": "oci-openai",
        "api_base": "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com",
        "api_model_name": "meta.llama-3.1-70b-instruct",
        "model_tokenizer": "Qwen/Qwen2.5-7B-Instruct",
    })
    assert r.status_code == 400, r.get_json()
    msg = r.get_json()["error"]
    assert "genai-bench[oci]" in msg and "oci-openai" in msg
    # nothing was queued or written for the rejected call
    assert not env["queue"].status()["queued"]


def test_params_json_on_disk_is_sanitized(env):
    r = start(env["client"])
    run_id = r.get_json()["run_id"]
    folder = env["base"] / run_id
    assert (folder / "params.json").is_file()
    assert (folder / "meta.json").is_file()
    assert (folder / "note.txt").is_file()
    blob = (folder / "params.json").read_text(encoding="utf-8")
    assert SECRET not in blob
    assert "api_key" not in json.loads(blob).get("values", {})
    assert (folder / "meta.json").read_text(encoding="utf-8").count(run_id) >= 1


def test_run_id_is_path_safe_primary_key(env):
    from genai_bench.web.naming import is_valid_run_id, RUN_ID_RE

    r = start(env["client"])
    run_id = r.get_json()["run_id"]
    assert is_valid_run_id(run_id)
    assert RUN_ID_RE.match(run_id)
    # invalid run_ids rejected by Store.folder (path traversal defense)
    with pytest.raises(ValueError):
        env["store"].folder("../escape")
    with pytest.raises(ValueError):
        env["store"].folder("a/b")


def test_patch_title_keeps_run_id_folder_and_dashboard(env):
    r = start(env["client"])
    run_id = r.get_json()["run_id"]
    old_dashboard = r.get_json()["dashboard_url"]
    folder = env["base"] / run_id
    assert folder.is_dir()
    patched = env["client"].patch(
        f"/api/experiments/{run_id}",
        json={"title": "我的新标题", "note": "备注", "tags": ["a"], "starred": True, "group": "g1"},
    )
    assert patched.status_code == 200
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == run_id  # unchanged
    assert meta["title"] == "我的新标题"
    assert meta["starred"] is True
    assert (folder / "note.txt").read_text(encoding="utf-8") == "备注"
    assert folder.is_dir()  # folder name unchanged
    assert env["client"].get(f"/api/experiments/{run_id}").get_json()["dashboard_url"] == old_dashboard
    assert patched.get_json()["dashboard_url"] == old_dashboard


def test_preflight_never_persists(env, monkeypatch):
    import genai_bench.web.api as api

    def fake_preflight(base, key, model):
        assert key == SECRET  # key passed in memory
        return {"ok": False, "status": "401", "message": "鉴权失败"}

    monkeypatch.setattr(api, "_preflight", fake_preflight)
    before = sorted(p.name for p in env["base"].iterdir())
    r = env["client"].post(
        "/api/preflight",
        json={"api_base": "http://x/v1", "api_model_name": "m", "secrets": {"api_key": SECRET}},
    )
    assert r.status_code == 200
    assert r.get_json()["status"] == "401"
    assert sorted(p.name for p in env["base"].iterdir()) == before  # nothing written
    assert SECRET not in r.data.decode("utf-8")


def test_preflight_real_401(monkeypatch):
    import urllib.error
    import genai_bench.web.api as api

    def raise_401(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(api.urllib.request, "urlopen", raise_401)
    result = api._preflight("http://x/v1", SECRET, "m")
    assert result["status"] == "401" and result["ok"] is False


# ---------------------------------------------------------------------------
# 3. Serial queue + stop (guardrail #9)
# ---------------------------------------------------------------------------
def test_serial_queue_one_at_a_time_and_stop_halts_batch(env):
    c = env["client"]
    ids = [start(c, group="sweep").get_json()["run_id"] for _ in range(3)]
    q = env["queue"]
    # tick once: exactly one child launched
    q._tick()
    assert q.status()["running"] == ids[0]
    assert q.status()["queued"] == ids[1:]
    assert len(env["launched"]) == 1
    # stop the running one: kill child AND cancel all queued siblings, no auto-advance
    res = c.post(f"/api/benchmark/{ids[0]}/stop")
    assert res.get_json()["ok"] is True
    assert env["launched"][ids[0]].terminated
    assert q.status()["paused"] is True
    assert q.status()["queued"] == []
    for rid in ids:
        assert q.status()["jobs"][[j["run_id"] for j in q.status()["jobs"]].index(rid)]["status"] == "stopped"
    # nothing else auto-starts after stop
    q._tick()
    assert len(env["launched"]) == 1
    assert q.status()["running"] is None
    # a NEW explicit start resumes the queue
    new_id = start(c).get_json()["run_id"]
    assert q.status()["paused"] is False
    q._tick()
    assert q.status()["running"] == new_id


def test_run_completion_marks_done_and_starts_next(env):
    c = env["client"]
    ids = [start(c).get_json()["run_id"] for _ in range(2)]
    q = env["queue"]
    advance(env, ids[0], 0)
    assert json.loads((env["base"] / ids[0] / "meta.json").read_text())["status"] == "done"
    q._tick()
    assert q.status()["running"] == ids[1]
    advance(env, ids[1], 2)
    assert json.loads((env["base"] / ids[1] / "meta.json").read_text())["status"] == "failed"


def test_multi_select_sweep_all_share_one_queue(env):
    c = env["client"]
    # multi-select = several sequential start calls into the SAME queue
    ids = [start(c, group="multi").get_json()["run_id"] for _ in ("chat",)]
    # a sweep = serial starts of the same task with different concurrency
    for conc in (8, 64, 128):
        r = start(c, values={
            "api_base": "http://localhost:8000/v1",
            "api_model_name": "Qwen2.5-7B",
            "model_tokenizer": "Qwen/Qwen2.5-7B",
            "concurrency": conc,
        }, group="sweep", sweep_key="c", sweep_values=[8, 32, 64, 128])
        ids.append(r.get_json()["run_id"])
    st = c.get("/api/benchmark/queue").get_json()
    assert len(st["queued"]) + (1 if st["running"] else 0) == 4
    assert all(j["group"] in ("sweep", "multi") for j in st["jobs"])


# ---------------------------------------------------------------------------
# 4. History / pagination / 反显 / rerun
# ---------------------------------------------------------------------------
def test_history_pagination_filter_and_cross_page_star(env):
    c = env["client"]
    ids = []
    for i in range(5):
        rid = start(c, values={
            "api_base": "http://localhost:8000/v1",
            "api_model_name": f"model-{i}",
            "model_tokenizer": "tok",
        }).get_json()["run_id"]
        ids.append(rid)
        if i == 0:
            c.patch(f"/api/experiments/{rid}", json={"starred": True, "title": "Starred One"})
    page = c.get("/api/experiments?page=1&size=20").get_json()
    assert page["total"] == 5 and page["size"] == 20
    assert page["items"][0]["run_id"] == ids[0]  # starred pinned first
    assert all("dashboard_url" in it for it in page["items"])
    assert "metrics" in page["items"][0]
    assert "has_results" in page["items"][0]
    # keyword filter
    assert c.get("/api/experiments?keyword=Starred").get_json()["total"] == 1
    # size coercion
    assert c.get("/api/experiments?size=13").get_json()["size"] == 20
    assert c.get("/api/experiments?size=50").get_json()["size"] == 50


def test_params_refill_includes_fixed_and_collapsed_values(env):
    c = env["client"]
    rid = start(c, task_id="embedding").get_json()["run_id"]
    p = c.get(f"/api/experiments/{rid}/params").get_json()
    assert p["run_id"] == rid and p["task_id"] == "embedding"
    assert p["values"]["batch"] == 128
    assert p["values"]["doc_length"] == 512
    assert p["values"]["concurrency"] == 1  # collapsed/fixed value present
    assert p["fixed"]["iteration_type"] == "batch_size"
    assert p["template_version"] == "3.0"
    assert "fields" in p and "defaults" in p and "meta" in p
    assert SECRET not in json.dumps(p)


def test_rerun_uses_snapshot_reinjects_key_new_run_id(env):
    c = env["client"]
    old = start(c).get_json()["run_id"]
    # remove any leaked key from request body to simulate re-injection from frontend
    r = c.post(f"/api/experiments/{old}/rerun", json={"secrets": {"api_key": SECRET}})
    assert r.status_code == 201
    body = r.get_json()
    assert body["rerun_of"] == old and body["run_id"] != old
    assert body["key_reinjected"] is True
    # old snapshot still intact; new folder exists
    assert (env["base"] / old / "params.json").is_file()
    assert (env["base"] / body["run_id"] / "params.json").is_file()
    assert SECRET not in (env["base"] / body["run_id"] / "params.json").read_text()
    with env["queue"]._lock:
        assert env["queue"]._jobs[body["run_id"]].env.get("MODEL_API_KEY") == SECRET
    # missing snapshot -> clear 404
    (env["base"] / old / "params.json").unlink()
    assert c.post(f"/api/experiments/{old}/rerun", json={}).status_code == 404


def test_parse_cli_reverse_mapping(env):
    c = env["client"]
    cli = ("genai-bench benchmark --task text-to-text --api-backend openai "
           "--api-base http://x/v1 --api-model-name Qwen --model-tokenizer Qwen/tok "
           "--traffic-scenario D(1024,512) --num-concurrency 64 "
           "--batch-size 1 --max-time-per-run 10")
    r = c.post("/api/params/parse-cli", json={"cli": cli}).get_json()
    assert r["ok"] is True and r["task_id"] == "chat"
    assert r["values"]["concurrency"] == 64
    assert r["values"]["length"] == "1k"


# ---------------------------------------------------------------------------
# 5. Charts / read-only metrics / export (guardrails #5, #10, #12)
# ---------------------------------------------------------------------------
def test_compare_chart_total_in_flight_axis(env):
    # 总在飞文档数 (batch x concurrency) must put 批10x并发10 and 批2x并发50 at
    # the same x, which is the whole point of offering that axis.
    c = env["client"]
    ids = []
    for batch, conc, p99 in ((10, 10, 0.9), (2, 50, 1.7)):
        rid = start(c, task_id="embedding", values={
            "api_base": "http://localhost:8000/v1",
            "api_model_name": "bge-m3",
            "model_tokenizer": "BAAI/bge-m3",
            "batch": batch,
            "concurrency": conc,
        }).get_json()["run_id"]
        write_batch_result(env["store"], rid, batch_size=batch,
                           num_concurrency=conc, p99=p99)
        ids.append(rid)
    compare = c.get(
        f"/api/experiments/chart?ids={','.join(ids)}&metric=p99&x=concurrency_x_batch"
    ).get_json()
    assert compare["mode"] == "compare"
    assert compare["x_field"] == "concurrency_x_batch"
    assert [p["x"] for p in compare["points"]] == [100, 100]
    # the y values stay distinct: same total pressure, different P99
    assert sorted(p["y"] for p in compare["points"]) == [0.9, 1.7]


def test_chart_single_and_compare_read_only(env):
    c = env["client"]
    ids = []
    for conc, p99 in ((8, 0.5), (32, 0.9), (64, 1.8)):
        rid = start(c, values={
            "api_base": "http://localhost:8000/v1",
            "api_model_name": "Qwen",
            "model_tokenizer": "tok",
            "concurrency": conc,
        }).get_json()["run_id"]
        write_result(env["store"], rid, num_concurrency=conc, p99=p99)
        ids.append(rid)
    single = c.get(f"/api/experiments/chart?ids={ids[0]}&metric=p99").get_json()
    assert single["mode"] == "single"
    assert len(single["series"][0]["points"]) > 0
    compare = c.get(f"/api/experiments/chart?ids={','.join(ids)}&metric=p99&x=num_concurrency").get_json()
    assert compare["mode"] == "compare" and len(compare["points"]) == 3
    assert [p["x"] for p in compare["points"]] == [8, 32, 64]
    # history shows key metrics without recomputation
    row = c.get("/api/experiments").get_json()["items"][0]
    assert row["has_results"] is True
    assert row["metrics"]["p99"] is not None


def test_png_chart_offline(env):
    from genai_bench.web.charts import render_line_chart

    png = render_line_chart([(1, 2), (2, 4), (3, 3)], title="P99", y_label="s")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 100


def test_export_zip_no_secret_original_result_names(env):
    c = env["client"]
    rid = start(c).get_json()["run_id"]
    original_name = write_result(env["store"], rid)
    r = c.get(f"/api/experiments/export?ids={rid}&format=zip")
    assert r.status_code == 200
    assert r.mimetype == "application/zip"
    assert "attachment" in r.headers["Content-Disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = z.namelist()
    assert "README.txt" in names and "summary.csv" in names
    assert any(n.endswith(f"results/{original_name}") for n in names)  # original name unchanged
    assert any(n.endswith("charts/p99.png") for n in names)
    blob = b"".join(z.read(n) for n in names if n.endswith((".txt", ".csv", ".json", ".png")))
    assert SECRET.encode() not in blob  # 导出包搜不到 API Key
    assert b"run_id" in blob


def test_export_header_is_latin1_safe_with_utf8_filename(env):
    """The multi-run name carries a Chinese ``条``; HTTP headers are latin-1 only,
    so a raw UTF-8 header makes gevent's WSGI server raise
    ``UnicodeError: Non-latin1 header`` and the whole ZIP download 500s.
    """
    from genai_bench.web.api import content_disposition

    header = content_disposition("genai-bench_3条_20261107-1420.zip")
    header.encode("latin-1")  # must not raise
    assert 'filename="genai-bench_3_20261107-1420.zip"' in header
    assert "filename*=UTF-8''genai-bench_3%E6%9D%A1_20261107-1420.zip" in header

    c = env["client"]
    ids = ",".join(start(c).get_json()["run_id"] for _ in range(2))
    r = c.get(f"/api/experiments/export?ids={ids}&format=zip")
    assert r.status_code == 200
    assert "filename*=UTF-8''" in r.headers["Content-Disposition"]
    r.headers["Content-Disposition"].encode("latin-1")  # header must stay ASCII


def test_export_csv_and_all_filter(env):
    c = env["client"]
    for i in range(3):
        rid = start(c, values={
            "api_base": "http://localhost:8000/v1",
            "api_model_name": f"m{i}",
            "model_tokenizer": "tok",
        }, group="batch").get_json()["run_id"]
        write_result(env["store"], rid)
    r = c.get("/api/experiments/export?all=1&filter=%7B%22group%22%3A%22batch%22%7D&format=csv")
    assert r.status_code == 200 and "text/csv" in r.mimetype
    text = r.data.decode("utf-8-sig")
    assert "run_id" in text and text.count("\n") >= 4
    assert SECRET not in text


# ---------------------------------------------------------------------------
# 6. Template file drives form AND naming (guardrail #6)
# ---------------------------------------------------------------------------
def test_template_file_change_changes_form_and_naming(env, tmp_path):
    from genai_bench.web.templates import Templates

    c = env["client"]
    # custom template: new title template + changed default concurrency
    cfg = env["cfg"]
    data = json.loads(Path(cfg.templates_file).read_text(encoding="utf-8"))
    data["naming"]["title_template"] = "CUSTOM::{model_short}::{key_params}"
    for t in data["tasks"]:
        if t["id"] == "chat":
            for f in t["fields"]:
                if f["key"] == "concurrency":
                    f["default"] = 128
    custom = tmp_path / "custom_templates.json"
    custom.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cfg2 = WebConfig(base_dir=tmp_path / "exp2", templates_path=custom)
    templates = Templates.load(cfg2)
    assert templates.public()["naming"]["title_template"] == "CUSTOM::{model_short}::{key_params}"
    chat = next(t for t in templates.public()["tasks"] if t["id"] == "chat")
    assert next(f for f in chat["fields"] if f["key"] == "concurrency")["default"] == 128
    from genai_bench.web.naming import make_title
    from datetime import datetime, timezone

    resolved = templates.resolve("chat", {"api_model_name": "M", "model_tokenizer": "T"})
    title = make_title(templates, resolved, datetime.now(timezone.utc))
    assert title.startswith("CUSTOM::M::")
    assert "c128" in title  # new default shows up in name


# ---------------------------------------------------------------------------
# 9. Tokenizer upload / select / offline resolution (user m00672)
# ---------------------------------------------------------------------------
def _upload_tokenizer(client, name="my-tok", filenames=("tokenizer.json", "tokenizer_config.json")):
    files = [(io.BytesIO(b'{"version":"1.0"}'), fn) for fn in filenames]
    return client.post(
        "/api/tokenizers",
        data={"name": name, "files": files},
        content_type="multipart/form-data",
    )


def test_tokenizer_upload_list_select_delete(env):
    c = env["client"]
    # /api/templates exposes the live (initially empty) tokenizer list
    assert c.get("/api/templates").get_json()["tokenizers"] == []
    assert c.get("/api/tokenizers").get_json()["items"] == []

    r = _upload_tokenizer(c)
    assert r.status_code == 201, r.get_data(as_text=True)
    item = r.get_json()["item"]
    assert item["ready"] is True and item["file_count"] == 2
    tid = item["id"]

    listed = c.get("/api/tokenizers").get_json()["items"]
    assert [x["id"] for x in listed] == [tid]
    # _tokenizers/ is NOT an experiment (run_id regex rejects it)
    assert c.get("/api/experiments").get_json()["total"] == 0

    # a portable tokenizer:<id> form value resolves to an ABSOLUTE dir path
    # in the child argv, while params.json keeps the portable reference.
    resp = c.post("/api/benchmark/start", json={
        "task_id": "chat",
        "values": {
            "api_base": "http://localhost:8000/v1",
            "api_model_name": "Qwen2.5-7B-Instruct",
            "model_tokenizer": "tokenizer:" + tid,
        },
        "secrets": {"api_key": "sk-not-a-real-key"},
    })
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    argv = body["argv"]
    mt = argv[argv.index("--model-tokenizer") + 1]
    assert mt != "tokenizer:" + tid
    assert Path(mt).is_absolute() and Path(mt).is_dir()
    assert (Path(mt) / "tokenizer.json").is_file()
    params = env["store"].read_params(body["run_id"])
    assert params["values"]["model_tokenizer"] == "tokenizer:" + tid  # portable ref kept

    # rerun re-resolves the same portable ref (no absolute path persisted)
    rr = c.post(f"/api/experiments/{body['run_id']}/rerun", json={"secrets": {"api_key": "x"}})
    assert rr.status_code == 201, rr.get_data(as_text=True)
    # rerun response intentionally omits argv; assert it succeeded + queued a 2nd run
    assert rr.get_json()["ok"] is True
    assert rr.get_json()["rerun_of"] == body["run_id"]

    # deleting is reflected everywhere; a missing ref is a clear error
    assert c.delete(f"/api/tokenizers/{tid}").get_json()["deleted"] is True
    assert c.get("/api/tokenizers").get_json()["items"] == []
    bad = c.post("/api/benchmark/start", json={
        "task_id": "chat",
        "values": {"api_base": "x", "api_model_name": "m", "model_tokenizer": "tokenizer:" + tid},
    })
    assert bad.status_code == 400 and "不存在" in bad.get_json()["error"]


def test_tokenizer_upload_rejects_bad_names_and_no_files(env):
    c = env["client"]
    assert c.post("/api/tokenizers", data={"name": "x"},
                  content_type="multipart/form-data").status_code == 400
    traversal = c.post("/api/tokenizers", data={
        "name": "x", "files": [(io.BytesIO(b"x"), "../../evil.json")],
    }, content_type="multipart/form-data")
    assert traversal.status_code == 400
    assert c.get("/api/tokenizers").get_json()["items"] == []


def test_common_field_options_are_objects(env):
    # fixes the latent 'undefined' select bug: api_backend options must be
    # {label,value} dicts, not raw strings
    payload = env["client"].get("/api/templates").get_json()
    chat = next(t for t in payload["tasks"] if t["id"] == "chat")
    backend = next(f for f in chat["common_fields"] if f["key"] == "api_backend")
    assert backend["options"] and isinstance(backend["options"][0], dict)
    assert backend["options"][0]["value"] == "openai"
    assert backend["options"][0]["label"]


# ---------------------------------------------------------------------------
# Purpose-named stress presets + fine ladder + 中断本批 (m01150/m01158/m01161)
# ---------------------------------------------------------------------------
def test_presets_and_ladders_exposed_and_resolvable(env):
    payload = env["client"].get("/api/templates").get_json()
    assert payload["version"] == "3.0"  # bump must not break naming/params tests
    presets = payload["presets"]
    ids = [p["id"] for p in presets]
    assert ids == ["capacity", "latency-knee", "max-throughput",
                   "standard", "soak", "long-context", "extreme"]
    assert next(p for p in presets if p.get("default"))["id"] == "capacity"
    series = [p for p in presets if p["kind"] == "series"]
    assert len(series) == 3 and all(p.get("ladder") == "full" for p in series)
    assert all("when" in p and "see" in p and "what" in p for p in presets)
    ladders = payload["ladders"]["full"]
    # dense ladder: chat concurrency, emb/rerank batch as x axis
    assert ladders["chat"]["x"] == "num_concurrency"
    assert ladders["embedding"]["x"] == "batch_size"
    assert ladders["rerank"]["x"] == "batch_size"
    assert ladders["chat"]["values"][0] == 4 and ladders["chat"]["values"][-1] == 256

    from genai_bench.web.templates import Templates
    t = Templates.load(env["cfg"])
    for p in presets:
        for tid in ("chat", "embedding", "rerank"):
            if p["kind"] == "series":
                spec = ladders[tid]
                for v in (spec["values"][0], spec["values"][-1]):
                    r = t.resolve(tid, {spec["field"]: v})
                    assert r.scenario, (p["id"], tid, v)
            else:
                r = t.resolve(tid, p["set"][tid])
                assert r.scenario, (p["id"], tid)


def test_dense_ladder_options_are_legal(env):
    payload = env["client"].get("/api/templates").get_json()
    def opts(tid, key):
        tk = next(t for t in payload["tasks"] if t["id"] == tid)
        return [o["value"] for o in next(f for f in tk["fields"] if f["key"] == key)["options"]]
    assert 4 in opts("chat", "concurrency") and 256 in opts("chat", "concurrency")
    assert "8k" in opts("chat", "length")
    assert 8 in opts("embedding", "batch") and 1024 in opts("embedding", "batch")
    assert 4096 in opts("embedding", "doc_length")
    assert "2048_256" in opts("rerank", "length")
    assert 512 in opts("rerank", "batch")


def test_series_preset_whole_group_enqueued_once_and_interrupt_halts_batch(env):
    """Frontend posts the WHOLE ladder in one shot (same group), then 中断本批
    must stop the running level and cancel EVERY remaining level, with no
    auto-continue. Already-finished levels keep their results."""
    c = env["client"]
    from genai_bench.web.templates import Templates
    t = Templates.load(env["cfg"])
    spec = t.public()["ladders"]["full"]["chat"]
    group = "preset-capacity-1234"
    ids = []
    for v in spec["values"]:
        r = start(c, values={
            "api_base": "http://localhost:8000/v1",
            "api_model_name": "Qwen2.5-7B",
            "model_tokenizer": "Qwen/Qwen2.5-7B",
            "concurrency": v,
        }, group=group, sweep_key=spec["field"], sweep_values=spec["values"])
        assert r.status_code == 201, r.get_json()
        ids.append(r.get_json()["run_id"])
    q = env["queue"]
    q._tick()
    assert q.status()["running"] == ids[0]
    assert q.status()["queued"] == ids[1:]
    assert len(env["launched"]) == 1

    # finish the FIRST level: its results must survive the interruption
    write_result(env["store"], ids[0], num_concurrency=spec["values"][0])
    advance_finished = None
    import time
    env["launched"][ids[0]].finish(0)
    q._tick()  # harvest done
    q._tick()  # start next
    assert q.status()["running"] == ids[1]

    # 中断本批: stop the currently running level
    res = c.post(f"/api/benchmark/{ids[1]}/stop")
    assert res.get_json()["ok"] is True
    assert env["launched"][ids[1]].terminated
    assert q.status()["paused"] is True
    assert q.status()["queued"] == []
    st = {j["run_id"]: j["status"] for j in q.status()["jobs"]}
    assert st[ids[0]] == "done"                 # finished level kept
    assert st[ids[1]] == "stopped"
    assert all(st[i] == "stopped" for i in ids[2:])  # every remaining level cancelled
    # result of the finished level still readable, chart still works
    assert c.get(f"/api/experiments/{ids[0]}/params").status_code == 200
    chart = c.get(f"/api/experiments/chart?ids={ids[0]}&metric=p99").get_json()
    assert chart["mode"] == "single"
    # nothing auto-starts afterwards
    q._tick()
    assert q.status()["running"] is None
    assert len(env["launched"]) == 2
    # group filter finds the whole batch (starred pinned etc. still fine)
    listing = c.get(f"/api/experiments?group={group}&size=100").get_json()
    assert listing["total"] == len(ids)


def test_interrupt_works_when_batch_is_still_all_queued(env):
    """Regression: right after a one-shot series enqueue, no level has been
    picked up by the worker yet. 中断本批 must still halt EVERY queued level
    (stopping a queued job is accepted) instead of saying 'nothing running'
    and letting the batch auto-start afterwards."""
    c = env["client"]
    group = "preset-queued-only"
    ids = [start(c, group=group).get_json()["run_id"] for _ in range(4)]
    q = env["queue"]
    st = q.status()
    assert st["running"] is None and st["queued"] == ids  # nothing launched yet
    # interrupt targets the first queued level of this batch
    res = c.post(f"/api/benchmark/{ids[0]}/stop")
    assert res.get_json()["ok"] is True
    assert q.status()["paused"] is True
    assert q.status()["queued"] == []
    statuses = {j["run_id"]: j["status"] for j in q.status()["jobs"]}
    assert all(statuses[i] == "stopped" for i in ids)
    q._tick()  # worker must not auto-start anything
    assert q.status()["running"] is None
    assert env["launched"] == {}


# ---------------------------------------------------------------------------
# 小白可读 / 套餐只填表不自动跑 / 单选 / 原始结果批量下载删除 (m02201)
# ---------------------------------------------------------------------------
def test_glossary_exposed_and_every_field_has_plain_explanation(env):
    """四项解释全部来自单一后端配置：名词表、常见组合、套餐组合说明、
    怎么读曲线；且每个表单字段都带『大白话』提示。"""
    payload = env["client"].get("/api/templates").get_json()
    g = payload["glossary"]
    assert g["intro"] and g["field_plain"]
    assert len(g["terms"]) >= 8 and len(g["combos"]) >= 5
    assert len(g["preset_combo"]) == len(payload["presets"])
    rc = g["read_chart"]
    assert rc["intro"] and rc["points"] and rc["hint"]
    for tk in payload["tasks"]:
        for f in tk["fields"] + tk["common_fields"]:
            assert f["plain"], (tk["id"], f["key"])  # 每个参数都有大白话
    # rerank 的 length 与 chat 同名, 必须有自己的专属解释(不能串成对话长度)
    rr = next(t for t in payload["tasks"] if t["id"] == "rerank")
    rr_len = next(f for f in rr["fields"] if f["key"] == "length")
    assert "Rerank" in rr_len["plain"] and "文档" in rr_len["plain"]


def test_preset_is_reference_only_backend_has_no_autorun_endpoint(env):
    """套餐不自动跑：后端没有任何『按套餐 id 直接启动』的接口；启动仍只有
    原来的 POST /api/benchmark/start（护栏#7），且非法套餐 id 不会影响它。"""
    c = env["client"]
    # 没有 /api/presets/<id>/run 之类的自动启动路由
    assert c.post("/api/presets/capacity/run").status_code == 404
    assert c.post("/api/preset/standard/start").status_code == 404
    # 启动接口本身仍然可用(套餐只是前端把参考值填进这个接口的 values)
    r = c.post("/api/benchmark/start", json={
        "task_id": "chat",
        "values": {"api_base": "x", "api_model_name": "m",
                   "model_tokenizer": "Qwen/Qwen2.5-7B", "concurrency": 32},
    })
    assert r.status_code == 201


def test_original_results_kept_and_batch_export_delete(env):
    """跑完的原始结果文件原样保留；批量 ZIP 内含原名原始结果 + 汇总表 +
    README；批量删除逐条 DELETE（运行中/排队中返回 409 保护）。"""
    c = env["client"]
    r = start(c)
    rid = r.get_json()["run_id"]
    advance(env, rid, 0)  # 让这一档跑完(done)，删除才不会被 409 保护拦下
    write_result(env["store"], rid, num_concurrency=32)
    # 引擎原始结果文件保持原名
    files = [p.name for p in env["store"].result_files(rid)]
    assert files and all("time_" in f and f.endswith("s.json") for f in files)
    # 批量 ZIP：原始结果原名 + summary.csv + README.txt
    resp = c.get(f"/api/experiments/export?ids={rid}&format=zip")
    assert resp.status_code == 200
    import io as _io, zipfile
    z = zipfile.ZipFile(_io.BytesIO(resp.data))
    names = z.namelist()
    assert any(n == "summary.csv" for n in names)
    assert any(n == "README.txt" for n in names)
    assert any(n.endswith(files[0]) for n in names)  # 原名未改
    assert b"sk-" not in resp.data  # 导出包里没有 API Key
    # 批量删除逐条可用；删除后该实验不再出现
    assert c.delete(f"/api/experiments/{rid}").get_json().get("deleted") is True
    assert c.get(f"/api/experiments/{rid}").status_code == 404

