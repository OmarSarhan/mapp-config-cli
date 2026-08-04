import unittest
from pathlib import Path
import tomllib

import mapp_config_cli
from mapp_config_cli.cli import parser
from mapp_config_cli.errors import CliError


class PackageTests(unittest.TestCase):
    def test_package_import_and_version(self):
        self.assertRegex(mapp_config_cli.__version__, r"^\d+\.\d+\.\d+$")

    def test_package_version_matches_project_metadata(self):
        project = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            mapp_config_cli.__version__,
            project["project"]["version"],
        )

    def test_removed_commands_are_not_parsed(self):
        for arguments in (
            ["completion-spec"],
            ["sql", "explain"],
            ["proposals", "delete", "id"],
            ["semantic", "query"],
            ["semantic", "functions", "list"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(CliError):
                parser().parse_args(arguments)


if __name__ == "__main__":
    unittest.main()
