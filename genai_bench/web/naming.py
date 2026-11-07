"""run_id generation + decoration-only title/slug (guardrail #11).

``run_id`` is the sole primary key: it is the experiment folder name, the
Dashboard locator, and the rerun handle. It is generated ONCE and NEVER changes.
``title`` / ``slug`` / ``tags`` / ``starred`` / ``group`` are decoration stored
only in meta.json; editing a title must not touch run_id, the folder name, or
the dashboard_url.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from genai_bench.web.templates import Resolved, Templates

# Strict primary-key shape: 8 hex - YYYYMMDD - HHMMSS. Passed verbatim as
# --experiment-folder-name, so it MUST NOT contain slashes / dots (path
# traversal). Used to validate every run_id coming from an API path.
RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9]{8}-[0-9]{6}$")

_SLUG_FORBIDDEN_RE = re.compile(r"[/\?%*:|\"<> ]")
_MODEL_SHORT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def new_run_id(now: datetime | None = None, tz_label: str = "Asia/Shanghai") -> str:
    """Generate an immutable, path-safe primary key."""
    now = now or datetime.now(ZoneInfo(tz_label))
    date_part = now.strftime("%Y%m%d")
    time_part = now.strftime("%H%M%S")
    return f"{uuid.uuid4().hex[:8]}-{date_part}-{time_part}"


def is_valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_RE.match(run_id or ""))


def model_short(model: str) -> str:
    """ASCII filename-safe model abbreviation for a slug."""
    if not model:
        return "model"
    text = _MODEL_SHORT_RE.sub("-", model.strip())
    return text.strip("-")[:40] or "model"


def _clean_slug(value: str, forbidden: str, max_len: int) -> str:
    value = _SLUG_FORBIDDEN_RE.sub("-", value)
    value = re.sub(r"[-_]{2,}", "-", value).strip("-_")
    return value[:max_len] or "run"


def make_title(
    templates: Templates,
    resolved: Resolved,
    now: datetime | None = None,
    *,
    sweep_key: str | None = None,
    sweep_values: list[Any] | None = None,
) -> str:
    """Build a readable title from the config's title_template."""
    naming = templates.naming
    tz = ZoneInfo(naming.get("timezone_label", "Asia/Shanghai"))
    local = (now or datetime.now(tz)).astimezone(tz)
    stamp_short = local.strftime(naming.get("stamp_short_format", "%m%d-%H%M"))
    if sweep_key and sweep_values is not None:
        tpl = naming.get(
            "sweep_title_template",
            "Sweep · {display} · {sweep_key}[{sweep_values}] · {stamp_short}",
        )
        return tpl.format(
            display=resolved.display,
            model_short=model_short(resolved.model),
            sweep_key=sweep_key,
            sweep_values=",".join(str(v) for v in sweep_values),
            stamp_short=stamp_short,
        )
    tpl = naming.get(
        "title_template", "{display} · {model_short} · {key_params} · {stamp_short}"
    )
    return tpl.format(
        display=resolved.display,
        model_short=model_short(resolved.model),
        key_params=resolved.key_params,
        stamp_short=stamp_short,
    )


def make_slug(
    templates: Templates, resolved: Resolved, run_id: str, now: datetime | None = None
) -> str:
    """Build an ASCII filename-safe slug (decoration; run_id still primary)."""
    naming = templates.naming
    tz = ZoneInfo(naming.get("timezone_label", "Asia/Shanghai"))
    local = (now or datetime.now(tz)).astimezone(tz)
    stamp_full = local.strftime(naming.get("stamp_full_format", "%Y%m%d-%H%M"))
    tpl = naming.get(
        "slug_template",
        "{short_code}_{model_short}_{key_slug}_{stamp_full}_{run4}",
    )
    raw = tpl.format(
        short_code=resolved.short_code,
        model_short=model_short(resolved.model),
        key_slug=resolved.key_slug,
        stamp_full=stamp_full,
        run4=run_id[:4],
    )
    return _clean_slug(
        raw,
        naming.get("slug_forbidden", "/\\?%*:|\"<> "),
        int(naming.get("slug_max_len", 80)),
    )


def export_names(
    templates: Templates,
    slugs: list[str],
    now: datetime | None = None,
    *,
    sweep_slug: str | None = None,
) -> dict[str, str]:
    """Readable export filenames (single zip/csv, multi zip)."""
    naming = templates.naming
    tz = ZoneInfo(naming.get("timezone_label", "Asia/Shanghai"))
    stamp_full = (now or datetime.now(tz)).astimezone(tz).strftime(
        naming.get("stamp_full_format", "%Y%m%d-%H%M")
    )
    export = naming.get("export", {})
    if len(slugs) == 1:
        return {
            "zip": export.get("single_zip", "{slug}.zip").format(slug=slugs[0]),
            "csv": export.get("single_csv", "{slug}.csv").format(slug=slugs[0]),
        }
    if sweep_slug:
        name = export.get("sweep_zip", "genai-bench_{sweep_slug}.zip").format(
            sweep_slug=sweep_slug
        )
    else:
        name = export.get(
            "multi_zip", "genai-bench_{N}条_{stamp_full}.zip"
        ).format(N=len(slugs), stamp_full=stamp_full)
    return {"zip": name}
