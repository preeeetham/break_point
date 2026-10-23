"""The jury's incident report: a strict JSON Schema, and a validator.

Only the second half of the jury's schema survived extraction from the slides: `proposed_action`, `approval`,
`execution`, `verification`, `prevention`, `limitations`. Those are reproduced exactly (same required fields,
consts, enums and `additionalProperties: false`). The first half (`incident_id` .. `evidence`) is modelled from the
brief's step 7 ("timeline, root cause, evidence, action taken, prevention") and marked ASSUMED below; when the real
schema arrives, replace REPORT_SCHEMA and nothing else changes, because the Reporter validates against it.

The submission file wraps reports as {"incident_reports": [...]}, as in the extracted document.
"""
from __future__ import annotations

from typing import Any

NULLABLE_STR = {"type": ["string", "null"]}

REPORT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Incident Zero incident report",
    "type": "object",
    "required": ["incident_id", "title", "status", "detected_at", "summary", "timeline", "root_cause", "evidence",
                 "proposed_action", "approval", "execution", "verification", "prevention", "limitations"],
    "properties": {
        # ---- ASSUMED (first half of the jury schema was not in the extracted text) ----
        "incident_id": {"type": "string"},
        "title": {"type": "string"},
        "status": {"type": "string", "enum": ["resolved", "dry_run_complete", "escalated", "rejected", "failed",
                                              "open"]},
        "detected_at": {"type": "string"},
        "resolved_at": NULLABLE_STR,
        "summary": {"type": "string"},
        "timeline": {
            "type": "array",
            "items": {
                "type": "object", "required": ["timestamp", "event"],
                "properties": {"timestamp": {"type": "string"}, "actor": {"type": "string"},
                               "event": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        "root_cause": {
            "type": "object", "required": ["component", "description", "confidence"],
            "properties": {
                "component": {"type": "string"},
                "category": {"type": "string"},
                "description": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "additional_causes": {
                    "type": "array",
                    "items": {
                        "type": "object", "required": ["component", "description", "confidence"],
                        "properties": {"component": {"type": "string"}, "category": {"type": "string"},
                                       "description": {"type": "string"},
                                       "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object", "required": ["id", "source", "detail"],
                "properties": {"id": {"type": "string"}, "source": {"type": "string"},
                               "query": {"type": "string"}, "detail": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        # ---- EXACT (from the extracted jury schema) ----
        "proposed_action": {
            "type": "object",
            "required": ["action", "reason", "dry_run"],
            "properties": {
                "action": {"type": "string", "description": "Must be an allowed Action API action."},
                "parameters": {"type": "object"},
                "reason": {"type": "string"},
                "dry_run": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "approval": {
            "type": "object",
            "required": ["required", "decision"],
            "properties": {
                "required": {"const": True},
                "decision": {"type": "string", "enum": ["pending", "approved", "rejected", "not_requested"]},
                "approver": NULLABLE_STR,
                "timestamp": NULLABLE_STR,
            },
            "additionalProperties": False,
        },
        "execution": {
            "type": "object",
            "required": ["attempted", "action_api_only"],
            "properties": {
                "attempted": {"type": "boolean"},
                "action_api_only": {"const": True},
                "action_result": NULLABLE_STR,
                "rollback_performed": {"type": "boolean"},
                "notes": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "verification": {
            "type": "object",
            "required": ["checked", "recovered"],
            "properties": {
                "checked": {"type": "boolean"},
                "recovered": {"type": ["boolean", "null"]},
                "signals_checked": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "prevention": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


class ReportInvalid(ValueError):
    pass


def validate(report: dict) -> None:
    """Raise ReportInvalid listing every violation. Uses jsonschema when installed, else a strict built-in check."""
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema ships with the incident_zero extra
        errors = _fallback_errors(report, REPORT_SCHEMA, "$")
    else:
        v = jsonschema.Draft202012Validator(REPORT_SCHEMA)
        errors = [f"{'/'.join(map(str, e.absolute_path)) or '$'}: {e.message}" for e in v.iter_errors(report)]
    if errors:
        raise ReportInvalid("; ".join(errors))


def _fallback_errors(value: Any, schema: dict, where: str) -> list[str]:
    errs: list[str] = []
    if "const" in schema and value != schema["const"]:
        errs.append(f"{where}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}: {value!r} not in {schema['enum']}")
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    pytypes = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float), "null": type(None)}
    if types and not any(isinstance(value, pytypes[x]) and not (x == "number" and isinstance(value, bool))
                         for x in types):
        return errs + [f"{where}: expected {types}"]
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errs += [f"{where}: missing {k}" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            errs += [f"{where}: unexpected {k}" for k in value if k not in props]
        for k, sub in props.items():
            if k in value:
                errs += _fallback_errors(value[k], sub, f"{where}/{k}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errs += _fallback_errors(item, schema["items"], f"{where}/{i}")
    return errs
