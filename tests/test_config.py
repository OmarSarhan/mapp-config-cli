from __future__ import annotations

import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mapp_config_cli.config import (
    MAX_CONFIG_FILE_BYTES,
    MAX_TOKEN_FILE_BYTES,
    ConfigStore,
    Profile,
    config_home,
    read_token_file,
)
from mapp_config_cli.errors import CliError


def _concurrent_profile_writer(
    root: str,
    worker: int,
    start,
) -> None:
    store = ConfigStore(Path(root))
    start.wait()
    for iteration in range(12):
        marker = f"{worker}-{iteration}"
        store.save_profile(
            Profile(
                f"worker-{worker}",
                f"https://worker-{marker}.example.com",
                f"instance-{marker}",
                "1.0",
            ),
            f"worker-token-{marker}",
        )
        store.save_profile(
            Profile(
                "shared",
                f"https://shared-{marker}.example.com",
                f"instance-{marker}",
                "1.0",
            ),
            f"shared-token-{marker}",
        )


@unittest.skipUnless(os.name == "posix", "POSIX permission semantics required")
class ConfigPermissionTests(unittest.TestCase):
    def test_writes_private_atomic_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            store = ConfigStore(root)
            store.save_profile(
                Profile("production", "https://config.example.com", "instance", "1.0"),
                "secret",
            )
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(store.profiles_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.credentials_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.lock_path.stat().st_mode), 0o600)
            self.assertFalse(list(root.glob("*.tmp")))

    def test_rejects_non_private_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("secret", encoding="utf-8")
            os.chmod(token, 0o644)
            with self.assertRaises(CliError):
                read_token_file(token)
            os.chmod(token, 0o600)
            self.assertEqual(read_token_file(token), "secret")

    def test_rejects_symlink_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("secret", encoding="utf-8")
            os.chmod(target, 0o600)
            link = root / "token"
            link.symlink_to(target)
            with self.assertRaises(CliError):
                read_token_file(link)

    def test_token_file_read_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            with token.open("wb") as stream:
                stream.truncate(MAX_TOKEN_FILE_BYTES + 1)
            os.chmod(token, 0o600)

            with self.assertRaises(CliError) as raised:
                read_token_file(token)

        self.assertEqual(
            raised.exception.error_code,
            "auth.token_file_too_large",
        )
        self.assertEqual(
            raised.exception.safe_details["maxBytes"],
            MAX_TOKEN_FILE_BYTES,
        )

    def test_token_file_swap_to_symlink_is_rejected_at_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "token"
            target = root / "target"
            token.write_text("original", encoding="utf-8")
            target.write_text("replacement", encoding="utf-8")
            os.chmod(token, 0o600)
            os.chmod(target, 0o600)
            real_open = os.open
            swapped = False

            def swap_before_open(path, flags, *args, **kwargs):
                nonlocal swapped
                if not swapped and Path(path) == token:
                    swapped = True
                    token.unlink()
                    token.symlink_to(target)
                return real_open(path, flags, *args, **kwargs)

            with (
                patch(
                    "mapp_config_cli.config.os.open",
                    side_effect=swap_before_open,
                ),
                self.assertRaises(CliError) as raised,
            ):
                read_token_file(token)

        self.assertTrue(swapped)
        self.assertEqual(raised.exception.error_code, "config.symlink_rejected")

    def test_configuration_json_read_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            root.mkdir(mode=0o700)
            profiles = root / "profiles.json"
            with profiles.open("wb") as stream:
                stream.truncate(MAX_CONFIG_FILE_BYTES + 1)
            os.chmod(profiles, 0o600)

            with self.assertRaises(CliError) as raised:
                ConfigStore(root).profiles_document()

        self.assertEqual(raised.exception.error_code, "config.file_too_large")
        self.assertEqual(
            raised.exception.safe_details["maxBytes"],
            MAX_CONFIG_FILE_BYTES,
        )

    def test_private_text_reads_reject_invalid_utf8_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "token"
            token.write_bytes(b"\xff")
            os.chmod(token, 0o600)
            with self.assertRaises(CliError) as token_error:
                read_token_file(token)

            config_root = root / "config"
            config_root.mkdir(mode=0o700)
            profiles = config_root / "profiles.json"
            profiles.write_bytes(b"\xff")
            os.chmod(profiles, 0o600)
            with self.assertRaises(CliError) as config_error:
                ConfigStore(config_root).profiles_document()

        self.assertEqual(
            token_error.exception.error_code,
            "auth.token_file_unreadable",
        )
        self.assertEqual(
            config_error.exception.error_code,
            "config.invalid_json",
        )

    def test_malformed_state_raises_cli_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            root.mkdir(mode=0o700)
            profiles = root / "profiles.json"
            profiles.write_text("{broken", encoding="utf-8")
            os.chmod(profiles, 0o600)
            with self.assertRaises(CliError) as raised:
                ConfigStore(root).profiles_document()
            self.assertEqual(raised.exception.error_code, "config.invalid_json")

    def test_rejects_cross_profile_credential_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            root.mkdir(mode=0o700)
            profiles = root / "profiles.json"
            credentials = root / "credentials.json"
            profiles.write_text(
                json.dumps(
                    {
                        "active": "production",
                        "profiles": {
                            "production": {
                                "endpoint": "https://config.example.com",
                                "instanceId": "instance",
                                "contractVersion": "1.0",
                                "credentialId": (
                                    "credential:other:"
                                    + "0" * 32
                                ),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            credentials.write_text(
                json.dumps(
                    {
                        "credential:other:" + "0" * 32: "wrong-token",
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(profiles, 0o600)
            os.chmod(credentials, 0o600)
            with self.assertRaises(CliError) as raised:
                ConfigStore(root).connection()
            self.assertEqual(raised.exception.error_code, "profile.malformed")

    def test_profile_documents_are_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            store.save_profile(
                Profile("test", "http://localhost", "instance", "1.0"),
                "token",
            )
            self.assertEqual(
                json.loads(store.profiles_path.read_text())["active"],
                "test",
            )

    def test_reads_legacy_profile_and_migrates_on_next_save(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            root.mkdir(mode=0o700)
            profiles = root / "profiles.json"
            credentials = root / "credentials.json"
            profiles.write_text(
                json.dumps(
                    {
                        "active": "production",
                        "profiles": {
                            "production": {
                                "endpoint": "https://old.example.com",
                                "instanceId": "old-instance",
                                "contractVersion": "1.0",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            credentials.write_text(
                json.dumps({"production": "old-token"}),
                encoding="utf-8",
            )
            os.chmod(profiles, 0o600)
            os.chmod(credentials, 0o600)
            store = ConfigStore(root)

            legacy_profile, legacy_token = store.connection()
            self.assertEqual(legacy_profile.endpoint, "https://old.example.com")
            self.assertEqual(legacy_token, "old-token")

            store.save_profile(
                Profile(
                    "production",
                    "https://new.example.com",
                    "new-instance",
                    "1.0",
                ),
                "new-token",
            )
            migrated_profile, migrated_token = store.connection()
            stored = json.loads(profiles.read_text())["profiles"]["production"]
            self.assertEqual(migrated_profile.endpoint, "https://new.example.com")
            self.assertEqual(migrated_token, "new-token")
            self.assertRegex(
                stored["credentialId"],
                r"^credential:production:[0-9a-f]{32}$",
            )
            self.assertNotIn(
                "credentialId",
                store.list_profiles()["profiles"]["production"],
            )

    def test_allow_http_is_explicit_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            root.mkdir(mode=0o700)
            profiles = root / "profiles.json"
            credentials = root / "credentials.json"
            profiles.write_text(
                json.dumps(
                    {
                        "active": "production",
                        "profiles": {
                            "production": {
                                "endpoint": "https://http.example.com",
                                "instanceId": "http-instance",
                                "contractVersion": "1.0",
                                "allowHttp": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            profiles.write_text(
                profiles.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            credentials.write_text(json.dumps({"production": "allow-token"}), encoding="utf-8")
            os.chmod(profiles, 0o600)
            os.chmod(credentials, 0o600)
            profile, token = ConfigStore(root).connection()
            self.assertTrue(profile.allow_http)
            self.assertEqual(token, "allow-token")

            profiles.write_text(
                json.dumps(
                    {
                        "active": "production",
                        "profiles": {
                            "production": {
                                "endpoint": "https://legacy.example.com",
                                "instanceId": "legacy-instance",
                                "contractVersion": "1.0",
                                "insecure": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(profiles, 0o600)
            profile, token = ConfigStore(root).connection()
            self.assertFalse(profile.allow_http)
            self.assertEqual(token, "allow-token")

    def test_interrupted_replace_cannot_cross_pair_endpoint_and_token(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            store.save_profile(
                Profile(
                    "production",
                    "https://old.example.com",
                    "old-instance",
                    "1.0",
                ),
                "old-token",
            )
            write_json = store._write_json

            def interrupt_profile_publish(path, data):
                if path == store.profiles_path:
                    raise CliError("simulated interruption")
                return write_json(path, data)

            with patch.object(
                store,
                "_write_json",
                side_effect=interrupt_profile_publish,
            ), self.assertRaises(CliError):
                store.save_profile(
                    Profile(
                        "production",
                        "https://new.example.com",
                        "new-instance",
                        "1.0",
                    ),
                    "new-token",
                )

            profile, token = store.connection()
            self.assertEqual(profile.endpoint, "https://old.example.com")
            self.assertEqual(token, "old-token")

    def test_concurrent_processes_preserve_profiles_and_credential_pairing(self):
        context = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as directory:
            root = str(Path(directory) / "config")
            start = context.Event()
            processes = [
                context.Process(
                    target=_concurrent_profile_writer,
                    args=(root, worker, start),
                )
                for worker in range(4)
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)

            store = ConfigStore(Path(root))
            document = store.list_profiles()
            for worker in range(4):
                self.assertIn(f"worker-{worker}", document["profiles"])
            shared = store.selected_profile("shared")
            token = store.token_for(shared)
            marker = shared.endpoint.removeprefix(
                "https://shared-"
            ).removesuffix(".example.com")
            self.assertEqual(token, f"shared-token-{marker}")

    def test_failed_token_publish_preserves_old_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            old = store.save_profile(
                Profile("production", "https://config.example.com", "instance", "1.0"),
                "old-token",
            )
            write_json = store._write_json

            def interrupt_profile_publish(path, data):
                if path == store.profiles_path:
                    raise CliError("simulated interruption")
                return write_json(path, data)

            with patch.object(store, "_write_json", side_effect=interrupt_profile_publish):
                with self.assertRaises(CliError):
                    store.replace_token(old, "new-token")

            profile, token = store.connection("production")
            self.assertEqual(profile, old)
            self.assertEqual(token, "old-token")

    def test_profile_save_can_be_rolled_back_without_overwriting_newer_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            previous = store.save_profile(
                Profile("production", "https://old.example.com", "old", "1.0"),
                "old-token",
            )
            saved = store.save_profile_transaction(
                Profile("production", "https://new.example.com", "new", "1.0"),
                "new-token",
                expected_profile=previous,
            )
            self.assertTrue(store.rollback_profile_save(saved))
            profile, token = store.connection("production")
            self.assertEqual(profile, previous)
            self.assertEqual(token, "old-token")

            newer = store.save_profile(
                Profile("production", "https://newer.example.com", "newer", "1.0"),
                "newer-token",
            )
            self.assertFalse(store.rollback_profile_save(saved))
            self.assertEqual(store.connection("production"), (newer, "newer-token"))

    def test_profile_install_rejects_a_concurrent_target_change(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            observed = store.save_profile(
                Profile("production", "https://old.example.com", "old", "1.0"),
                "old-token",
            )
            concurrent = store.save_profile(
                Profile(
                    "production",
                    "https://concurrent.example.com",
                    "concurrent",
                    "1.0",
                ),
                "concurrent-token",
            )

            with self.assertRaises(CliError) as raised:
                store.save_profile_transaction(
                    Profile("production", "https://new.example.com", "new", "1.0"),
                    "new-token",
                    expected_profile=observed,
                )

            self.assertEqual(raised.exception.error_code, "profile.changed")
            self.assertEqual(
                store.connection("production"),
                (concurrent, "concurrent-token"),
            )

    def test_rollback_restores_pruned_credential_and_preserves_active_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            previous = store.save_profile(
                Profile("production", "https://old.example.com", "old", "1.0"),
                "old-token",
            )
            saved = store.save_profile_transaction(
                Profile("production", "https://new.example.com", "new", "1.0"),
                "new-token",
                expected_profile=previous,
            )
            store.save_profile(
                Profile("staging", "https://staging.example.com", "staging", "1.0"),
                "staging-token",
            )

            self.assertTrue(store.rollback_profile_save(saved))
            self.assertEqual(
                store.connection("production"),
                (previous, "old-token"),
            )
            self.assertEqual(store.list_profiles()["active"], "staging")

    def test_successful_token_rotations_remove_superseded_local_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            profile = store.save_profile(
                Profile("production", "https://config.example.com", "instance", "1.0"),
                "token-0",
            )

            for index in range(1, 4):
                profile = store.replace_token(profile, f"token-{index}")

            self.assertEqual(
                store.credentials_document(),
                {profile.credential_id: "token-3"},
            )

    def test_post_commit_cleanup_failure_does_not_report_install_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            previous = store.save_profile(
                Profile("production", "https://old.example.com", "old", "1.0"),
                "old-token",
            )
            write_json = store._write_json
            credential_writes = 0

            def fail_cleanup(path, data):
                nonlocal credential_writes
                if path == store.credentials_path:
                    credential_writes += 1
                    if credential_writes == 2:
                        raise CliError("simulated cleanup failure")
                return write_json(path, data)

            with patch.object(store, "_write_json", side_effect=fail_cleanup):
                saved = store.save_profile_transaction(
                    Profile("production", "https://new.example.com", "new", "1.0"),
                    "new-token",
                    expected_profile=previous,
                )

            self.assertEqual(
                store.connection("production"),
                (saved.installed, "new-token"),
            )

    def test_malformed_check_cache_prevents_profile_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            profile = store.save_profile(
                Profile("production", "https://config.example.com", "instance", "1.0"),
                "token",
            )
            store.checks_path.write_text("{broken", encoding="utf-8")
            os.chmod(store.checks_path, 0o600)

            with self.assertRaises(CliError) as raised:
                store.remove_profile("production")

            self.assertEqual(raised.exception.error_code, "config.invalid_json")
            self.assertEqual(store.connection("production"), (profile, "token"))

    def test_checked_operations_cache_is_private_and_target_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            profile = store.save_profile(
                Profile("production", "https://config.example.com", "instance", "1.0"),
                "token",
            )
            fingerprint = "c" * 64
            store.save_check(profile, {
                "checkFingerprint": fingerprint,
                "originalRevision": "rev-1",
                "operations": [{"op": "set", "path": "/title", "value": "Safe"}],
            })
            loaded = store.load_check(profile, fingerprint)
            self.assertEqual(loaded["revision"], "rev-1")
            self.assertEqual(stat.S_IMODE(store.checks_path.stat().st_mode), 0o600)

    def test_checked_operations_cache_is_scoped_by_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            production = store.save_profile(
                Profile(
                    "production",
                    "https://config.example.com",
                    "production-instance",
                    "1.0",
                ),
                "production-token",
            )
            staging = store.save_profile(
                Profile(
                    "staging",
                    "https://staging-config.example.com",
                    "staging-instance",
                    "1.0",
                ),
                "staging-token",
            )
            fingerprint = "d" * 64
            store.save_check(
                production,
                {
                    "checkFingerprint": fingerprint,
                    "originalRevision": "production-revision",
                    "operations": [
                        {"op": "set", "path": "/title", "value": "Production"}
                    ],
                },
            )
            store.save_check(
                staging,
                {
                    "checkFingerprint": fingerprint,
                    "originalRevision": "staging-revision",
                    "operations": [
                        {"op": "set", "path": "/title", "value": "Staging"}
                    ],
                },
            )

            production_check = store.load_check(production, fingerprint)
            staging_check = store.load_check(staging, fingerprint)
            self.assertEqual(production_check["revision"], "production-revision")
            self.assertEqual(production_check["endpoint"], production.endpoint)
            self.assertEqual(staging_check["revision"], "staging-revision")
            self.assertEqual(staging_check["endpoint"], staging.endpoint)
            stored_checks = json.loads(store.checks_path.read_text())["checks"]
            self.assertEqual(
                set(stored_checks),
                {
                    f"production:workspace:{fingerprint}",
                    f"staging:workspace:{fingerprint}",
                },
            )

    def test_checked_operations_cache_reads_legacy_fingerprint_key(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            profile = store.save_profile(
                Profile(
                    "production",
                    "https://config.example.com",
                    "production-instance",
                    "1.0",
                ),
                "token",
            )
            fingerprint = "e" * 64
            store._write_json(
                store.checks_path,
                {
                    "checks": {
                        fingerprint: {
                            "profile": profile.name,
                            "endpoint": profile.endpoint,
                            "instanceId": profile.instance_id,
                            "revision": "legacy-revision",
                            "operations": [
                                {"op": "set", "path": "/title", "value": "Legacy"}
                            ],
                            "explanation": "Created by the previous cache format.",
                        }
                    }
                },
            )

            loaded = store.load_check(profile, fingerprint)
            self.assertEqual(loaded["revision"], "legacy-revision")
            self.assertEqual(loaded["profile"], profile.name)

    def test_checked_operations_cache_separates_workspace_and_semantic_domains(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            profile = store.save_profile(
                Profile(
                    "production",
                    "https://config.example.com",
                    "production-instance",
                    "1.0",
                ),
                "token",
            )
            fingerprint = "f" * 64
            store.save_check(
                profile,
                {
                    "checkFingerprint": fingerprint,
                    "originalRevision": "workspace-revision",
                    "operations": [
                        {"op": "set", "path": "/title", "value": "Workspace"}
                    ],
                },
                domain="workspace",
            )
            store.save_check(
                profile,
                {
                    "fingerprint": fingerprint,
                    "assetId": "asset:derived:places",
                    "baseVersion": 4,
                    "operations": [
                        {
                            "op": "set",
                            "path": "/curated/description",
                            "value": "Places",
                        }
                    ],
                },
                domain="semantic",
            )

            workspace = store.load_check(
                profile,
                fingerprint,
                domain="workspace",
            )
            semantic = store.load_check(
                profile,
                fingerprint,
                domain="semantic",
            )
            keys = set(json.loads(store.checks_path.read_text())["checks"])

        self.assertEqual(workspace["revision"], "workspace-revision")
        self.assertEqual(semantic["revision"], 4)
        self.assertEqual(semantic["assetId"], "asset:derived:places")
        self.assertEqual(
            keys,
            {
                f"production:workspace:{fingerprint}",
                f"production:semantic:{fingerprint}",
            },
        )

    def test_checked_operations_cache_rejects_cross_domain_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            profile = store.save_profile(
                Profile(
                    "production",
                    "https://config.example.com",
                    "production-instance",
                    "1.0",
                ),
                "token",
            )
            fingerprint = "a" * 64
            store.save_check(
                profile,
                {
                    "checkFingerprint": fingerprint,
                    "originalRevision": "workspace-revision",
                    "operations": [
                        {"op": "set", "path": "/title", "value": "Workspace"}
                    ],
                },
                domain="workspace",
            )
            with self.assertRaises(CliError) as raised:
                store.load_check(
                    profile,
                    fingerprint,
                    domain="semantic",
                )

        self.assertEqual(
            raised.exception.error_code,
            "semantic.proposal.check_domain_mismatch",
        )

    def test_configuration_status_validates_all_private_state_files(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            profile = store.save_profile(
                Profile("production", "https://config.example.com", "instance", "1.0"),
                "token",
            )
            store.save_check(
                profile,
                {
                    "checkFingerprint": "f" * 64,
                    "originalRevision": "revision",
                    "operations": [],
                },
            )

            status = store.configuration_status()
            self.assertEqual(
                set(status),
                {"profilesFile", "credentialsFile", "checksFile", "lockFile"},
            )
            for file_status in status.values():
                self.assertTrue(file_status["exists"])
                self.assertTrue(file_status["private"])
                self.assertEqual(file_status["mode"], "0600")

            os.chmod(store.checks_path, 0o644)
            with self.assertRaises(CliError) as raised:
                store.configuration_status()
            self.assertEqual(raised.exception.error_code, "config.insecure_permissions")

    def test_config_home_honors_explicit_then_xdg(self):
        with patch.dict(
            os.environ,
            {
                "CONFIG_CLI_HOME": "/tmp/explicit-config",
                "XDG_CONFIG_HOME": "/tmp/xdg-config",
            },
            clear=False,
        ):
            self.assertEqual(config_home(), Path("/tmp/explicit-config"))
        with patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": "/tmp/xdg-config"},
            clear=False,
        ):
            os.environ.pop("CONFIG_CLI_HOME", None)
            self.assertEqual(
                config_home(),
                Path("/tmp/xdg-config/mapp-config-cli"),
            )


if __name__ == "__main__":
    unittest.main()
