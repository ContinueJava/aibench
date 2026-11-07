# genai-bench WebUI — optimized design (v2.1)

This is an **optimization of** `genai-bench-webui-技术方案.md` (v2, the
single-image / single-process design). The original architecture is sound and is
kept. This document records only the deltas that were necessary to make it
actually runnable against this checkout, plus the acceptance criteria.

## 0. Verdict on the original plan

**Reasonable.** The five decisions D1–D5 hold up against the source:

- D1 — confirmed: `genai_bench/ui/dashboard.py` is a Rich **terminal** panel
  inside the `benchmark` process; there is no HTTP route, no port, no "view by
  run id". A read-only Web monitor is a genuine gap.
- D2 — confirmed viable: `import genai_bench` monkey-patches gevent, but a Flask
  app in a background thread + `subprocess.Popen` both work under the patch
  (probed). Single process, single port, `subprocess` calling the existing CLI,
  `--num-workers` omitted → all fine.
- D4 — correct and important: templates / schema / CLI preview are all served by
  the backend; the frontend hard-codes zero flags.
- D5 — correct: default bind is `127.0.0.1`; the container CMD passes
  `--host 0.0.0.0` and prints a warning.
- "No DB, state = experiment folder + localStorage", "don't touch the engine /
  sampling / metrics", "409 when a run is already active" — all kept.

Four things in the original plan would have **failed at runtime** and are
corrected below; `docs/upstream-contract.md` is the verified fact base.

## 1. Revision: D3 (secrets via env) needed an upstream patch

The plan says "API Key goes into the child's environment, never argv / snapshot /
logs". Two problems found by running the CLI:

1. `validate_api_key` only looked at `--api-key`; with only `MODEL_API_KEY` set,
   the CLI exited `2` with `API key is required for openai backend`.
2. `cli.py` reconstructs `cmd_line` from `ctx.params` (writing it into
   `experiment_metadata.json`) and logs every param — so even the D3 design
   would still leak through other paths once a key reached the CLI.

Fix (already applied in this branch, see `upstream-contract.md` §5):
`validate_api_key` now accepts `MODEL_API_KEY` / `OPENAI_API_KEY` as satisfying
the requirement and returns `None`; `SENSITIVE_PARAMS` masks 13 credential keys
in both the log block and the persisted `cmd_line`. The WebUI additionally:
never writes the key into `run.json`, never returns it from any API, filters the
`run.log` tail, and injects `--log-dir` so logs stay inside the experiment
folder.

## 2. Revision: Rerank template backend

The plan's §7.4 Rerank template inherits the global `openai` default. But
`validate_task` restricts `text-to-rerank` to `{vllm, sglang}`
(`validation.py:363-364`); a Rerank run against `openai` fails with exit 2.
**The Rerank template now defaults `api_backend` to `vllm`** (and offers
`sglang`), and never offers `openai` for that task. Also documented: upstream
`validate_iteration_params` forces `batch_size` iteration + `num_concurrency=[1]`
for both embeddings and rerank, so the "concurrency" field is read-only 1 there —
matching the plan's intent, but now stated as a constraint rather than a choice.

## 3. Revision: zero-build frontend instead of Vite/React

The plan's §6.1 permits "any pure-static output with relative `/api` calls".
This machine has **no Node/npm**, and the hard requirement is that the final
image contains no Node and no `frontend/` source. Building a Vite bundle here is
impossible and would add a build stage whose only job is to produce files we can
write directly.

Decision: hand-written `genai_bench/web/static/{index.html,app.css,app.js}` —
no build step, no `frontend/` directory, no npm. The dynamic form is still
100% driven by `/api/templates` and `/api/cli-schema`, so acceptance criterion
"edit the template → the UI changes" still holds. Consequence: the 3-stage
Dockerfile collapses to a **single stage** (`python:3.11-slim`), which is
smaller and satisfies "no Node in the final image" trivially. The existing root
`Dockerfile` (benchmark image) is left untouched; the WebUI image is a separate
`Dockerfile.webui`.

