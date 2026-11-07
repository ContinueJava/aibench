"""Loader for the ONE backend template config file (guardrail #6).

Everything the form and the naming rules need lives in
``templates/webui_templates.json``: recommended (stress-tier) parameter
values, task display names / short codes, the abbreviation table, the
title/slug templates, metric definitions and the duration presets.  Editing
that file changes both the New-Run form and the auto-generated names on the
next process restart -- no frontend change and no rebuild is required.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from genai_bench.web.config import WebConfig


# Backends whose vendor code appends the FULL "/v1/..." path to the host we hand
# it (see genai_bench/user/openai_user.py: endpoint = "/v1/chat/completions").
# A base that already ends in "/v1" would therefore become "/v1/v1/chat/..." ,
# so strip it for these backends only.  Other backends (oci-*, azure-*, aws-*,
# gcp-*) build their own URLs from the base and must be left untouched.
OPENAI_PATH_BACKENDS = frozenset({"openai", "vllm", "sglang", "together"})


def normalise_api_base(backend: Any, base: Any) -> Any:
    """Drop a trailing ``/v1`` for backends that append it themselves."""
    if not isinstance(base, str) or not base:
        return base
    if str(backend or "") not in OPENAI_PATH_BACKENDS:
        return base
    trimmed = base.strip().rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return trimmed


def _as_option(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise one option entry to a uniform shape."""
    return {
        "label": raw.get("label", str(raw.get("value", ""))),
        "value": raw.get("value"),
        "short": raw.get("short", ""),
        "slug": raw.get("slug", ""),
        "scenario": raw.get("scenario"),
    }


@dataclass
class Resolved:
    """A fully-resolved task + form values, ready to build a CLI argv or a name."""

    task_id: str
    task_def: dict[str, Any]
    values: dict[str, Any]
    scenario: str
    num_concurrency: list[int]
    batch_size: list[int]
    iteration_type: str
    max_time_per_run: int
    max_requests_per_run: int
    key_params: str
    key_slug: str
    engine: dict[str, Any] = field(default_factory=dict)

    @property
    def display(self) -> str:
        return self.task_def["display_name"]

    @property
    def short_code(self) -> str:
        return self.task_def["short_code"]

    @property
    def task(self) -> str:
        return self.task_def["task"]

    @property
    def model(self) -> str:
        return str(self.values.get("api_model_name") or "")

    @property
    def tokenizer(self) -> str:
        return str(self.values.get("model_tokenizer") or "")

    def friendly(self) -> dict[str, Any]:
        """The full, sanitized, human-facing form state (for params.json / 反显)."""
        return {
            "template_version": self.task_def.get("_version"),
            "task_id": self.task_id,
            "task": self.task,
            "values": dict(self.values),
            "fixed": dict(self.task_def.get("fixed", {})),
        }


