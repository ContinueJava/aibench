# Upstream contract (verified against this checkout)

This document is the **single source of truth** for the WebUI's integration with
the `genai-bench` CLI. Every fact below was verified by running
`genai-bench benchmark --help` and by reading the source of this checkout
(version `0.0.5`, `pyproject.toml:3`). The WebUI MUST treat these as a contract:
when upstream changes, update this file first, then the WebUI's parameter
schema.

> Rule: the WebUI never hard-codes a flag name in its frontend. All flag names,
> task enums, and templates come from the backend (`genai_bench/web/templates/`),
> which in turn is validated against this contract.

## 1. Entry point

- Console script: `genai-bench = "genai_bench.cli.cli:cli"` (`pyproject.toml:44`).
- The WebUI launches the benchmark as a subprocess with the **module entry
  point** (more robust than relying on the console script being on PATH):
  `[sys.executable, "-m", "genai_bench.cli.cli", "benchmark", ...]`.
- Verified: `python -m genai_bench.cli.cli --help` prints
  `Commands: benchmark / excel / plot`.
- `import genai_bench` runs `monkey.patch_all()` (`genai_bench/__init__.py`) —
  the WebUI process is gevent-monkey-patched. Verified that a Flask server in a
  background thread + `subprocess.Popen` still work under the patch; the WebUI
  uses `gevent.pywsgi.WSGIServer` (gevent is already a dependency).

## 2. Required options (all must be supplied explicitly)

| Option | Type / choices | Notes |
| --- | --- | --- |
| `--api-backend` | Choice, 12 values | `openai, oci-openai, oci-cohere, oci-cohere-v2, oci-genai, cohere, aws-bedrock, azure-openai, gcp-vertex, together, vllm, sglang` (`option_groups.py:81-104`) |
| `--api-base` | str | base URL (`option_groups.py:74-79`) |
| `--api-model-name` | str | model name in request body (`option_groups.py:35-41`) |
| `--task` | Choice, 7 values | `text-to-text, text-to-image, text-to-embeddings, text-to-rerank, text-to-speech, image-text-to-text, image-to-embeddings` (`option_groups.py:43-64`) |
| `--model-tokenizer` | str | **required + prompt**; HF id or local path (`option_groups.py:630-638`) |
| `--max-time-per-run` | `IntRange(min=1)` | **required + prompt**; unit is **MINUTES**, converted to seconds at `cli.py:346` (`max_time_per_run *= 60`) |
| `--max-requests-per-run` | `IntRange(min=1)` | **required + prompt** (`option_groups.py:438-446`) |

`--api-key` is NOT required at the option level, but `validate_api_key`
(`validation.py:285-335`) requires a value for backends `openai / vllm / sglang`.
Env fallback: since the WebUI must not put secrets in argv, `validate_api_key`
was patched so that when `--api-key` is absent it accepts `MODEL_API_KEY` (or
`OPENAI_API_KEY`) from the environment and returns `None`; the real value is then
picked up by `--model-api-key` (whose `envvar="MODEL_API_KEY"`,
`option_groups.py:130-137`) and handed to the auth factory
(`cli.py:186-189`: `model_api_key or api_key`). Explicit `--api-key` still wins.

## 3. Iteration / scenario rules (must be mirrored by WebUI)

- `--iteration-type` Choice `num_concurrency | batch_size`, default
  `num_concurrency`, callback `validate_iteration_params` (`validation.py:220-261`).
- For `text-to-embeddings` AND `text-to-rerank`, the callback **forces**
  `iteration_type=batch_size`, `num_concurrency=[1]`, and
  `batch_size = batch_size or DEFAULT_BATCH_SIZES`. Any concurrency value the
  user supplies is silently overwritten.
- For every other task it **forces** `iteration_type=num_concurrency` and
  `batch_size=[1]`.
- `--num-concurrency`, `--batch-size`, `--traffic-scenario` are all `multiple=True`
  (repeat the flag once per value). `num_concurrency`/`batch_size` are `is_eager`.
- `DEFAULT_NUM_CONCURRENCIES = [1,2,4,8,16,32,64,128,256]`;
  `DEFAULT_BATCH_SIZES = [1,2,4,8,24,48,96]`.
- **The WebUI must always send `--traffic-scenario` and the iteration values
  explicitly** (never rely on defaults), otherwise `expected_runs` computed by
  the WebUI will not match the real run count.
- Scenario syntax (`option_groups.py:476-581`):
  `D(in,out)`, `N(mean,std)/(mean,std)`, `U(min,max)/(min,max)` / `U(max,max)`,
  `E(tokens_per_doc)`, `R(tokens_per_doc,tokens_per_query)`, `A(num_chars)`,
  `I(w,h[,num_images])`, or `dataset`.
- Default scenarios (only used when the flag is omitted — the WebUI never relies
  on these): chat 5, embeddings 5, re-rank 7, image 3, speech 5.
  Note an upstream inconsistency: help text says `R(2048,100)` while
  `DEFAULT_SCENARIOS_FOR_RERANK` in `validation.py:90-99` says `R(2096,100)`.

## 4. Backend/task compatibility (critical for the Rerank template)

- `validate_task` (`validation.py:338-364`) applies
  `backend_task_restrictions = {"text-to-rerank": {"vllm", "sglang"}}`: the
  Rerank task is **only** supported by `vllm` and `sglang`. Sending it with
  `openai` fails with `exit=2` and
  `Task 'text-to-rerank' is not supported by the 'openai' backend...`.