This is the one deliberate deviation from §6.1/§10 and is called out here.

## 4. Revision: an upstream regression had to be fixed first

An earlier fix in this session added `%(asctime)s` to the panel formatter while
the shared `date_format` still contained `%f` — illegal for `time.strftime` on
Windows → `ValueError: Invalid format string` on the very first log record, so
**no benchmark subprocess could start at all**. Fixed via a portable
`DATE_FORMAT = "%Y-%m-%d %H:%M:%S"` used by all four handlers
(`upstream-contract.md` §8). Not an upstream bug: a regression from this
session's earlier work.

## 5. Additions the plan implied but did not state

- Child env: `ENABLE_UI=false` (else Rich Live draws to a non-TTY),
  `--log-dir <experiment folder>`, and an explicit whitelist of env vars
  (incl. `HF_TOKEN` for private tokenizers). Never dump the parent env.
- Always send `--traffic-scenario` and iteration values explicitly; otherwise
  `expected_runs` diverges from upstream defaults.
- `max_time_per_run` is **minutes** in argv but seconds in metadata.
- `server_gpu_count` must be a string in hand-written metadata.
- `save()` may never write a result file (empty run) — the monitor must show
  "waiting / no results" instead of erroring.
- `run_id` validation: `^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$`, used for path
  traversal defense on every `/api/experiments/{run_id}` handler.
- Static-file serving resolves against the static root and rejects `..`.

## 6. Architecture (unchanged from v2 except §3)

```
genai_bench/web/
    __init__.py        create_app() factory + SPA/404 routing
    config.py          WebConfig (base dir, templates path, host/port, limits)
    api.py             /api/* blueprint (all 11 endpoints)
    mapping.py         the ONE params→argv mapper (start/preview/rerun share it)
    runner.py          subprocess lifecycle + run.json + status monitor thread
    metrics.py         read-only experiment folder reader (no recomputation)
    cli.py             `webui` click command (gevent.pywsgi.WSGIServer)
    templates/         simple_templates.json, cli_schema.json
    static/            index.html, app.css, app.js  (zero build)
docs/upstream-contract.md, docs/webui-design.md
Dockerfile.webui
tests/web/
```

State machine: `running → completed | failed | unknown`, driven by a monitor
thread polling `Popen.poll()` and writing `run.json`. History = the list of
experiment folders. Rerun = re-read sanitized `run.json` params and start a new
run id. No DB.

## 7. API (unchanged contract)

`GET /api/health`, `GET /api/templates`, `GET /api/cli-schema`,
`POST /api/benchmark/preview`, `POST /api/benchmark/start`,
`GET /api/experiments?limit=50`, `GET /api/experiments/{id}`,
`GET /api/experiments/{id}/metrics`, `POST /api/experiments/{id}/rerun`,
`GET /api/experiments/{id}/logs`, JSON 404 for unmatched `/api/*`, SPA fallback
for everything else. Uniform error `{"error":{"code","message","details"}}`.

## 8. Acceptance (from §13, plus the new constraints)

1. `genai-bench webui --help` works; `--host/--port/--experiment-base-dir` exist.
2. `python -m genai_bench.cli.cli webui --port N` serves the SPA on one port;
   no second server, no Node.
3. Templates/schema come from the backend; editing `simple_templates.json`
   changes the form without touching JS.
4. Simple mode: chat / embeddings / rerank each produce a valid preview and a
   valid argv; rerank defaults to `vllm`.
5. Secrets never appear in argv, `run.json`, `experiment_metadata.json`, any API
   response, or the log tail.
6. Start → 201 with `run_id`; a second concurrent start → 409.
7. Monitor page polls and renders `aggregated_metrics` without recomputing.
8. History lists folders descending; rerun uses sanitized params.
9. Path traversal in `{run_id}` and in static paths is rejected.
10. `python -m build` wheel contains `static/` and `templates/`; `ruff check`
    passes; `tests/web` passes.
11. A real `benchmark` subprocess gets past logging (no `%f` crash) with
    `ENABLE_UI=false`.
