from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mapp_config_cli.cli import _open_artifact_root, main, write_private_output
from mapp_config_cli.config import ConfigStore
from mapp_config_cli.errors import CliError, EXIT_CONNECTIVITY


class WindowsSupportBoundaryOrderTests(unittest.TestCase):
    def test_mutating_command_fails_before_local_or_remote_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            output = root / "result.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("mapp_config_cli.cli._is_native_windows", return_value=True),
                patch("mapp_config_cli.cli.run") as execute,
            ):
                code = main(
                    [
                        "--out",
                        str(output),
                        "proposals",
                        "apply",
                        "proposal-1",
                        "--confirm",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                    store=ConfigStore(config_root),
                )

            execute.assert_not_called()
            self.assertEqual(EXIT_CONNECTIVITY, code)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual(
                "platform.unsupported",
                json.loads(stderr.getvalue())["code"],
            )
            self.assertFalse(config_root.exists())
            self.assertFalse(output.exists())


@unittest.skipUnless(os.name == "nt", "native Windows safety tests")
class WindowsWriterDefenseTests(unittest.TestCase):
    def test_artifact_writer_defense_fails_without_creating_a_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "artifacts"

            with self.assertRaises(CliError) as raised:
                _open_artifact_root(str(destination))

            self.assertFalse(destination.exists())

        self.assertEqual(
            "visual.artifact_destination_unsafe",
            raised.exception.error_code,
        )

    def test_private_writer_defense_fails_without_creating_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "output.json"

            with self.assertRaises(CliError) as raised:
                write_private_output(str(destination), '{"secret": true}\n')

            self.assertFalse(destination.exists())

        self.assertEqual(
            "output.destination_unsafe",
            raised.exception.error_code,
        )
