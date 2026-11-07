"""Translate a resolved WebUI form into the real genai-bench CLI argv + env.

Guardrail #5: launching reuses the existing CLI entry as a subprocess (the
*formal* shape, not a temporary bridge). Guardrail #10: the API key is NEVER
placed in argv (so it cannot leak into experiment_metadata.json's cmd_line or
logs); it is injected only into the child's whitelisted environment.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from genai_bench.web.config import PROJECT_ROOT, WebConfig
from genai_bench.web.templates import Resolved

# Environment variables the child is allowed to inherit. Everything else is
# dropped so a secret can never slip through from the parent's environment.
ENV_WHITELIST = (
    "PATH",
    "SYSTEMROOT",
    "PYTHONPATH",
    "PYTHONIOENCODING",
    "ENABLE_UI",
    "GENAI_BENCH_LOGGING_LEVEL",
    "HF_HOME",
    "TRANSFORMERS_CACHE",
    "MODEL_API_KEY",
    "OPENAI_API_KEY",
    "HF_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
    "HOME",
)

# Secret form keys -> the env var the upstream CLI reads (see cli/validation.py
# validate_api_key and option_groups.py --model-api-key envvar="MODEL_API_KEY").
SECRET_ENV_MAP: dict[str, str] = {
    "api_key": "MODEL_API_KEY",
    "model_api_key": "MODEL_API_KEY",
    "api_key_env": "OPENAI_API_KEY",
    "azure_ad_token": "AZURE_AD_TOKEN",
    "hf_token": "HF_TOKEN",
}

# Fields that must never be persisted or returned (defense in depth).
SENSITIVE_KEYS = frozenset(SECRET_ENV_MAP) | {
    "api_key",
    "model_api_key",
    "azure_ad_token",
    "aws_secret_access_key",
    "aws_session_token",
    "github_token",
}


def build_argv(config: WebConfig, resolved: Resolved, run_id: str) -> list[str]:
    """Build the full, prompt-free argv for one serial benchmark run."""
    engine = resolved.engine
    argv: list[str] = [
        sys.executable,
        "-m",
        "genai_bench.cli.cli",
        "benchmark",
        # -- required / prompt options (must all be present so the child never
        # blocks on an interactive prompt in a headless subprocess) --
        "--api-backend",
        str(engine["api_backend"]),
        "--api-base",
        str(engine["api_base"]),
        "--api-model-name",
        str(engine["api_model_name"]),
        "--task",
        str(engine["task"]),
        "--model-tokenizer",
        str(engine["model_tokenizer"]),
        "--max-requests-per-run",
        str(engine["max_requests_per_run"]),
        "--max-time-per-run",
        str(engine["max_time_per_run"]),
        # -- where results / logs go (run_id is the primary key = folder name) --
        "--experiment-base-dir",
        str(config.base_dir.resolve()),
        "--experiment-folder-name",
        run_id,
        "--log-dir",
        str((config.base_dir / run_id).resolve()),
        # -- one iteration point per run (sweep = several serial starts) --
        "--iteration-type",
        str(engine["iteration_type"]),
    ]
    for scenario in engine.get("traffic_scenario", []):
        argv += ["--traffic-scenario", str(scenario)]
    for value in engine.get("num_concurrency", []):
        argv += ["--num-concurrency", str(value)]
    for value in engine.get("batch_size", []):
        argv += ["--batch-size", str(value)]
    if engine.get("model"):
        argv += ["--model", str(engine["model"])]

    # Optional advanced flags (config-declared, only when the user set them).
    advanced_cli = {
        "spawn_rate": "--spawn-rate",
        "warmup_ratio": "--warmup-ratio",
        "metrics_time_unit": "--metrics-time-unit",
        "server_engine": "--server-engine",
        "server_version": "--server-version",
        "server_gpu_type": "--server-gpu-type",
        "server_gpu_count": "--server-gpu-count",
    }
    for key, flag in advanced_cli.items():
        value = engine.get(key)
        if value not in (None, ""):
            argv += [flag, str(value)]
    return argv


def build_child_env(secrets: dict[str, str] | None = None) -> dict[str, str]:
    """Whitelist-only child env; secrets go in as env vars, never as argv."""
    env: dict[str, str] = {}
    for name in ENV_WHITELIST:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env["ENABLE_UI"] = "false"  # headless: no Rich terminal Live panel
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    for form_key, value in (secrets or {}).items():
        env_name = SECRET_ENV_MAP.get(form_key)
        if env_name and value:
            env[env_name] = value
    return env


def sanitize(obj: Any) -> Any:
    """Recursively strip secret keys from a params-like structure (脱敏)."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items() if k not in SENSITIVE_KEYS}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


def validate(resolved: Resolved) -> list[str]:
    """Return a list of validation errors (empty = valid)."""
    errors: list[str] = []
    engine = resolved.engine
    required = [
        "api_backend",
        "api_base",
        "api_model_name",
        "model_tokenizer",
        "task",
    ]
    for key in required:
        if not engine.get(key):
            errors.append(f"缺少必填项 {key}")
    if not resolved.scenario:
        errors.append("未能解析出合法的 traffic_scenario(场景串)")
    try:
        if int(engine["max_time_per_run"]) < 1:
            errors.append("时长必须 >= 1 分钟")
    except (TypeError, ValueError, KeyError):
        errors.append("时长非法")
    return errors
