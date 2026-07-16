from __future__ import annotations

import unittest

from mapp_config_cli.errors import CliError
from mapp_config_cli.operations import build_operations, parse_set


class OperationTests(unittest.TestCase):
    def test_parses_strict_json_and_plain_strings(self):
        self.assertEqual(parse_set("/a=true")["value"], True)
        self.assertEqual(parse_set("/a=#2563eb")["value"], "#2563eb")

    def test_rejects_nan_and_infinity(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(CliError):
                parse_set(f"/a={value}")

    def test_rejects_invalid_pointer_escape(self):
        with self.assertRaises(CliError):
            parse_set("/a~2b=1")

    def test_slash_addresses_an_empty_key_but_empty_root_is_rejected(self):
        self.assertEqual(parse_set("/=1")["path"], "/")
        with self.assertRaises(CliError):
            parse_set("=1")

    def test_requires_an_operation(self):
        with self.assertRaises(CliError):
            build_operations([], [])


if __name__ == "__main__":
    unittest.main()
