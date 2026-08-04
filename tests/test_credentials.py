from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mapp_config_cli.client import VerifiedTarget
from mapp_config_cli.config import ConfigStore, Profile
from mapp_config_cli.credentials import rotation_result, verify_and_replace_token
from mapp_config_cli.errors import CliError


class CredentialRotationTests(unittest.TestCase):
    def test_verifies_new_token_before_replacing_old_one(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            old = store.save_profile(
                Profile("production", "https://config.example.com", "instance", "1.0"),
                "old-token",
            )
            seen = {}

            def client_factory(endpoint, token, **kwargs):
                seen.update(endpoint=endpoint, token=token, kwargs=kwargs)
                return object()

            target = VerifiedTarget(
                old,
                {"instanceId": "instance"},
                {"instanceId": "instance", "contractVersion": "1.0"},
                {
                    "authenticated": True,
                    "actor": "token:rotation",
                    "tokenId": "rotation",
                    "scopes": ["full"],
                    "expires": None,
                },
            )
            with patch("mapp_config_cli.credentials.verify_target", return_value=target):
                replacement, verified = verify_and_replace_token(
                    store, "production", "new-token", client_factory=client_factory
                )

            self.assertEqual(seen["endpoint"], old.endpoint)
            self.assertEqual(seen["token"], "new-token")
            self.assertIs(verified, target)
            self.assertEqual(store.connection("production"), (replacement, "new-token"))
            self.assertNotIn("new-token", str(rotation_result(replacement, target)))

    def test_verification_failure_preserves_old_token(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            old = store.save_profile(
                Profile("production", "https://config.example.com", "instance", "1.0"),
                "old-token",
            )
            failure = CliError("credential rejected", error_code="auth.failed")
            with patch("mapp_config_cli.credentials.verify_target", side_effect=failure):
                with self.assertRaises(CliError):
                    verify_and_replace_token(
                        store,
                        "production",
                        "new-token",
                        client_factory=lambda *args, **kwargs: object(),
                    )
            self.assertEqual(store.connection("production"), (old, "old-token"))


if __name__ == "__main__":
    unittest.main()
