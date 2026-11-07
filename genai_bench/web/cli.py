"""``genai-bench webui`` command (one command, one port, one page).

This is a thin Click command registered on the existing CLI group; Flask and
the whole WebUI are imported lazily so the benchmark/excel/plot commands keep
working even in a minimal install.
"""

from __future__ import annotations

import click

from genai_bench.web.config import WebConfig


@click.command("webui", help="启动 genai-bench 单人版管理页(单进程/单端口/单页面)")
@click.option("--host", default=None, help="监听地址(默认 0.0.0.0, env GENAI_BENCH_WEB_HOST)")
@click.option("--port", default=None, type=int, help="监听端口(默认 8000, env GENAI_BENCH_WEB_PORT)")
@click.option(
    "--experiment-base-dir",
    default=None,
    type=click.Path(),
    help="实验目录(默认 ./genai_bench_experiments, env GENAI_BENCH_EXPERIMENT_BASE_DIR)",
)
@click.option(
    "--templates",
    default=None,
    type=click.Path(),
    help="覆盖单一模板配置文件(webui_templates.json)",
)
def webui(host, port, experiment_base_dir, templates):
    """Start the WebUI server (blocking)."""
    from genai_bench.web.app import serve

    overrides: dict[str, object] = {}
    if host is not None:
        overrides["host"] = host
    if port is not None:
        overrides["port"] = port
    if experiment_base_dir is not None:
        from pathlib import Path

        overrides["base_dir"] = Path(experiment_base_dir).resolve()
    if templates is not None:
        from pathlib import Path

        overrides["templates_path"] = Path(templates).resolve()
    cfg = WebConfig.from_env(**overrides)
    serve(cfg)
