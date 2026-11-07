"""Export packaging: zip / summary CSV (guardrails #5, #10, #11, #12).

* Engine result files are copied with their ORIGINAL names, never renamed.
* ``params.json`` and ``meta.json`` inside the package are sanitized (no API
  key plaintext, ever). The key lives only in child env during a live run.
* Readable per-run directories are named by ``slug`` (decoration); ``run_id``
  stays the primary key inside meta.json / summary.csv.
* Charts are rendered by the pure-Python :mod:`genai_bench.web.charts` ->
  no CDN, no Node, no matplotlib, works offline in the final image.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

from genai_bench.web import mapping, metrics, naming
from genai_bench.web.charts import render_from_chart_payload
from genai_bench.web.store import Store
from genai_bench.web.templates import Templates

_SUMMARY_COLUMNS = [
    "run_id",
    "title",
    "task",
    "model",
    "start",
    "status",
    "scenario",
    "num_concurrency",
    "batch_size",
    "throughput_req_s",
    "tokens_throughput_tok_s",
    "p50_s",
    "p99_s",
    "ttft_p50_s",
    "ttft_p99_s",
    "error_rate",
    "group",
    "tags",
]

_METRIC_COLUMNS = {
    "throughput_req_s": "throughput",
    "tokens_throughput_tok_s": "tokens_throughput",
    "p50_s": "p50",
    "p99_s": "p99",
    "ttft_p50_s": "ttft_p50",
    "ttft_p99_s": "ttft_p99",
    "error_rate": "error_rate",
}


def _rows(store: Store, templates: Templates, run_ids: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run_id in run_ids:
        s = metrics.summary(store, templates, run_id)
        meta = store.read_meta(run_id)
        row: dict[str, Any] = {
            "run_id": run_id,
            "title": s.get("title", run_id),
            "task": s.get("task", ""),
            "model": s.get("model", ""),
            "start": s.get("start", ""),
            "status": s.get("status", ""),
            "scenario": s.get("scenario", ""),
            "num_concurrency": s.get("num_concurrency", ""),
            "batch_size": s.get("batch_size", ""),
            "group": meta.get("group", ""),
            "tags": ";".join(meta.get("tags") or []),
        }
        for col, key in _METRIC_COLUMNS.items():
            row[col] = (s.get("metrics") or {}).get(key)
        rows.append(row)
    return rows


def csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_SUMMARY_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


def _abbrev_table(templates: Templates) -> str:
    abbrevs = templates.naming.get("abbrevs", {})
    return "\n".join(f"  {k} = {v}" for k, v in abbrevs.items()) or "  (none)"


def readme_bytes(templates: Templates, run_ids: list[str], params_preview: str = "") -> bytes:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    text = (
        "genai-bench 单人版 导出包\n"
        "==========================\n\n"
        f"导出时间: {now}\n"
        f"实验条数: {len(run_ids)}\n"
        "run_id 列表:\n"
        + "".join(f"  - {r}\n" for r in run_ids)
        + "\n"
        "说明:\n"
        "  * run_id 是唯一主键, 永不变; title/slug 只是可读装饰(meta.json)。\n"
        "  * results/ 内为引擎原始结果文件, 保持原名, 未重命名/改写。\n"
        "  * params.json 已脱敏: 不含任何 API Key/令牌明文。\n"
        "  * 本包不含、也不导出任何 API Key(密钥只在单次启动的子进程环境内存中)。\n"
        "  * 图表为离线渲染的 PNG(无 CDN/无 Node/无运行时网络)。\n\n"
        "缩写表(与模板配置一致):\n"
        f"{_abbrev_table(templates)}\n\n"
        "参数快照(脱敏, 摘要):\n"
        f"{params_preview or '(见各实验目录 params.json)'}\n\n"
        "与规格/引擎的偏差:\n"
        + "".join(f"  - {d}\n" for d in templates.deviations)
    )
    return text.encode("utf-8")


def _single_chart_png(store: Store, templates: Templates, run_id: str, metric: str) -> bytes:
    payload = metrics.chart_data(store, templates, [run_id], metric=metric)
    return render_from_chart_payload(payload)


def _compare_png(store: Store, templates: Templates, run_ids: list[str], metric: str = "p99") -> bytes:
    payload = metrics.chart_data(store, templates, run_ids, metric=metric, x="auto")
    return render_from_chart_payload(payload)


def build_zip(
    store: Store,
    templates: Templates,
    run_ids: list[str],
    *,
    include_charts: bool = True,
    filename: str | None = None,
) -> tuple[bytes, str]:
    """Build the full export zip in memory. Returns (bytes, filename)."""
    rows = _rows(store, templates, run_ids)
    slugs = [store.read_meta(r).get("slug") or r for r in run_ids]
    now = datetime.now(timezone.utc)
    names = naming.export_names(templates, slugs, now)
    fname = filename or names.get("zip", "genai-bench_export.zip")

    # sanitized params preview for README
    previews = []
    for run_id in run_ids[:5]:
        p = mapping.sanitize(store.read_params(run_id))
        previews.append(f"  {run_id}: {p.get('task_id', '')} {p.get('values', {})}")
    params_preview = "\n".join(previews)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", readme_bytes(templates, run_ids, params_preview))
        zf.writestr("summary.csv", csv_bytes(rows))

        for run_id, row in zip(run_ids, rows):
            slug = store.read_meta(run_id).get("slug") or run_id
            base = f"experiments/{slug}"
            # engine originals, names unchanged
            for path in store.result_files(run_id):
                zf.write(path, f"{base}/results/{path.name}")
            # sanitized metadata
            meta = store.read_meta(run_id)
            zf.writestr(
                f"{base}/meta.json",
                json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            zf.writestr(
                f"{base}/params.json",
                json.dumps(mapping.sanitize(store.read_params(run_id)), ensure_ascii=False, indent=2).encode("utf-8"),
            )
            zf.writestr(f"{base}/note.txt", store.read_note(run_id).encode("utf-8"))
            # per-run summary (one row)
            zf.writestr(f"{base}/summary.csv", csv_bytes([row]))
            if include_charts:
                for metric, out in (
                    ("throughput", "throughput.png"),
                    ("p99", "p99.png"),
                    ("error_rate", "error_rate.png"),
                ):
                    zf.writestr(
                        f"{base}/charts/{out}",
                        _single_chart_png(store, templates, run_id, metric),
                    )

        # whole-batch compare chart
        if include_charts and len(run_ids) > 1:
            zf.writestr("compare_p99.png", _compare_png(store, templates, run_ids))
    return buf.getvalue(), fname


def build_csv(store: Store, templates: Templates, run_ids: list[str]) -> tuple[bytes, str]:
    rows = _rows(store, templates, run_ids)
    slugs = [store.read_meta(r).get("slug") or r for r in run_ids]
    names = naming.export_names(templates, slugs, datetime.now(timezone.utc))
    # multi csv -> generic name (export_names only defines single csv)
    fname = names.get("csv") or f"genai-bench_{len(run_ids)}条.csv"
    return csv_bytes(rows), fname