- Therefore the Rerank simple-mode template MUST default `api_backend` to
  `vllm` or `sglang`, not `openai`.

## 5. Secrets: where they can leak (and the fix)

- `cli.py:174-177` logs every `ctx.params` value ("Options you provided:") and
  `cli.py:275-285` reconstructs a `cmd_line` from `ctx.params`; that command line
  is persisted into `experiment_metadata.json` as `ExperimentMetadata.cmd`
  (`cli.py:363-390`, `protocol.py:225`).
- `auth_config` (`auth/openai/...`, `auth/azure/openai_auth.py:53-92`) does NOT
  contain plain secrets, but `cmd` and the log do.
- Fix applied: `genai_bench/utils.py` defines `SENSITIVE_PARAMS` (13 keys:
  `api_key, model_api_key, azure_ad_token, aws_secret_access_key,
  aws_session_token, security_token, storage_aws_secret_access_key,
  storage_aws_session_token, storage_azure_account_key,
  storage_azure_connection_string, storage_azure_sas_token,
  storage_gcp_credentials_path, github_token`). `cli.py` replaces those values
  with `***` in both the log block and the reconstructed `cmd_line`.
  `grep "cmd"` confirms `ExperimentMetadata.cmd` has no downstream parser, so
  masking is safe.
- **WebUI rules**: (a) never put a secret in argv — pass it via a whitelisted
  subprocess env var (`MODEL_API_KEY` etc.); (b) never persist it in `run.json`;
  (c) never return it from any API; (d) filter the tail of `run.log` in case the
  upstream CLI logs a secret directly; (e) set `ENABLE_UI=false` for the headless
  child; (f) inject `--log-dir <experiment folder>` so `genai_bench.log` is not
  dropped in the CWD (`logging.py:178-197`).
- `server_gpu_count` in `ExperimentMetadata` is `Optional[str]` — a hand-written
  metadata object must use a **string**, not an int.

## 6. Output artifacts (consumed read-only by the WebUI)

- `get_experiment_path` (`cli/utils.py:67-119`): with `--experiment-base-dir
  <base>` and `--experiment-folder-name <run_id>` the results land exactly in
  `<base>/<run_id>/`. The WebUI fixes both flags for every run.
- `experiment_metadata.json`: written once at start (`cli.py:387-390`). Read-only;
  NEVER used as a rerun source (its `cmd` string may contain a secret).
- Per-run metrics file name (`cli.py:515-523`):
  `f"{sanitized_scenario_str}_{task}_{iteration_type}_{iteration}_time_{total_run_time}s.json"`.
  `sanitize_string` (`utils.py:9-16`) turns `/` and `,` into `_` and removes
  `(`/`)` → `D(100,1000)` becomes `D100_1000`.
- Result-file regex (`analysis/experiment_loader.py:90-95`):
  `r"^.+_.+_(?:concurrency|batch_size)_\d+_time_\d+s\.json$"`.
- Metrics JSON shape (`metrics/aggregated_metrics_collector.py:402-408`):
  `{"aggregated_metrics": <dict>, "individual_request_metrics": [<dict>],
  "_time_unit": "s"|"ms"}`. The WebUI MUST NOT recompute statistics or
  percentiles — it only reads.
- `AggregatedMetrics` fields (`metrics/metrics.py:136-183`): `scenario,
  num_concurrency, batch_size, iteration_type, run_duration,
  mean_output_throughput_tokens_per_s, mean_input_throughput_tokens_per_s,
  mean_total_tokens_throughput_tokens_per_s, requests_per_second,
  error_codes_frequency, error_rate, num_error_requests,
  num_completed_requests, num_requests, stats`.
- `save()` (`aggregated_metrics_collector.py:379-389`) does **not** write a file
  when `all_request_metrics` is empty — the monitor MUST tolerate "no result
  file yet / never generated".
- `max_time_per_run` is minutes in argv but `max_time_per_run_s` in metadata
  (seconds). The WebUI's `expected_runs = len(scenarios) * len(iteration_values)`.

## 7. Environment variables the child may need (whitelist)

`MODEL_API_KEY`, `OPENAI_API_KEY`, `HF_TOKEN` (private tokenizers —
`validate_tokenizer` at `validation.py:172-217` reads `os.environ.get("HF_TOKEN")`),
plus cloud-specific `AWS_*`, `AZURE_*`, `GCP_*`, `GITHUB_*`, `OCI_*`. The WebUI
copies only an explicit whitelist from the request into the child env; it never
dumps the parent process env wholesale, and never echoes a secret back.

## 8. Known upstream regression fixed in this branch (must stay fixed)

- `logging.py` used `date_format = "%Y-%m-%d %H:%M:%S.%f"` in 4 handlers.
  `logging.Formatter.formatTime` uses `time.strftime`, which rejects `%f` on
  Windows with `ValueError: Invalid format string`. It was harmless in HEAD
  because the panel formatter lacked `%(asctime)s`; adding `%(asctime)s` in an
  earlier fix activated it. Fixed by introducing the portable constant
  `DATE_FORMAT = "%Y-%m-%d %H:%M:%S"` and using it in all four handlers.
  Without this fix no benchmark subprocess can start.
