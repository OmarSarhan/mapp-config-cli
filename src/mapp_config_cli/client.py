from __future__ import annotations

import json
import math
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Profile
from .errors import (
    CliError,
    EXIT_AUTHENTICATION,
    EXIT_CONFLICT,
    EXIT_CONNECTIVITY,
    EXIT_VALIDATION,
)
from .version import __version__


SUPPORTED_CONTRACT_MAJOR = 1
SUPPORTED_API_MAJOR = 1
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)"
    r"(?:\.(?:0|[1-9][0-9]*)){0,2}"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def normalize_endpoint(endpoint: str, *, allow_http: bool = False) -> str:
    value = endpoint.strip()
    if not value or any(character.isspace() for character in value):
        raise CliError(
            "Endpoint must be an absolute HTTP(S) URL.",
            error_code="endpoint.invalid",
        )
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CliError(
            f"Endpoint is invalid: {exc}",
            error_code="endpoint.invalid",
        ) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CliError(
            "Endpoint must be an absolute HTTP(S) URL.",
            error_code="endpoint.invalid",
        )
    if parsed.username is not None or parsed.password is not None:
        raise CliError(
            "Endpoint must not contain embedded credentials.",
            error_code="endpoint.userinfo_rejected",
        )
    if parsed.query or parsed.fragment:
        raise CliError(
            "Endpoint must not contain a query string or fragment.",
            error_code="endpoint.not_root",
        )
    if parsed.path not in {"", "/"}:
        raise CliError(
            "Endpoint must be the configuration service root URL.",
            details={"path": parsed.path},
            error_code="endpoint.not_root",
        )
    loopback = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"} or parsed.hostname.lower().endswith(".localhost")
    if parsed.scheme == "http" and not (loopback or allow_http):
        raise CliError(
            "Remote endpoints must use HTTPS. Use --allow-http only for a trusted development endpoint.",
            error_code="endpoint.https_required",
        )
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def quote_segment(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _version_major(version: Any, *, label: str, error_code: str) -> int:
    if not isinstance(version, str) or not version:
        raise CliError(
            f"Server did not provide a valid {label} version.",
            EXIT_CONFLICT,
            details={f"{label}Version": version},
            error_code=error_code,
        )
    match = VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise CliError(
            f"Server provided an unsupported {label} version format.",
            EXIT_CONFLICT,
            details={f"{label}Version": version},
            error_code=error_code,
        )
    return int(match.group("major"))


def contract_major(version: Any) -> int:
    return _version_major(
        version,
        label="contract",
        error_code="contract.invalid_version",
    )


def api_major(version: Any) -> int:
    return _version_major(
        version,
        label="api",
        error_code="api.invalid_version",
    )


def require_compatible_contract(version: Any) -> str:
    major = contract_major(version)
    if major != SUPPORTED_CONTRACT_MAJOR:
        raise CliError(
            "Server contract is not compatible with this client.",
            EXIT_CONFLICT,
            details={
                "contractVersion": version,
                "supportedContractMajor": SUPPORTED_CONTRACT_MAJOR,
            },
            error_code="contract.incompatible",
        )
    return str(version)


def require_compatible_api(version: Any) -> str:
    major = api_major(version)
    if major != SUPPORTED_API_MAJOR:
        raise CliError(
            "Server API is not compatible with this client.",
            EXIT_CONFLICT,
            details={
                "apiVersion": version,
                "supportedApiMajor": SUPPORTED_API_MAJOR,
            },
            error_code="api.incompatible",
        )
    return str(version)


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Return redirect responses as errors without issuing another request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reject_constant(value: str):
    raise ValueError(f"{value} is not valid JSON")


def _scrub_secret(value: Any, secret: str | None) -> Any:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[redacted]")
    if isinstance(value, dict):
        return {key: _scrub_secret(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_secret(item, secret) for item in value]
    return value


def _response_json(body: bytes, *, status: int, content_type: str | None) -> dict[str, Any]:
    if not body and status == 204:
        return {}
    try:
        value = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CliError(
            "Configuration service returned a non-JSON response.",
            EXIT_CONNECTIVITY,
            details={
                "contentType": content_type,
                "responseBytes": len(body),
            },
            http_status=status,
            error_code="api.non_json_response",
        ) from exc
    if not isinstance(value, dict):
        raise CliError(
            "Configuration service returned an unexpected JSON value.",
            EXIT_CONNECTIVITY,
            details={"responseType": type(value).__name__},
            http_status=status,
            error_code="api.invalid_response",
        )
    return value


def _read_bounded(stream) -> bytes:
    body = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise CliError(
            "Configuration service response exceeds the 20 MiB client limit.",
            EXIT_CONNECTIVITY,
            error_code="api.response_too_large",
        )
    return body


def _http_exit(status: int, details: Any, failure_code: int | None) -> int:
    if status in {401, 403}:
        return EXIT_AUTHENTICATION
    message = details.get("error", "") if isinstance(details, dict) else ""
    if status == 409 or "workspace changed" in str(message).lower():
        return EXIT_CONFLICT
    if failure_code is not None:
        return failure_code
    if status in {400, 404, 422}:
        return EXIT_VALIDATION
    return EXIT_CONNECTIVITY


class ApiClient:
    def __init__(
        self,
        endpoint: str,
        token: str | None = None,
        *,
        timeout: float = 60,
        allow_http: bool = False,
    ):
        self.endpoint = normalize_endpoint(endpoint, allow_http=allow_http)
        self.token = token
        if not math.isfinite(timeout) or timeout <= 0:
            raise CliError("Timeout must be greater than zero.", error_code="client.invalid_timeout")
        self.timeout = timeout
        context = ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            RejectRedirects(),
            urllib.request.HTTPSHandler(context=context),
        )

    def _transport_endpoint(self) -> tuple[str, str | None]:
        parsed = urllib.parse.urlsplit(self.endpoint)
        if parsed.scheme == "http" and parsed.hostname and parsed.hostname.endswith(".localhost"):
            port = f":{parsed.port}" if parsed.port else ""
            transport = urllib.parse.urlunsplit(
                parsed._replace(netloc=f"127.0.0.1{port}")
            )
            return transport, parsed.netloc
        return self.endpoint, None

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Any = None,
        authenticated: bool = True,
        failure_code: int | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/"):
            raise CliError("API path must start with '/'.", error_code="client.invalid_path")
        transport_endpoint, host_header = self._transport_endpoint()
        headers = {
            "Accept": "application/json",
            "User-Agent": f"mapp-config-cli/{__version__}",
        }
        if host_header:
            headers["Host"] = host_header
        if authenticated:
            if not self.token:
                raise CliError(
                    "No bearer token is available.",
                    EXIT_AUTHENTICATION,
                    error_code="auth.credential_missing",
                )
            headers["Authorization"] = f"Bearer {self.token}"
        body = None
        if payload is not None:
            try:
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise CliError(
                    f"Request payload is not valid JSON: {exc}",
                    EXIT_VALIDATION,
                    error_code="client.invalid_payload",
                ) from exc
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            transport_endpoint + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                response_body = _read_bounded(response)
                return _scrub_secret(
                    _response_json(
                        response_body,
                        status=response.status,
                        content_type=response.headers.get("Content-Type"),
                    ),
                    self.token if authenticated else None,
                )
        except urllib.error.HTTPError as exc:
            try:
                if 300 <= exc.code < 400:
                    raise CliError(
                        "Configuration service redirects are not allowed.",
                        EXIT_CONNECTIVITY,
                        details={"status": exc.code},
                        http_status=exc.code,
                        error_code="api.redirect_rejected",
                    ) from exc
                response_body = _read_bounded(exc)
                try:
                    details: Any = json.loads(
                        response_body.decode("utf-8"),
                        parse_constant=_reject_constant,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    details = {
                        "contentType": exc.headers.get("Content-Type"),
                        "responseBytes": len(response_body),
                    }
                details = _scrub_secret(details, self.token)
                message = (
                    details.get("error")
                    if isinstance(details, dict) and isinstance(details.get("error"), str)
                    else f"Configuration service returned HTTP {exc.code}."
                )
                raise CliError(
                    str(_scrub_secret(message, self.token)),
                    _http_exit(exc.code, details, failure_code),
                    details=details,
                    http_status=exc.code,
                    error_code="api.http_error",
                ) from exc
            finally:
                exc.close()
        except CliError:
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
            raise CliError(
                f"Unable to reach configuration endpoint: {exc}",
                EXIT_CONNECTIVITY,
                error_code="api.unreachable",
            ) from exc
        except OSError as exc:
            raise CliError(
                f"Configuration request failed: {exc}",
                EXIT_CONNECTIVITY,
                error_code="api.transport_error",
            ) from exc


@dataclass(frozen=True)
class VerifiedTarget:
    profile: Profile
    identity: dict[str, Any]
    contract: dict[str, Any]

    @property
    def live_instance_id(self) -> str:
        return str(self.identity["instanceId"])

    @property
    def contract_version(self) -> str:
        return str(self.contract["contractVersion"])

    def context(self) -> dict[str, Any]:
        return {
            "profile": self.profile.name,
            "endpoint": self.profile.endpoint,
            "instanceId": self.live_instance_id,
        }


def verify_target(client: ApiClient, profile: Profile) -> VerifiedTarget:
    require_compatible_contract(profile.contract_version)
    identity = client.request("/api/public/identity", authenticated=False)
    live_instance = identity.get("instanceId")
    if not isinstance(live_instance, str) or not live_instance:
        raise CliError(
            "Configuration service identity response is incomplete.",
            EXIT_CONFLICT,
            details=identity,
            error_code="instance.invalid_identity",
        )
    if live_instance != profile.instance_id:
        raise CliError(
            "Live configuration instance does not match the selected profile.",
            EXIT_CONFLICT,
            details={
                "storedInstanceId": profile.instance_id,
                "liveInstanceId": live_instance,
            },
            error_code="instance.mismatch",
        )
    if identity.get("contractVersion") is not None:
        require_compatible_contract(identity["contractVersion"])
    contract = client.request("/api/contract")
    contract_instance = contract.get("instanceId")
    if contract_instance != live_instance:
        raise CliError(
            "Authenticated contract identity does not match the public instance identity.",
            EXIT_CONFLICT,
            details={
                "publicInstanceId": live_instance,
                "contractInstanceId": contract_instance,
            },
            error_code="instance.contract_mismatch",
        )
    require_compatible_contract(contract.get("contractVersion"))
    require_compatible_api(contract.get("apiVersion"))
    return VerifiedTarget(profile, identity, contract)
