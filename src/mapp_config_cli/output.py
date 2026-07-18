"""Safe, deterministic presentation helpers for interactive users."""

from __future__ import annotations

import json
from typing import Any


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "-"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _doctor(data: dict[str, Any]) -> str:
    lines = [f"Status: {'healthy' if data.get('healthy') is True else 'needs attention'}"]
    for section, fields in (
        ("profile", ("name", "endpoint", "storedInstanceId", "allowHttp")),
        ("target", ("liveInstanceId", "identityMatches", "compatible", "apiVersion", "contractVersion")),
        ("authentication", ("authenticated", "actor", "scopes")),
        ("workspace", ("accessible", "key", "revision")),
    ):
        value = data.get(section)
        if isinstance(value, dict):
            lines.append(f"\n{section.title()}:")
            lines.extend(f"  {field}: {_value(value[field])}" for field in fields if field in value)
    checks = data.get("checks")
    if isinstance(checks, list):
        lines.append("\nChecks:")
        for check in checks:
            if isinstance(check, dict):
                lines.append(f"  {'PASS' if check.get('passed') is True else 'FAIL'}  {_value(check.get('id'))}")
    return "\n".join(lines) + "\n"


def _proposal(data: dict[str, Any], action: str) -> str:
    container = data.get("proposal" if action == "create" else "check")
    if not isinstance(container, dict):
        return _json(data)
    title = "Proposal" if action == "create" else "Proposal check"
    lines = [title]
    for field in ("id", "status", "originalRevision", "explanation"):
        if field in container:
            lines.append(f"{field}: {_value(container[field])}")
    operations = container.get("operations")
    if isinstance(operations, list):
        lines.append(f"operations: {len(operations)}")
        for operation in operations:
            if isinstance(operation, dict):
                # Render the response as received; do not derive or fetch extra data.
                lines.append("  " + _value(operation))
    validation = data.get("validation")
    if isinstance(validation, dict):
        lines.append("Validation:")
        for level in ("errors", "warnings", "information"):
            values = validation.get(level)
            if isinstance(values, list):
                lines.append(f"  {level}: {len(values)}")
                lines.extend(f"    - {_value(item)}" for item in values)
    actions = data.get("nextActions")
    if isinstance(actions, list) and actions:
        lines.append("Next actions:")
        lines.extend(f"  - {_value(item)}" for item in actions)
    return "\n".join(lines) + "\n"


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def render(data: Any, *, command: str, output: str = "json") -> str:
    """Render existing response data without enriching or mutating it."""
    if output == "json":
        return _json(data)
    if output != "human":
        raise ValueError(f"unsupported output format: {output}")
    if not isinstance(data, dict):
        return _json(data)
    if command == "doctor":
        return _doctor(data)
    if command in {"proposals check", "proposals create"}:
        return _proposal(data, command.split()[1])
    return _json(data)