class Templates:
    """Accessor around the single webui_templates.json document."""

    def __init__(self, data: dict[str, Any], config: WebConfig | None = None):
        self.data = data
        self.config = config

    @classmethod
    def load(cls, config: WebConfig) -> "Templates":
        path: Path = config.templates_file
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or "tasks" not in data:
            raise ValueError(f"Invalid template config: {path}")
        return cls(data, config)

    # -- raw sections -------------------------------------------------------
    @property
    def version(self) -> str:
        return str(self.data.get("version", "0"))

    @property
    def naming(self) -> dict[str, Any]:
        return self.data.get("naming", {})

    @property
    def metrics(self) -> list[dict[str, Any]]:
        return self.data.get("metrics", [])

    @property
    def durations(self) -> list[dict[str, Any]]:
        return self.data.get("durations", [])

    @property
    def max_requests(self) -> int:
        return int(self.data.get("max_requests_per_run", 2_000_000_000))

    @property
    def deviations(self) -> list[str]:
        return self.data.get("deviations", [])

    @property
    def ladders(self) -> dict[str, Any]:
        """Fine-grained pressure ladders (per task) declared in the config file."""
        return self.data.get("ladders", {})

    @property
    def presets(self) -> list[dict[str, Any]]:
        """Purpose-named one-click presets ("套餐") declared in the config file."""
        return self.data.get("presets", [])

    @property
    def glossary(self) -> dict[str, Any]:
        """Beginner-friendly explanations (大白话), all in the one config file."""
        return self.data.get("glossary", {})

    def task_ids(self) -> list[str]:
        return [t["id"] for t in self.data["tasks"]]

    def task(self, task_id: str) -> dict[str, Any]:
        for task in self.data["tasks"]:
            if task["id"] == task_id:
                # attach version so a params.json can record which template made it
                enriched = dict(task)
                enriched["_version"] = self.version
                return enriched
        raise KeyError(f"Unknown task_id: {task_id!r}")

    # -- field helpers ------------------------------------------------------
    def common_fields(self, task_id: str) -> list[dict[str, Any]]:
        """Common connection/advanced fields with per-task overrides applied."""
        task = self.task(task_id)
        overrides = task.get("overrides", {})
        result: list[dict[str, Any]] = []
        for raw in self.data.get("common_fields", []):
            field_def = dict(raw)
            if raw["key"] in overrides:
                field_def.update(overrides[raw["key"]])
            result.append(field_def)
        return result

    def task_fields(self, task_id: str) -> list[dict[str, Any]]:
        """Primary fields with ``options_ref`` resolved against duration presets."""
        task = self.task(task_id)
        result: list[dict[str, Any]] = []
        for raw in task.get("fields", []):
            field_def = dict(raw)
            if "options_ref" in field_def and field_def["options_ref"] == "durations":
                field_def["options"] = [
                    _as_option(
                        {
                            "label": d["label"],
                            "value": d["value"],
                            "short": d.get("short", f"t{d['value']}m"),
                            "slug": d.get("slug", f"t{d['value']}m"),
                        }
                    )
                    for d in self.durations
                ]
            elif "options" in field_def:
                field_def["options"] = [_as_option(o) for o in field_def["options"]]
            result.append(field_def)
        return result

    def all_fields(self, task_id: str) -> list[dict[str, Any]]:
        return self.common_fields(task_id) + self.task_fields(task_id)

    def defaults(self, task_id: str) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field_def in self.common_fields(task_id):
            values[field_def["key"]] = field_def.get("default")
        for field_def in self.task_fields(task_id):
            values[field_def["key"]] = field_def.get("default")
        return values

    @staticmethod
    def option(field_def: dict[str, Any], value: Any) -> dict[str, Any] | None:
        for opt in field_def.get("options", []):
            if str(opt.get("value")) == str(value):
                return opt
        return None

    # -- resolve ------------------------------------------------------------
    def resolve(self, task_id: str, values: dict[str, Any] | None = None) -> Resolved:
        """Merge form values over defaults and derive engine parameters + naming tokens."""
        task = self.task(task_id)
        fields = self.task_fields(task_id)
        merged = self.defaults(task_id)
        if values:
            for key, value in values.items():
                merged[key] = value  # keep explicit None (advanced blanks) for 反显

        def field_by(maps_to: str) -> dict[str, Any] | None:
            return next((f for f in fields if f.get("maps_to") == maps_to), None)

        # traffic scenario string (chat D(in,out) / embedding E(n) / rerank R(doc,query))
        scenario_field = field_by("traffic_scenario")
        scenario = ""
        if scenario_field is not None:
            opt = self.option(scenario_field, merged.get(scenario_field["key"]))
            if opt and opt.get("scenario"):
                scenario = str(opt["scenario"])

        # concurrency / batch: single iteration point per run (sweep = serial starts)
        conc_field = field_by("num_concurrency")
        num_concurrency = (
            [int(merged[conc_field["key"]])] if conc_field is not None else [1]
        )
        batch_field = field_by("batch_size")
        if batch_field is not None:
            batch_size = [int(merged[batch_field["key"]])]
        else:
            batch_size = [int(x) for x in task.get("fixed", {}).get("batch_size", [1])]

        iteration_type = task.get("fixed", {}).get("iteration_type", "num_concurrency")
        time_field = field_by("max_time_per_run")
        max_time_per_run = int(merged[time_field["key"]]) if time_field else 10

        # short tokens for title / slug, in the config-declared order
        tokens: list[str] = []
        slug_tokens: list[str] = []
        by_key = {f["key"]: f for f in fields}
        for key in task.get("key_param_order", []):
            field_def = by_key.get(key)
            if field_def is None:
                continue
            opt = self.option(field_def, merged.get(key))
            if opt:
                tokens.append(str(opt.get("short") or opt.get("label") or opt["value"]))
                slug_tokens.append(str(opt.get("slug") or opt["value"]))
            else:
                # readonly / plain numeric field without an option list
                abbrev = field_def.get("abbrev", key)
                tokens.append(f"{abbrev}{merged.get(key)}")
                slug_tokens.append(f"{abbrev}{merged.get(key)}")

        # engine argv values (no secrets ever enter this dict)
        engine: dict[str, Any] = {
            "api_backend": merged.get("api_backend"),
            # the form may carry a vLLM-style ".../v1" base; the vendor appends
            # "/v1/..." itself, so the engine value must not double it up
            "api_base": normalise_api_base(
                merged.get("api_backend"), merged.get("api_base")
            ),
            "api_model_name": merged.get("api_model_name"),
            "task": task["task"],
            "model": merged.get("api_model_name") or merged.get("model_tokenizer"),
            "model_tokenizer": merged.get("model_tokenizer"),
            "max_time_per_run": max_time_per_run,
            "max_requests_per_run": self.max_requests,
            "traffic_scenario": [scenario] if scenario else [],
            "num_concurrency": num_concurrency,
            "batch_size": batch_size,
            "iteration_type": iteration_type,
        }
        for field_def in self.common_fields(task_id):
            if field_def.get("group") == "advanced" and field_def.get("cli_flag"):
                value = merged.get(field_def["key"])
                if value not in (None, ""):
                    engine[field_def["key"]] = value

        return Resolved(
            task_id=task_id,
            task_def=task,
            values=merged,
            scenario=scenario,
            num_concurrency=num_concurrency,
            batch_size=batch_size,
            iteration_type=iteration_type,
            max_time_per_run=max_time_per_run,
            max_requests_per_run=self.max_requests,
            key_params=" ".join(tokens),
            key_slug="-".join(slug_tokens),
            engine=engine,
        )

    # -- public payload for GET /api/templates ------------------------------
    def public(self) -> dict[str, Any]:
        tasks = []
        for task_id in self.task_ids():
            task = self.task(task_id)
            fields_public = []
            for field_def in self.task_fields(task_id):
                fields_public.append(
                    {
                        "key": field_def["key"],
                        "label": field_def["label"],
                        "type": field_def["type"],
                        "abbrev": field_def.get("abbrev", ""),
                        "default": field_def.get("default"),
                        "group": field_def.get("group", "primary"),
                        "sweepable": bool(field_def.get("sweepable")),
                        "allow_custom": bool(field_def.get("allow_custom")),
                        "custom_type": field_def.get("custom_type", "number"),
                        "hint": field_def.get("hint", ""),
                        "plain": field_def.get("plain")
                        or self.glossary.get("field_plain", {}).get(
                            field_def["key"], ""
                        ),
                        "options": [
                            {
                                "label": o["label"],
                                "value": o["value"],
                                "short": o.get("short", ""),
                                "slug": o.get("slug", ""),
                            }
                            for o in field_def.get("options", [])
                        ],
                    }
                )
            common_public = []
            for field_def in self.common_fields(task_id):
                # raw string options (e.g. api_backend ["openai", "vllm"])
                # must become {label,value} objects, because the frontend
                # fieldHtml() renders a <select> from o.value/o.label.
                options = []
                for raw in field_def.get("options", []):
                    if isinstance(raw, dict):
                        options.append(_as_option(raw))
                    else:
                        options.append({"label": str(raw), "value": raw, "short": "", "slug": str(raw)})
                common_public.append(
                    {
                        "key": field_def["key"],
                        "label": field_def["label"],
                        "type": field_def["type"],
                        "default": field_def.get("default"),
                        "group": field_def.get("group", "connection"),
                        "required": bool(field_def.get("required")),
                        "hint": field_def.get("hint", ""),
                        "plain": field_def.get("plain")
                        or self.glossary.get("field_plain", {}).get(
                            field_def["key"], ""
                        ),
                        "options": options,
                        "low_frequency": bool(field_def.get("low_frequency")),
                        "allow_custom": bool(field_def.get("allow_custom")),
                        "custom_type": field_def.get("custom_type", "text"),
                    }
                )
            tasks.append(
                {
                    "id": task_id,
                    "display_name": task["display_name"],
                    "short_code": task["short_code"],
                    "task": task["task"],
                    "defaults_note": task.get("defaults_note", ""),
                    "fields": fields_public,
                    "common_fields": common_public,
                    "key_param_order": task.get("key_param_order", []),
                }
            )
        return {
            "version": self.version,
            "naming": {
                "abbrevs": self.naming.get("abbrevs", {}),
                "timezone_label": self.naming.get("timezone_label", "Asia/Shanghai"),
                "title_template": self.naming.get("title_template", ""),
                "sweep_title_template": self.naming.get("sweep_title_template", ""),
                "slug_max_len": self.naming.get("slug_max_len", 80),
            },
            "metrics": self.metrics,
            "durations": self.durations,
            "max_requests_per_run": self.max_requests,
            "tasks": tasks,
            "glossary": self.glossary,
            "presets": [p for p in self.presets if isinstance(p, dict)],
            "ladders": {
                key: value
                for key, value in self.ladders.items()
                if not key.startswith("_")
            },
            "deviations": self.deviations,
        }
