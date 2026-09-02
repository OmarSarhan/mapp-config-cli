"""Safe, deterministic presentation helpers for interactive users."""

from __future__ import annotations

import json
from typing import Any


def sanitize_terminal_text(value: Any, *, preserve_newlines: bool = False) -> str:
    """Render untrusted text without emitting terminal control characters."""
    text = str(value)
    return "".join(
        character
        if (
            ord(character) > 0x1F
            and ord(character) != 0x7F
            and not 0x80 <= ord(character) <= 0x9F
        )
        or (preserve_newlines and character == "\n")
        else f"\\u{ord(character):04x}"
        for character in text
    )


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "-"
    if isinstance(value, (dict, list)):
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sanitize_terminal_text(serialized)
    return sanitize_terminal_text(value)


def _doctor(data: dict[str, Any]) -> str:
    lines = [f"Status: {'healthy' if data.get('healthy') is True else 'needs attention'}"]
    for section, fields in (
        ("profile", ("name", "endpoint", "storedInstanceId", "allowHttp")),
        ("target", ("liveInstanceId", "identityMatches", "compatible", "apiVersion", "contractVersion")),
        ("authentication", ("authenticated", "actor", "scopes")),
        ("workspace", ("accessible", "key", "revision")),
        (
            "semantic",
            (
                "advertised",
                "authorized",
                "available",
                "schemaVersion",
                "catalogRevision",
            ),
        ),
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
        return _human_json(data)
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


def _semantic_generation(data: dict[str, Any]) -> str:
    draft = data.get("draft")
    generation = data.get("generation")
    if not isinstance(draft, dict) or not isinstance(generation, dict):
        return _human_json(data)
    lines = ["Semantic draft"]
    for field in ("assetId", "baseVersion", "target", "explanation"):
        if field in draft:
            lines.append(f"{field}: {_value(draft[field])}")
    lines.extend([
        f"provider: {_value(generation.get('provider'))}",
        f"model: {_value(generation.get('model'))}",
        f"metadataOnly: {_value(generation.get('metadataOnly'))}",
    ])
    if "contextOptions" in generation:
        lines.append(
            f"contextOptions: {_value(generation['contextOptions'])}"
        )
    lines.append(
        f"proposalCreated: {_value(generation.get('proposalCreated'))}"
    )
    operations = draft.get("operations")
    if isinstance(operations, list):
        lines.append(f"operations: {len(operations)}")
        lines.extend(
            "  " + _value(operation)
            for operation in operations
        )
    actions = data.get("nextActions")
    if isinstance(actions, list) and actions:
        lines.append("Next actions:")
        lines.extend(f"  - {_value(item)}" for item in actions)
    return "\n".join(lines) + "\n"


def _derived_layer_plan(data: dict[str, Any]) -> str:
    plan = data.get("derivedLayerPlan")
    if not isinstance(plan, dict):
        return _human_json(data)
    request = plan.get("createRequest")
    access = plan.get("accessPathProbe")
    lines = [
        "Derived-layer plan",
        f"mutationApplied: {_value(data.get('mutationApplied'))}",
        f"version: {_value(plan.get('version'))}",
        f"planFingerprint: {_value(plan.get('planFingerprint'))}",
    ]
    if isinstance(request, dict):
        for field in ("name", "kind", "sources"):
            if field in request:
                lines.append(f"{field}: {_value(request[field])}")
    if isinstance(access, dict):
        summary = access.get("summary")
        lines.append("Access paths:")
        if isinstance(summary, dict):
            for field in (
                "relationScanCount",
                "indexBackedScanCount",
                "sequentialScanCount",
                "foreignScanCount",
                "subPlanCount",
                "executionGroupCount",
                "transformedPredicate",
            ):
                if field in summary:
                    lines.append(f"  {field}: {_value(summary[field])}")
        warnings = access.get("warnings")
        if isinstance(warnings, list):
            lines.append(f"  warnings: {len(warnings)}")
            lines.extend(f"    - {_value(warning)}" for warning in warnings)
        lines.append(f"  sources: {_value(access.get('sources'))}")
        lines.append(f"  relationScans: {_value(access.get('relationScans'))}")
    for field in (
        "resolvedSpatialScope",
        "queryPlanProbe",
        "queryPlanningProbe",
        "materializationProbe",
        "createRequest",
    ):
        if field in plan:
            lines.append(f"{field}: {_value(plan[field])}")
    return "\n".join(lines) + "\n"


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _human_json(data: Any) -> str:
    # JSON already escapes control characters inside strings. Preserve only
    # the serializer's structural newlines while escaping raw C1 controls.
    return sanitize_terminal_text(_json(data), preserve_newlines=True)


def render(data: Any, *, command: str, output: str = "json") -> str:
    """Render existing response data without enriching or mutating it."""
    if output == "json":
        return _json(data)
    if output != "human":
        raise ValueError(f"unsupported output format: {output}")
    if not isinstance(data, dict):
        return _human_json(data)
    if command == "doctor":
        return _doctor(data)
    if command in {"proposals check", "proposals create"}:
        return _proposal(data, command.split()[1])
    if command.startswith("semantic generate "):
        return _semantic_generation(data)
    if command == "derived-layers plan":
        return _derived_layer_plan(data)
    return _human_json(data)
