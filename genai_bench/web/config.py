"""Runtime configuration for the genai-bench WebUI (single process/port).

The WebUI is delivered as a single Flask process served by
``gevent.pywsgi.WSGIServer``. All state lives on disk in ``base_dir`` (one
folder per ``run_id``) plus in-process memory (the serial run queue).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_DEFAULT_STATIC_DIR = _PACKAGE_DIR / "static"

# The repository root (…/genai_bench/web/config.py -> …/<repo>). Added to the
# child's PYTHONPATH so ``python -m genai_bench.cli.cli`` works even when the
# package was not installed into site-packages (source checkout / tests).
PROJECT_ROOT = _PACKAGE_DIR.parent.parent

# The ONE backend template config file (guardrail #6): recommended params,
# task display names / short codes, abbreviation table and title/slug templates
# all live here. Editing it changes the form AND naming on next restart.
_TEMPLATES_FILE = "webui_templates.json"


@dataclass
class WebConfig:
    """Configuration for one WebUI process."""

    host: str = "0.0.0.0"
    port: int = 8000
    #: Path of the experiment's parent dir. The CLI subprocess is launched with
    #: this as cwd so a source checkout (no ``pip install``) still resolves
    #: ``python -m genai_bench.cli.cli``; results still land in base_dir.
    child_cwd: Path | None = None
    base_dir: Path = field(
        default_factory=lambda: Path.cwd() / "genai_bench_experiments"
    )
    templates_path: Path | None = None
    static_dir: Path = _DEFAULT_STATIC_DIR
    max_concurrent_runs: int = 1  # guardrail #9: strictly serial, always 1

    @classmethod
    def from_env(cls, **overrides: object) -> "WebConfig":
        """Build a config from environment variables, then apply overrides.

        Environment variables:
            GENAI_BENCH_WEB_HOST / GENAI_BENCH_WEB_PORT
            GENAI_BENCH_EXPERIMENT_BASE_DIR
            GENAI_BENCH_TEMPLATES  (override the single template config file)
            GENAI_BENCH_STATIC_DIR
        """
        kwargs: dict[str, object] = {}
        env_map = {
            "GENAI_BENCH_WEB_HOST": "host",
            "GENAI_BENCH_EXPERIMENT_BASE_DIR": "base_dir",
            "GENAI_BENCH_TEMPLATES": "templates_path",
            "GENAI_BENCH_STATIC_DIR": "static_dir",
            "GENAI_BENCH_CHILD_CWD": "child_cwd",
        }
        for env_name, attr in env_map.items():
            value = os.environ.get(env_name)
            if not value:
                continue
            kwargs[attr] = Path(value).resolve() if attr.endswith(("dir", "path")) else value
        port = os.environ.get("GENAI_BENCH_WEB_PORT")
        if port:
            try:
                kwargs["port"] = int(port)
            except ValueError:
                pass
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        # max_concurrent_runs is deliberately NOT overridable: the whole point
        # of the 单人版 is that exactly one benchmark runs at a time.
        return cls(**kwargs)

    @property
    def templates_file(self) -> Path:
        if self.templates_path is None:
            return _DEFAULT_TEMPLATES_DIR / _TEMPLATES_FILE
        if self.templates_path.is_dir():
            return self.templates_path / _TEMPLATES_FILE
        return self.templates_path

    def ensure_dirs(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
