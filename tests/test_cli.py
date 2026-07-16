from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from mapp_config_cli.cli import _locale, main, parser
from mapp_config_cli.config import ConfigStore, Profile
from mapp_config_cli.errors import (
    CliError,
    EXIT_CONFLICT,
    EXIT_CONNECTIVITY,
    EXIT_USAGE,
    EXIT_VALIDATION,
    EXIT_VISUAL,
)

from tests.support import JsonServer, standard_routes


class CliTests(unittest.TestCase):
    def configured_store(
        self,
        directory: str,
        endpoint: str,
        *,
        instance_id: str = "instance-1",
        token: str = "stored-token",
    ) -> ConfigStore:
        store = ConfigStore(Path(directory) / "config")
        store.save_profile(
            Profile("test", endpoint, instance_id, "1.0"),
            token,
        )
        return store

    def invoke(self, arguments, store):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(arguments, stdout=stdout, stderr=stderr, store=store)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_describe_includes_target_workspace_auth_and_versions(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(["describe"], store)
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["storedInstanceId"], "instance-1")
        self.assertEqual(payload["liveInstanceId"], "instance-1")
        self.assertEqual(payload["workspaceKey"], "demo")
        self.assertEqual(payload["revision"], "rev-1")
        self.assertEqual(payload["actor"], "token:abc")
        self.assertEqual(payload["scopes"], ["full"])
        self.assertTrue(payload["compatibility"]["compatible"])
        self.assertIn("client", payload["versions"])

    def test_identity_mismatch_stops_before_sending_token(self):
        routes = standard_routes(instance_id="different-instance")
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(
                directory,
                server.endpoint,
                instance_id="expected-instance",
            )
            code, stdout, stderr = self.invoke(
                [
                    "proposals",
                    "create",
                    "--base-revision",
                    "rev-1",
                    "--set",
                    "/locale/view/z=12",
                ],
                store,
            )
            requests = list(server.requests)
        self.assertEqual(code, EXIT_CONFLICT)
        self.assertEqual(stdout, "")
        self.assertEqual(len(requests), 1)
        self.assertIsNone(requests[0]["headers"].get("Authorization"))
        self.assertEqual(json.loads(stderr)["code"], "instance.mismatch")

    def test_proposal_create_requires_and_sends_base_revision(self):
        captured = {}

        def create(request):
            captured.update(request["body"])
            return (
                201,
                {
                    "proposal": {
                        "id": "proposal-1",
                        "status": "pending",
                        "originalRevision": request["body"]["revision"],
                    }
                },
            )

        routes = standard_routes()
        routes[("POST", "/api/proposals")] = create
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "proposals",
                    "create",
                    "--base-revision",
                    "rev-observed",
                    "--set",
                    "/locale/view/z=12",
                    "--explanation",
                    "Change only the zoom.",
                ],
                store,
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(captured["revision"], "rev-observed")
        self.assertEqual(captured["operations"][0]["value"], 12)

        with self.assertRaises(CliError) as raised:
            parser().parse_args(
                ["proposals", "create", "--set", "/locale/view/z=12"]
            )
        self.assertEqual(raised.exception.exit_code, EXIT_USAGE)
        with self.assertRaises(CliError):
            parser().parse_args(
                [
                    "proposals",
                    "create",
                    "--base-revision",
                    "",
                    "--set",
                    "/locale/view/z=12",
                ]
            )

    def test_proposal_apply_requires_confirmation(self):
        with self.assertRaises(CliError):
            parser().parse_args(["proposals", "apply", "proposal-1"])
        parsed = parser().parse_args(
            ["proposals", "apply", "proposal-1", "--confirm"]
        )
        self.assertTrue(parsed.confirm)

    def test_stale_proposal_is_not_sent_for_application(self):
        routes = standard_routes(revision="rev-current")
        routes[("GET", "/api/proposals/proposal-1")] = (
            200,
            {
                "proposal": {
                    "id": "proposal-1",
                    "status": "pending",
                    "originalRevision": "rev-old",
                }
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["proposals", "apply", "proposal-1", "--confirm"],
                store,
            )
            apply_requests = [
                request
                for request in server.requests
                if request["method"] == "POST" and request["path"].endswith("/apply")
            ]
        self.assertEqual(code, EXIT_CONFLICT)
        self.assertEqual(stdout, "")
        self.assertEqual(apply_requests, [])
        self.assertEqual(
            json.loads(stderr)["code"],
            "proposal.revision_conflict",
        )

    def test_apply_timeout_preserves_committed_state_and_is_not_retried(self):
        routes = standard_routes()
        routes[("GET", "/api/proposals/proposal-1")] = (
            200,
            {
                "proposal": {
                    "id": "proposal-1",
                    "status": "pending",
                    "originalRevision": "rev-1",
                }
            },
        )
        routes[("POST", "/api/proposals/proposal-1/apply")] = (
            504,
            {
                "error": "Workspace was saved, but XYZ reload did not complete.",
                "saved": True,
                "revision": "rev-2",
                "proposal": {
                    "id": "proposal-1",
                    "status": "applied",
                    "appliedRevision": "rev-2",
                },
                "reload": {"completed": False, "targetFingerprint": "sha256-value"},
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["proposals", "apply", "proposal-1", "--confirm"],
                store,
            )
            apply_requests = [
                request
                for request in server.requests
                if request["method"] == "POST"
                and request["path"] == "/api/proposals/proposal-1/apply"
            ]
        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        error = json.loads(stderr)
        self.assertEqual(error["code"], "api.http_error")
        self.assertEqual(error["httpStatus"], 504)
        self.assertTrue(error["details"]["saved"])
        self.assertEqual(error["details"]["revision"], "rev-2")
        self.assertEqual(error["details"]["proposal"]["status"], "applied")
        self.assertEqual(
            error["details"]["proposal"]["appliedRevision"],
            "rev-2",
        )
        self.assertEqual(len(apply_requests), 1)

    def test_applying_proposal_can_be_reconciled_without_old_revision_preflight(self):
        routes = standard_routes(revision="rev-2")
        routes[("GET", "/api/proposals/proposal-1")] = (
            200,
            {
                "proposal": {
                    "id": "proposal-1",
                    "status": "applying",
                    "originalRevision": "rev-1",
                }
            },
        )
        routes[("POST", "/api/proposals/proposal-1/apply")] = (
            200,
            {
                "proposal": {
                    "id": "proposal-1",
                    "status": "applied",
                    "originalRevision": "rev-1",
                    "appliedRevision": "rev-2",
                },
                "reload": {"completed": True},
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["proposals", "apply", "proposal-1", "--confirm"],
                store,
            )
            workspace_requests = [
                request
                for request in server.requests
                if request["path"] == "/api/workspace"
            ]
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["proposal"]["status"], "applied")
        self.assertEqual(workspace_requests, [])

    def test_direct_mutation_is_always_dry_run(self):
        captured = {}

        def mutate(request):
            captured.update(request["body"])
            return (200, {"diff": [], "saved": False})

        routes = standard_routes()
        routes[("POST", "/api/mutate")] = mutate
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["set", "--set", "/locale/view/z=12"],
                store,
            )
        self.assertEqual(code, 0, stderr)
        self.assertIs(captured["save"], False)
        with self.assertRaises(CliError):
            parser().parse_args(
                ["set", "--set", "/locale/view/z=12", "--save"]
            )

    def test_direct_mutation_fails_closed_without_explicit_saved_false(self):
        for response in ({}, {"saved": True, "revision": "rev-2"}):
            with self.subTest(response=response):
                routes = standard_routes()
                routes[("POST", "/api/mutate")] = (200, response)
                with tempfile.TemporaryDirectory() as directory, JsonServer(
                    routes
                ) as server:
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        ["set", "--set", "/locale/view/z=12"],
                        store,
                    )
                self.assertEqual(code, EXIT_CONFLICT)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    json.loads(stderr)["code"],
                    "mutation.dry_run_unconfirmed",
                )

    def test_missing_server_command_fails_closed(self):
        routes = standard_routes()
        routes[("GET", "/api/contract")][1]["commands"].remove("workspace get")
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(["workspace", "get"], store)
        self.assertEqual(code, EXIT_CONFLICT)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["code"], "capability.missing")

    def test_visual_commands_send_bounded_explicit_view(self):
        captured = {}

        def visual(request):
            captured.update(request["body"])
            return (200, {"plan": request["body"]})

        routes = standard_routes()
        routes[("POST", "/api/visual-plan")] = visual
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "visual-plan",
                    "--layer",
                    "Bus Stops",
                    "--locale",
                    "en-GB",
                    "--lng",
                    "-1.55",
                    "--lat",
                    "53.81",
                    "--zoom",
                    "12.5",
                ],
                store,
            )
            incomplete_code, _, incomplete_error = self.invoke(
                ["visual-plan", "--layer", "Bus Stops", "--lng", "-1.55"],
                store,
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(captured["locale"], "en-GB")
        self.assertEqual(captured["centre"], [-1.55, 53.81])
        self.assertEqual(captured["zoom"], 12.5)
        self.assertEqual(incomplete_code, EXIT_USAGE)
        self.assertEqual(
            json.loads(incomplete_error)["code"],
            "usage.incomplete_visual_centre",
        )

    def test_every_visual_command_accepts_and_forwards_locale(self):
        captured: list[tuple[str, dict]] = []

        def visual(request):
            captured.append((request["path"], request["body"]))
            plan = {
                "layer": request["body"]["layer"],
                "locale": request["body"].get("locale"),
            }
            if request["path"] == "/api/visual-plan":
                return (200, {"plan": plan})
            return (200, {"plan": plan, "visual": {"passed": True}})

        routes = standard_routes()
        routes[("POST", "/api/visual-plan")] = visual
        routes[("POST", "/api/visual-test")] = visual
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            results = [
                self.invoke(
                    [command, "--layer", "Bus Stops", "--locale", "cy"],
                    store,
                )
                for command in ("visual-plan", "visual-test", "screenshot")
            ]
        for code, _, stderr in results:
            self.assertEqual(code, 0, stderr)
        self.assertEqual(
            captured,
            [
                ("/api/visual-plan", {"layer": "Bus Stops", "locale": "cy"}),
                ("/api/visual-test", {"layer": "Bus Stops", "locale": "cy"}),
                ("/api/visual-test", {"layer": "Bus Stops", "locale": "cy"}),
            ],
        )

    def test_visual_commands_reject_malformed_or_failed_success_responses(self):
        cases = (
            ("visual-plan", {}, EXIT_CONNECTIVITY, "visual.invalid_response"),
            ("visual-test", {}, EXIT_CONNECTIVITY, "visual.invalid_response"),
            (
                "visual-test",
                {
                    "plan": {"layer": "Bus Stops"},
                    "visual": {"passed": False},
                },
                EXIT_VISUAL,
                "visual.failed",
            ),
        )
        for command, response, expected_code, error_code in cases:
            with self.subTest(command=command, response=response):
                routes = standard_routes()
                path = (
                    "/api/visual-plan"
                    if command == "visual-plan"
                    else "/api/visual-test"
                )
                routes[("POST", path)] = (200, response)
                with tempfile.TemporaryDirectory() as directory, JsonServer(
                    routes
                ) as server:
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        [command, "--layer", "Bus Stops"],
                        store,
                    )
                self.assertEqual(code, expected_code)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["code"], error_code)

    def test_verified_context_overwrites_server_supplied_values(self):
        routes = standard_routes()
        routes[("GET", "/api/schema")] = (
            200,
            {
                "schema": {},
                "profile": "spoofed",
                "endpoint": "https://spoofed.example",
                "instanceId": "spoofed",
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(["schema"], store)
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["profile"], "test")
        self.assertEqual(payload["endpoint"], server.endpoint)
        self.assertEqual(payload["instanceId"], "instance-1")

    def test_named_locale_uses_pinned_xyz_effective_merge_semantics(self):
        workspace = {
            "locale": {
                "layers": {
                    "Inherited": {"format": "mvt", "display": True},
                    "Changed": {
                        "format": "mvt",
                        "style": {"states": ["default", "selected"]},
                    },
                },
                "plugins": ["base"],
                "info": [{"field": "base"}],
                "flags": [True],
                "truthyScalar": "keep",
                "truthyArray": ["keep"],
                "emptyArray": [],
                "falsyScalar": "",
            },
            "locales": {
                "locale": {"layers": {"Wrong": {"format": "mvt"}}},
                "cy": {
                    "layers": {
                        "Changed": {
                            "style": {"states": ["selected"]},
                        }
                    },
                    "plugins": ["welsh"],
                    "info": [{"field": "override"}],
                    "flags": [1],
                    "truthyScalar": {"ignored": True},
                    "truthyArray": {"ignored": True},
                    "emptyArray": {"ignored": True},
                    "falsyScalar": {"merged": True},
                },
            },
        }
        default_name, default_locale = _locale(workspace, None)
        named_name, named_locale = _locale(workspace, "cy")
        literal_name, literal_locale = _locale(workspace, "locale")

        self.assertEqual(default_name, "locale")
        self.assertEqual(literal_name, "locale")
        self.assertEqual(default_locale, literal_locale)
        self.assertNotIn("Wrong", default_locale["layers"])
        self.assertEqual(named_name, "cy")
        self.assertIn("Inherited", named_locale["layers"])
        self.assertEqual(
            named_locale["layers"]["Changed"]["style"]["states"],
            ["selected"],
        )
        self.assertEqual(named_locale["plugins"], ["base", "welsh"])
        self.assertEqual(
            named_locale["info"],
            [{"field": "base"}, {"field": "override"}],
        )
        self.assertEqual(named_locale["flags"], [True, 1])
        self.assertEqual(named_locale["truthyScalar"], "keep")
        self.assertEqual(named_locale["truthyArray"], ["keep"])
        self.assertEqual(named_locale["emptyArray"], [])
        self.assertEqual(named_locale["falsyScalar"], {"merged": True})
        self.assertEqual(named_locale["key"], "cy")
        self.assertNotIn("key", workspace["locales"]["cy"])

    def test_locale_without_default_uses_xyz_synthetic_empty_default(self):
        workspace = {
            "locales": {
                "locale": {"layers": {"Ignored": {"format": "mvt"}}},
                "cy": {"view": {"z": 8}},
            }
        }
        self.assertEqual(
            _locale(workspace, None),
            ("locale", {"layers": {}}),
        )
        self.assertEqual(
            _locale(workspace, "locale"),
            ("locale", {"layers": {}}),
        )
        name, named = _locale(workspace, "cy")
        self.assertEqual(name, "cy")
        self.assertEqual(named["layers"], {})
        self.assertEqual(named["view"], {"z": 8})
        self.assertEqual(named["key"], "cy")

    def test_proposal_mutations_reject_malformed_success_responses(self):
        cases = (
            (
                ["proposals", "create", "--base-revision", "rev-1", "--set", "/x=1"],
                ("POST", "/api/proposals"),
            ),
            (
                ["proposals", "decline", "proposal-1", "--confirm"],
                ("POST", "/api/proposals/proposal-1/decline"),
            ),
        )
        for arguments, route in cases:
            with self.subTest(arguments=arguments):
                routes = standard_routes()
                routes[route] = (200, {})
                with tempfile.TemporaryDirectory() as directory, JsonServer(
                    routes
                ) as server:
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(arguments, store)
                self.assertEqual(code, EXIT_CONNECTIVITY)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    json.loads(stderr)["code"],
                    "proposal.invalid_response",
                )

        routes = standard_routes()
        routes[("GET", "/api/proposals/proposal-1")] = (
            200,
            {
                "proposal": {
                    "id": "proposal-1",
                    "status": "pending",
                    "originalRevision": "rev-1",
                }
            },
        )
        routes[("POST", "/api/proposals/proposal-1/apply")] = (200, {})
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["proposals", "apply", "proposal-1", "--confirm"],
                store,
            )
        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["code"],
            "proposal.invalid_response",
        )

    def test_safety_evidence_commands_reject_empty_success_responses(self):
        cases = (
            (
                ["workspace", "get"],
                ("GET", "/api/workspace"),
                "workspace.invalid_response",
            ),
            (
                ["layers", "list"],
                ("GET", "/api/workspace"),
                "workspace.invalid_response",
            ),
            (
                ["validate"],
                ("POST", "/api/validate"),
                "validation.invalid_response",
            ),
            (
                [
                    "sql",
                    "test",
                    "--layer",
                    "Bus Stops",
                    "--expression",
                    "id",
                ],
                ("POST", "/api/sql/test"),
                "sql.invalid_response",
            ),
            (
                ["proposals", "show", "proposal-1"],
                ("GET", "/api/proposals/proposal-1"),
                "proposal.invalid_response",
            ),
            (
                ["proposals", "list"],
                ("GET", "/api/proposals"),
                "proposal.invalid_response",
            ),
            (
                ["schema"],
                ("GET", "/api/schema"),
                "schema.invalid_response",
            ),
            (
                ["rules"],
                ("GET", "/api/rules"),
                "rules.invalid_response",
            ),
            (
                ["catalog", "list"],
                ("GET", "/api/catalog"),
                "catalog.invalid_response",
            ),
            (
                ["icons", "list"],
                ("GET", "/api/icons"),
                "icons.invalid_response",
            ),
            (
                ["sql", "capabilities"],
                ("GET", "/api/sql/capabilities"),
                "sql.invalid_response",
            ),
            (
                ["auth", "status"],
                ("GET", "/api/auth/me"),
                "auth.invalid_response",
            ),
        )
        for arguments, route, error_code in cases:
            with self.subTest(arguments=arguments):
                routes = standard_routes()
                routes[route] = (200, {})
                with tempfile.TemporaryDirectory() as directory, JsonServer(
                    routes
                ) as server:
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(arguments, store)
                self.assertEqual(code, EXIT_CONNECTIVITY)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["code"], error_code)

    def test_xyz_commands_validate_success_response_contracts(self):
        valid_status = {
            "requestedGeneration": 2,
            "appliedGeneration": 2,
            "healthy": True,
        }
        valid_reload = {
            "requestedGeneration": 3,
            "status": {
                "requestedGeneration": 3,
                "appliedGeneration": 3,
                "healthy": True,
                "completed": True,
            },
        }
        for arguments, route, response, expected in (
            (
                ["xyz", "status"],
                ("GET", "/api/xyz/status"),
                valid_status,
                0,
            ),
            (
                ["xyz", "reload", "--confirm"],
                ("POST", "/api/xyz/reload"),
                valid_reload,
                0,
            ),
            (
                ["xyz", "status"],
                ("GET", "/api/xyz/status"),
                {},
                EXIT_CONNECTIVITY,
            ),
            (
                ["xyz", "reload", "--confirm"],
                ("POST", "/api/xyz/reload"),
                {},
                EXIT_CONNECTIVITY,
            ),
        ):
            with self.subTest(arguments=arguments, response=response):
                routes = standard_routes()
                routes[route] = (200, response)
                with tempfile.TemporaryDirectory() as directory, JsonServer(
                    routes
                ) as server:
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(arguments, store)
                self.assertEqual(code, expected, stderr)
                if expected:
                    self.assertEqual(stdout, "")
                    self.assertEqual(
                        json.loads(stderr)["code"],
                        "xyz.invalid_response",
                    )

    def test_missing_layer_and_rule_are_nonzero(self):
        routes = standard_routes()
        routes[("GET", "/api/rules")] = (
            200,
            {"rules": [{"id": "known", "category": "schema"}]},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            layer_code, _, layer_error = self.invoke(
                ["layers", "get", "Missing"],
                store,
            )
            rule_code, _, rule_error = self.invoke(
                ["explain-error", "missing.rule"],
                store,
            )
        self.assertEqual(layer_code, EXIT_VALIDATION)
        self.assertEqual(rule_code, EXIT_VALIDATION)
        self.assertEqual(json.loads(layer_error)["code"], "layer.not_found")
        self.assertEqual(json.loads(rule_error)["code"], "rules.not_found")

    @unittest.skipUnless(os.name == "posix", "POSIX token permissions required")
    def test_token_file_overrides_stored_credential_for_automation(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(
                directory,
                server.endpoint,
                token="wrong-token",
            )
            token_file = Path(directory) / "token"
            token_file.write_text("file-token\n", encoding="utf-8")
            os.chmod(token_file, 0o600)
            code, stdout, stderr = self.invoke(
                ["--token-file", str(token_file), "auth", "status"],
                store,
            )
            authenticated = [
                request
                for request in server.requests
                if request["path"] != "/api/public/identity"
            ]
        self.assertEqual(code, 0, stderr)
        self.assertTrue(authenticated)
        self.assertTrue(
            all(
                request["headers"].get("Authorization") == "Bearer file-token"
                for request in authenticated
            )
        )

    def test_malformed_state_returns_json_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            root.mkdir(mode=0o700)
            path = root / "profiles.json"
            path.write_text("{broken", encoding="utf-8")
            os.chmod(path, 0o600)
            code, stdout, stderr = self.invoke(["describe"], ConfigStore(root))
        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertFalse(payload["ok"])
        self.assertNotIn("Traceback", stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX token permissions required")
    def test_init_uses_private_token_file_and_persists_profile(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            token_file = Path(directory) / "token"
            token_file.write_text("init-token\n", encoding="utf-8")
            os.chmod(token_file, 0o600)
            store = ConfigStore(Path(directory) / "config")
            code, stdout, stderr = self.invoke(
                [
                    "init",
                    server.endpoint,
                    "--profile",
                    "local",
                    "--token-file",
                    str(token_file),
                ],
                store,
            )
            selected = store.selected_profile()
            token = store.token_for(selected)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(selected.name, "local")
        self.assertEqual(selected.instance_id, "instance-1")
        self.assertEqual(token, "init-token")
        self.assertTrue(json.loads(stdout)["compatible"])


if __name__ == "__main__":
    unittest.main()
