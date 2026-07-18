import shlex
import subprocess
import unittest

from mapp_config_cli.cli import parser
from mapp_config_cli.completion import generate_completion
from mapp_config_cli.output import render


class CompletionTests(unittest.TestCase):
    def bash_completions(self, words, current):
        script = generate_completion(parser(), "bash")
        quoted_words = " ".join(shlex.quote(word) for word in words)
        completed = subprocess.run(
            [
                "bash",
                "-c",
                script
                + f"\nCOMP_WORDS=({quoted_words})\n"
                + f"COMP_CWORD={current}\n"
                + "_config_cli_complete\nprintf '%s\\n' \"${COMPREPLY[@]}\"\n",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.splitlines()

    def test_scripts_are_deterministic_and_cover_nested_commands(self):
        for shell in ("bash", "zsh", "fish"):
            first = generate_completion(parser(), shell)
            self.assertEqual(first, generate_completion(parser(), shell))
            self.assertIn("config-cli", first)
            self.assertIn("proposals", first)
            self.assertIn("base-revision", first)

    def test_bash_ignores_global_option_values_when_finding_command_path(self):
        self.assertEqual(
            self.bash_completions(
                ["config-cli", "--profile", "production", "proposals", "cr"],
                4,
            ),
            ["create"],
        )

    def test_bash_completes_options_for_nested_commands(self):
        self.assertIn(
            "--base-revision",
            self.bash_completions(
                ["config-cli", "proposals", "create", "--b"],
                3,
            ),
        )

    def test_bash_completes_reload_xyz_alias_confirmation(self):
        self.assertIn(
            "--confirm",
            self.bash_completions(
                ["config-cli", "reload-xyz", "--c"],
                2,
            ),
        )

    def test_fish_nested_conditions_and_required_values_are_explicit(self):
        script = generate_completion(parser(), "fish")
        self.assertIn(
            "-n '__config_cli_path_is proposals create' -l base-revision -r",
            script,
        )
        self.assertIn("-n '__config_cli_path_is' -l profile -r", script)
        self.assertNotIn("__fish_seen_subcommand_from", script)

    def test_unknown_shell_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported shell"):
            generate_completion(parser(), "powershell")


class HumanOutputTests(unittest.TestCase):
    def test_json_remains_default(self):
        data = {"z": 2, "a": 1}
        self.assertEqual(render(data, command="doctor"), '{\n  "z": 2,\n  "a": 1\n}\n')

    def test_doctor_human_output_is_stable(self):
        data = {
            "healthy": True,
            "profile": {"name": "prod", "endpoint": "https://example.invalid"},
            "authentication": {"authenticated": True, "actor": "agent"},
            "workspace": {"accessible": True, "key": "map", "revision": "r1"},
            "checks": [{"id": "auth.access", "passed": True}],
        }
        output = render(data, command="doctor", output="human")
        self.assertIn("Status: healthy", output)
        self.assertIn("PASS  auth.access", output)
        self.assertNotIn("token", output.lower())

    def test_proposal_human_output_uses_only_response(self):
        data = {
            "check": {"originalRevision": "r1", "operations": [{"op": "remove", "path": "/x"}]},
            "validation": {"errors": [], "warnings": ["review"], "information": []},
            "nextActions": [{"id": "proposal.create"}],
        }
        output = render(data, command="proposals check", output="human")
        self.assertIn("Proposal check", output)
        self.assertIn('"path":"/x"', output)
        self.assertIn("warnings: 1", output)


if __name__ == "__main__":
    unittest.main()
