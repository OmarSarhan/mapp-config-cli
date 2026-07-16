from __future__ import annotations

import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mapp_config_cli.config import ConfigStore, Profile, config_home, read_token_file
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
