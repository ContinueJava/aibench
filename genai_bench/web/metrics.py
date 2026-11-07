"""Read-only access to engine result JSONs (guardrail #5).

The engine already computed every aggregate and percentile. This module ONLY
reads the persisted values -- it never recomputes a statistic. The saved JSON
shape (aggregated_metrics_collector.save) is::

    {
      "aggregated_metrics": {
         "requests_per_second": ...,
         "mean_total_tokens_throughput_tokens_per_s": ...,
         "error_rate": ...,
         "num_concurrency" / "batch_size": ...,
         "scenario": ..., "run_duration": ...,
         "stats": {"e2e_latency": {"p50": .., "p99": ..}, "ttft": {...}, ...}
      },
      "individual_request_metrics": [ {RequestLevelMetrics...}, ... ],
      "_time_unit": "s" | "ms"
    }

There is NO time column in the saved data, so the single-run 时序图 is derived
read-only from the per-request ``individual_request_metrics`` sequence; the
multi-run 对比图 uses one aggregate point per experiment (x = sweep field).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from genai_bench.web.store import Store
from genai_bench.web.templates import Templates


def _dig(data: dict[str, Any], dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _load_result(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "aggregated_metrics" not in data:
        return None
    return data


def _best_aggregate(store: Store, run_id: str) -> dict[str, Any] | None:
    """Pick the result file with the most completed requests (read-only).

    A single WebUI run normally produces ONE iteration point => ONE result
    file, but the engine may write more (e.g. a sweep expressed in one CLI
    call). Choosing by num_completed_requests avoids returning an empty file.
    """
    best: dict[str, Any] | None = None
    best_count = -1
    for path in store.result_files(run_id):
        data = _load_result(path)
        if data is None:
            continue
        agg = data.get("aggregated_metrics") or {}
        count = _number(agg.get("num_completed_requests")) or 0
        if count > best_count:
            best_count = int(count)
            best = data
    return best


def metric_values(templates: Templates, agg: dict[str, Any]) -> dict[str, float | None]:
    """Extract the configured key metrics from an aggregated_metrics dict."""
    out: dict[str, float | None] = {}
    for metric in templates.metrics:
        out[metric["key"]] = _number(_dig(agg, metric["agg"]))
    return out


def summary(store: Store, templates: Templates, run_id: str) -> dict[str, Any]:
    """Key metrics + headline fields for one experiment (history row / export)."""
    meta = store.read_meta(run_id)
    data = _best_aggregate(store, run_id)
    agg = (data or {}).get("aggregated_metrics") or {}
    values = metric_values(templates, agg)
    unit = (data or {}).get("_time_unit", "s")
    return {
        "run_id": run_id,
        "title": meta.get("title", run_id),
        "slug": meta.get("slug", run_id),
        "task": meta.get("task", ""),
        "model": meta.get("model", ""),
        "start": meta.get("start", ""),
        "status": meta.get("status", "unknown"),
        "scenario": agg.get("scenario", ""),
        "num_concurrency": agg.get("num_concurrency"),
        "batch_size": agg.get("batch_size"),
        "run_duration": agg.get("run_duration"),
        "metrics": values,
        "metrics_unit": unit,
        "has_results": data is not None,
    }


def _series_from_requests(
    templates: Templates, requests: list[dict[str, Any]], metric_key: str, max_points: int = 400
) -> dict[str, Any]:
    """Read-only per-request sequence for a single-run time-series chart.

    We map each configured metric to the corresponding RequestLevelMetrics
    field. NO percentile recomputation: for p50/p99-style metrics we plot the
    raw per-request latency (labelled as such), while the aggregate P50/P99
    stay in the summary. Throughput/error series come from the aggregate point
    (a single value) because those are not per-request fields.
    """
    field_by_metric = {
        "p50": "e2e_latency",
        "p99": "e2e_latency",
        "ttft_p50": "ttft",
        "ttft_p99": "ttft",
    }
    metric_def = next((m for m in templates.metrics if m["key"] == metric_key), None)
    label = metric_def["label"] if metric_def else metric_key
    unit = metric_def["unit"] if metric_def else ""
    req_field = field_by_metric.get(metric_key)
    if req_field is None:
        return {"metric": metric_key, "label": label, "unit": unit, "points": [], "mode": "aggregate"}
    points: list[dict[str, Any]] = []
    total = len(requests)
    if total == 0:
        return {"metric": metric_key, "label": label, "unit": unit, "points": [], "mode": "per_request"}
    step = max(1, total // max_points)
    for i in range(0, total, step):
        value = _number(requests[i].get(req_field))
        if value is not None:
            points.append({"x": i, "y": value})
    return {
        "metric": metric_key,
        "label": f"{label} · 逐请求 {req_field}",
        "unit": unit,
        "points": points,
        "mode": "per_request",
        "note": "结果文件没有时间列, 此序列为逐请求值(只读), 非引擎重算的百分位",
    }


def chart_data(
    store: Store,
    templates: Templates,
    run_ids: list[str],
    metric: str = "p99",
    x: str = "auto",
) -> dict[str, Any]:
    """Payload for GET /api/experiments/chart.

    Single id -> time series (per-request). Multiple ids -> compare chart,
    x = sweep field (num_concurrency / batch_size / scenario) or run index.
    """
    valid_keys = {m["key"] for m in templates.metrics}
    if metric not in valid_keys:
        metric = "p99"
    if not run_ids:
        return {"mode": "empty", "metric": metric, "series": [], "points": []}

    if len(run_ids) == 1:
        run_id = run_ids[0]
        data = _best_aggregate(store, run_id)
        agg = (data or {}).get("aggregated_metrics") or {}
        requests = (data or {}).get("individual_request_metrics") or []
        series = _series_from_requests(templates, requests, metric)
        # throughput / error_rate have no per-request field -> one aggregate point
        if series["mode"] == "aggregate":
            value = _number(_dig(agg, next(m["agg"] for m in templates.metrics if m["key"] == metric)))
            if value is not None:
                series["points"] = [{"x": 0, "y": value}]
        return {
            "mode": "single",
            "metric": metric,
            "unit": series["unit"],
            "label": series["label"],
            "note": series.get("note", ""),
            "series": [{"run_id": run_id, "title": store.read_meta(run_id).get("title", run_id), "points": series["points"]}],
        }

    # multi-run compare
    x_key = x
    if x_key in ("", "auto", None):
        x_key = "num_concurrency"
    points: list[dict[str, Any]] = []
    for idx, run_id in enumerate(run_ids):
        data = _best_aggregate(store, run_id)
        agg = (data or {}).get("aggregated_metrics") or {}
        values = metric_values(templates, agg)
        if x_key == "index":
            xv: Any = idx
        elif x_key in ("num_concurrency", "batch_size"):
            xv = _number(agg.get(x_key))
        elif x_key == "concurrency_x_batch":
            # total documents in flight = batch_size x num_concurrency; this is
            # the axis that makes 批10x并发10 comparable with 批2x并发50.
            bs = _number(agg.get("batch_size"))
            nc = _number(agg.get("num_concurrency"))
            xv = bs * nc if bs is not None and nc is not None else None
        elif x_key == "scenario":
            xv = agg.get("scenario", run_id[:8])
        else:
            xv = _number(_dig(agg, x_key))
        if xv is None:
            xv = idx
        points.append(
            {
                "run_id": run_id,
                "title": store.read_meta(run_id).get("title", run_id),
                "x": xv,
                "y": values.get(metric),
                "metrics": values,
            }
        )
    points.sort(key=lambda p: (isinstance(p["x"], str), p["x"]))
    metric_def = next((m for m in templates.metrics if m["key"] == metric), {})
    return {
        "mode": "compare",
        "metric": metric,
        "unit": metric_def.get("unit", ""),
        "label": metric_def.get("label", metric),
        "x_field": x_key,
        "points": points,
    }
