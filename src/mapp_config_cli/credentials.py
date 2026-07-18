from __future__ import annotations

from typing import Any, Callable

from .client import ApiClient, VerifiedTarget, verify_target
from .config import ConfigStore, Profile
from .errors import CliError, EXIT_AUTHENTICATION


ClientFactory = Callable[..., ApiClient]


def verify_and_replace_token(
    store: ConfigStore,
    profile_name: str | None,
    new_token: str,
    *,
    timeout: float = 30.0,
    client_factory: ClientFactory = ApiClient,
) -> tuple[Profile, VerifiedTarget]:
    """Verify a replacement token, then atomically publish it.

    Verification uses the exact stored profile identity and endpoint. The
    existing credential remains referenced unless every public and
    authenticated compatibility check succeeds.
    """
    if not isinstance(new_token, str) or not new_token:
        raise CliError(
            "Token must not be empty.",
            EXIT_AUTHENTICATION,
            error_code="auth.token_empty",
        )
    profile = store.selected_profile(profile_name)
    client = client_factory(
        profile.endpoint,
        new_token,
        timeout=timeout,
        allow_http=profile.allow_http,
    )
    target = verify_target(client, profile)
    replacement = store.replace_token(profile, new_token)
    return replacement, target


def rotation_result(profile: Profile, target: VerifiedTarget) -> dict[str, Any]:
    """Build a secret-free command result for a completed rotation."""
    return {
        "profile": profile.name,
        "endpoint": profile.endpoint,
        "instanceId": target.live_instance_id,
        "contractVersion": target.contract_version,
        "credentialReplaced": True,
    }
