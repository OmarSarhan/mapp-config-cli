from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_CONFLICT = 4
EXIT_CONNECTIVITY = 5
EXIT_VISUAL = 6
EXIT_AUTHENTICATION = 7


SENSITIVE_KEYS = {
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}
SENSITIVE_KEY_PARTS = ("authorization", "bearer", "credential", "password", "secret", "token")


def redact(value: Any) -> Any:
    """Remove obvious credential fields while retaining useful error details."""
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if str(key).lower() in SENSITIVE_KEYS
                or any(part in str(key).lower() for part in SENSITIVE_KEY_PARTS)
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


@dataclass
class CliError(Exception):
    message: str
    exit_code: int = EXIT_USAGE
    details: Any = None
    http_status: int | None = None
    error_code: str | None = None
    _safe_details: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        super().__init__(self.message)
        self._safe_details = redact(self.details)

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": self.message,
            "exitCode": self.exit_code,
        }
        if self.error_code:
            payload["code"] = self.error_code
        if self.http_status is not None:
            payload["httpStatus"] = self.http_status
        if self._safe_details not in (None, {}, []):
            payload["details"] = self._safe_details
        return payload
