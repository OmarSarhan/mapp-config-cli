from __future__ import annotations

import json
from typing import Iterable

from .errors import CliError, EXIT_USAGE


def _reject_constant(value: str):
    raise ValueError(f"{value} is not valid JSON")


def validate_pointer(pointer: str) -> str:
    if not pointer.startswith("/"):
        raise CliError(
            "Workspace paths must be non-root RFC 6901 JSON Pointers.",
            EXIT_USAGE,
            details={"path": pointer},
            error_code="operation.invalid_pointer",
        )
    index = 0
    while index < len(pointer):
        if pointer[index] == "~":
            if index + 1 >= len(pointer) or pointer[index + 1] not in "01":
                raise CliError(
                    "JSON Pointer escapes must use ~0 or ~1.",
                    EXIT_USAGE,
                    details={"path": pointer},
                    error_code="operation.invalid_pointer",
                )
            index += 2
        else:
            index += 1
    return pointer


def parse_set(value: str) -> dict:
    if "=" not in value:
        raise CliError(
            "--set requires POINTER=JSON.",
            EXIT_USAGE,
            error_code="operation.missing_value",
        )
    pointer, raw = value.split("=", 1)
    validate_pointer(pointer)
    try:
        parsed = json.loads(raw, parse_constant=_reject_constant)
    except json.JSONDecodeError:
        parsed = raw
    except ValueError as exc:
        raise CliError(
            f"Invalid JSON value: {exc}",
            EXIT_USAGE,
            details={"path": pointer},
            error_code="operation.invalid_json",
        ) from exc
    return {"op": "set", "path": pointer, "value": parsed}


def build_operations(sets: Iterable[str], unsets: Iterable[str]) -> list[dict]:
    operations = [parse_set(value) for value in sets]
    operations.extend({"op": "unset", "path": validate_pointer(path)} for path in unsets)
    if not operations:
        raise CliError(
            "At least one --set or --unset operation is required.",
            EXIT_USAGE,
            error_code="operation.empty",
        )
    return operations
