from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mapp_config_cli.cli import main, parser
from mapp_config_cli.config import ConfigStore, Profile
from mapp_config_cli.errors import (
    CliError,
    EXIT_AUTHENTICATION,
    EXIT_CONFLICT,
    EXIT_CONNECTIVITY,
    EXIT_INTERRUPTED,
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

    def test_doctor_reports_safe_readiness_and_advertised_capabilities(self):
        routes = standard_routes()
        secret = "doctor-secret-token"
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(
                directory,
                server.endpoint,
                token=secret,
            )
            code, stdout, stderr = self.invoke(["doctor"], store)
        self.assertEqual(code, 0, stderr)
        self.assertNotIn(secret, stdout + stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["healthy"])
        self.assertEqual(payload["profile"]["name"], "test")
        self.assertEqual(payload["credential"], {
            "available": True,
            "source": "credentialStore",
        })
        self.assertTrue(payload["configuration"]["profilesFile"]["private"])
        self.assertTrue(payload["configuration"]["credentialsFile"]["private"])
        self.assertTrue(payload["target"]["identityMatches"])
        self.assertEqual(payload["authentication"]["scopes"], ["full"])
        self.assertEqual(payload["workspace"]["key"], "demo")
        self.assertTrue(payload["capabilities"]["sql"]["advertised"])
        self.assertTrue(payload["capabilities"]["visual"]["test"])
        self.assertTrue(all(check["passed"] for check in payload["checks"]))

    def test_doctor_missing_credential_is_structured_and_secret_free(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            store.credentials_path.write_text("{}\n", encoding="utf-8")
            os.chmod(store.credentials_path, 0o600)
            code, stdout, stderr = self.invoke(["doctor"], store)
        self.assertEqual(code, EXIT_AUTHENTICATION)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "auth.credential_missing")
        self.assertNotIn("stored-token", stderr)

    def test_derived_layer_create_forwards_definition(self):
        captured = {}

        def create(request):
            captured.update(request["body"])
            return 201, {"derivedLayer": {
                "name": request["body"]["name"],
                "kind": request["body"]["kind"],
            }}

        routes = standard_routes()
        routes[("POST", "/api/derived-layers")] = create
        query = (
            "SELECT h3_id, geom_3857 FROM leeds.h3_cells "
            "JOIN leeds.definitive_paths ON true"
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            query_file = Path(directory) / "query.sql"
            query_file.write_text(query, encoding="utf-8")
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke([
                "derived-layers", "create", "paths_h3_r9",
                "--kind", "materialized",
                "--query-file", str(query_file),
                "--source", "leeds.h3_cells",
                "--source", "leeds.definitive_paths",
                "--id-column", "h3_id",
                "--geometry-column", "geom_3857",
            ], store)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(captured["query"], query)
        self.assertEqual(captured["kind"], "materialized")
        self.assertEqual(captured["sources"], [
            "leeds.h3_cells", "leeds.definitive_paths"
        ])
        self.assertEqual(
            json.loads(stdout)["derivedLayer"]["name"],
            "paths_h3_r9",
        )

    def test_derived_layer_replace_forwards_complete_confirmed_definition(self):
        captured = {}

        def replace(request):
            captured.update(request["body"])
            return 200, {"derivedLayer": {
                "name": "paths_h3_r9",
                "kind": "materialized",
                "replacedKind": "view",
            }}

        routes = standard_routes()
        routes[("POST", "/api/derived-layers/paths_h3_r9/replace")] = replace
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            query_file = Path(directory) / "query.sql"
            query_file.write_text(
                "SELECT cell_id, geom_3857 FROM leeds.h3_cells",
                encoding="utf-8",
            )
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke([
                "derived-layers", "replace", "paths_h3_r9",
                "--kind", "materialized",
                "--query-file", str(query_file),
                "--source", "leeds.h3_cells",
                "--id-column", "cell_id",
                "--geometry-column", "geom_3857",
                "--confirm",
            ], store)

        self.assertEqual(code, 0, stderr)
        self.assertTrue(captured["confirmed"])
        self.assertEqual(captured["kind"], "materialized")
        self.assertEqual(json.loads(stdout)["derivedLayer"]["replacedKind"], "view")

    def test_derived_layer_in_use_feedback_preserves_detected_uses(self):
        routes = standard_routes()
        routes[("POST", "/api/derived-layers/paths_h3_r9/drop")] = (
            409,
            {
                "error": "Delete blocked: derived layer is still in use. Nothing was changed.",
                "code": "derived_layer.in_use",
                "operation": "drop",
                "blocked": True,
                "dropped": False,
                "dependents": ["view reporting.paths"],
                "workspaceReferences": ["locale.layers.Paths"],
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["derived-layers", "drop", "paths_h3_r9", "--confirm"],
                store,
            )

        self.assertEqual(code, EXIT_CONFLICT)
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "derived_layer.in_use")
        self.assertEqual(payload["details"]["operation"], "drop")
        self.assertTrue(payload["details"]["blocked"])
        self.assertFalse(payload["details"]["dropped"])
        self.assertEqual(
            payload["details"]["workspaceReferences"],
            ["locale.layers.Paths"],
        )
        self.assertEqual(
            payload["details"]["dependents"],
            ["view reporting.paths"],
        )

    def test_derived_layer_refresh_requires_confirmation(self):
        with self.assertRaises(CliError):
            parser().parse_args([
                "derived-layers", "refresh", "paths_h3_r9"
            ])

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions required")
    def test_doctor_rejects_insecure_check_cache_before_network(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            store.checks_path.write_text("{}\n", encoding="utf-8")
            os.chmod(store.checks_path, 0o644)

            code, stdout, stderr = self.invoke(["doctor"], store)
            requests = list(server.requests)

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(requests, [])
        payload = json.loads(stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exitCode"], EXIT_CONNECTIVITY)
        self.assertEqual(payload["code"], "config.insecure_permissions")
        self.assertEqual(
            payload["details"]["nextAction"]["id"],
            "config.inspect_permissions",
        )
        diagnostic = payload["details"]["diagnostic"]
        self.assertEqual(diagnostic["mode"], "0644")
        self.assertTrue(diagnostic["path"].endswith("checks.json"))

    @unittest.skipUnless(os.name == "posix", "POSIX symbolic links required")
    def test_doctor_rejects_symlinked_check_cache_before_network(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            target = Path(directory) / "checks-target.json"
            target.write_text("{}\n", encoding="utf-8")
            os.chmod(target, 0o600)
            store.checks_path.symlink_to(target)

            code, stdout, stderr = self.invoke(["doctor"], store)
            requests = list(server.requests)

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(requests, [])
        payload = json.loads(stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exitCode"], EXIT_CONNECTIVITY)
        self.assertEqual(payload["code"], "config.symlink_rejected")
        self.assertEqual(
            payload["details"]["nextAction"]["id"],
            "config.inspect_permissions",
        )

    def test_doctor_unreachable_endpoint_recommends_endpoint_check(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.configured_store(directory, "http://127.0.0.1:1")
            code, stdout, stderr = self.invoke(
                ["--timeout", "0.1", "doctor"],
                store,
            )

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "api.unreachable")
        self.assertEqual(
            payload["details"]["nextAction"]["id"],
            "endpoint.check",
        )

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions required")
    def test_doctor_recommends_permissions_fix_for_an_unsafe_token_file(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            token_file = Path(directory) / "token"
            token_file.write_text("replacement-token", encoding="utf-8")
            os.chmod(token_file, 0o644)

            code, stdout, stderr = self.invoke(
                ["--token-file", str(token_file), "doctor"],
                store,
            )
            requests = list(server.requests)

        self.assertEqual(code, EXIT_AUTHENTICATION)
        self.assertEqual(stdout, "")
        self.assertEqual(requests, [])
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "config.insecure_permissions")
        self.assertEqual(
            payload["details"]["nextAction"]["id"],
            "config.inspect_permissions",
        )

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
                    "rev-1",
                    "--from-check",
                    "a" * 64,
                ]
            )
        with tempfile.TemporaryDirectory() as directory:
            local_store = ConfigStore(Path(directory) / "config")
            code, stdout, stderr = self.invoke(
                [
                    "proposals",
                    "create",
                    "--from-check",
                    "a" * 64,
                    "--set",
                    "/locale/view/z=12",
                ],
                local_store,
            )
            self.assertFalse(local_store.root.exists())
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["code"],
            "usage.conflicting_check_input",
        )

    def test_proposal_validation_failure_explains_sql_remediation_safely(self):
        pointer = "/locale/layers/Boolean Layer/infoj/0/fieldfx"
        routes = standard_routes()
        routes[("POST", "/api/proposals")] = (
            422,
            {
                "error": "SQL result type does not match the renderer.",
                "validation": {
                    "ruleId": "sql.result_type",
                    "pointer": pointer,
                    "expectedType": "bool",
                    "actualType": "text",
                },
            },
        )
        expression = "UPPER(private_boolean)"
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "proposals",
                    "create",
                    "--base-revision",
                    "rev-1",
                    "--set",
                    f'{pointer}="{expression}"',
                ],
                store,
            )

        self.assertEqual(code, EXIT_VALIDATION)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "proposal.validation_failed")
        self.assertEqual(payload["details"]["rejectedPointers"], [pointer])
        self.assertEqual(payload["details"]["ruleId"], "sql.result_type")
        self.assertEqual(payload["details"]["expectedType"], "bool")
        self.assertEqual(payload["details"]["actualType"], "text")
        remediation = payload["details"]["remediation"]
        self.assertEqual(remediation["command"], "config-cli sql test")
        self.assertEqual(remediation["arguments"]["layer"], "Boolean Layer")
        self.assertNotIn(expression, stderr)

    def test_proposal_check_previews_without_creating_and_separates_evidence(self):
        captured = {}

        def check(request):
            captured.update(request["body"])
            return 200, {
                "check": {
                    "valid": True,
                    "proposalCreated": False,
                    "checkFingerprint": "a" * 64,
                    "originalRevision": request["body"]["revision"],
                    "operations": request["body"]["operations"],
                    "diff": [{"path": "/locale/view/z", "old": 10, "value": 12}],
                    "warnings": ["Review map scale."],
                }
            }

        routes = standard_routes()
        routes[("POST", "/api/proposals/check")] = check
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "proposals", "check",
                    "--base-revision", "rev-1",
                    "--set", "/locale/view/z=12",
                    "--explanation", "Change only the zoom.",
                ],
                store,
            )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertFalse(payload["check"]["proposalCreated"])
        self.assertEqual(payload["validation"]["errors"], [])
        self.assertEqual(payload["validation"]["warnings"], ["Review map scale."])
        self.assertEqual(payload["nextActions"][0]["id"], "proposal.create")
        self.assertEqual(captured["revision"], "rev-1")
        self.assertEqual(captured["explanation"], "Change only the zoom.")

    def test_proposal_create_from_check_reuses_exact_cached_operations(self):
        fingerprint = "b" * 64
        routes = standard_routes()
        routes[("POST", "/api/proposals/check")] = (
            200,
            {"check": {
                "valid": True,
                "proposalCreated": False,
                "originalRevision": "rev-1",
                "checkFingerprint": fingerprint,
                "operations": [{"op": "set", "path": "/locale/view/z", "value": 12}],
                "diff": [{"path": "/locale/view/z", "old": 10, "value": 12}],
                "warnings": [],
            }},
        )
        captured = {}

        def create(request):
            captured.update(request["body"])
            return 201, {"proposal": {
                "id": "proposal-checked",
                "status": "pending",
                "originalRevision": "rev-1",
            }}

        routes[("POST", "/api/proposals")] = create
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            check_code, _, check_error = self.invoke(
                ["proposals", "check", "--base-revision", "rev-1", "--set", "/locale/view/z=12"],
                store,
            )
            code, stdout, stderr = self.invoke(
                ["proposals", "create", "--from-check", fingerprint], store
            )
        self.assertEqual(check_code, 0, check_error)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(captured["checkFingerprint"], fingerprint)
        self.assertEqual(captured["revision"], "rev-1")
        self.assertEqual(captured["operations"][0]["value"], 12)
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
        self.assertEqual({"approved": True}, apply_requests[0]["body"])

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
        for command, arguments in (
            ("workspace get", ["workspace", "get"]),
            ("layers effective", ["layers", "list"]),
            ("xyz reload", ["reload-xyz", "--confirm"]),
        ):
            with self.subTest(command=command):
                routes = standard_routes()
                routes[("GET", "/api/contract")][1]["commands"].remove(command)
                with tempfile.TemporaryDirectory() as directory, JsonServer(
                    routes
                ) as server:
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(arguments, store)
                self.assertEqual(code, EXIT_CONFLICT)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    json.loads(stderr)["code"],
                    "capability.missing",
                )

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

    def test_visual_command_can_fetch_returned_artifacts(self):
        routes = standard_routes()
        routes[("POST", "/api/visual-test")] = (
            200,
            {
                "plan": {"layer": "Bus Stops"},
                "visual": {
                    "passed": True,
                    "artifacts": {
                        "beforePage": "run-1/before-page.png",
                        "afterPage": "run-1/after-page.png",
                    },
                },
            },
        )
        routes[("GET", "/api/artifacts/run-1/before-page.png")] = (
            200,
            b"before",
            {"Content-Type": "image/png"},
        )
        routes[("GET", "/api/artifacts/run-1/after-page.png")] = (
            200,
            b"after",
            {"Content-Type": "image/png"},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            output = Path(directory) / "artifacts"
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "visual-test",
                    "--layer",
                    "Bus Stops",
                    "--artifact-dir",
                    str(output),
                ],
                store,
            )
            before = output / "run-1/before-page.png"
            after = output / "run-1/after-page.png"
            before_bytes = before.read_bytes()
            after_bytes = after.read_bytes()
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["localArtifacts"]["beforePage"], str(before))
        self.assertEqual(payload["localArtifacts"]["afterPage"], str(after))
        self.assertEqual(before_bytes, b"before")
        self.assertEqual(after_bytes, b"after")

    def test_proposal_candidate_visual_commands_bind_identity_and_route(self):
        captured: list[tuple[str, dict]] = []

        def preview(request):
            captured.append((request["path"], request["body"]))
            result = {
                "source": "candidate",
                "proposalId": "proposal-1",
                "candidateHash": "sha256:candidate",
                "plan": {
                    "layer": request["body"]["layer"],
                    "locale": request["body"].get("locale"),
                },
            }
            if not request["path"].endswith("/visual-plan"):
                result["visual"] = {"passed": True}
            return 200, result

        routes = standard_routes()
        for endpoint in ("visual-plan", "visual-test", "screenshot"):
            routes[("POST", f"/api/proposals/proposal-1/{endpoint}")] = preview
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            results = [
                self.invoke(
                    [
                        "proposals",
                        action,
                        "proposal-1",
                        "--layer",
                        "Bus Stops",
                        "--locale",
                        "cy",
                    ],
                    store,
                )
                for action in (
                    "preview-plan",
                    "preview-test",
                    "preview-screenshot",
                )
            ]
        for code, stdout, stderr in results:
            self.assertEqual(code, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["source"], "candidate")
            self.assertEqual(payload["proposalId"], "proposal-1")
            self.assertEqual(payload["candidateHash"], "sha256:candidate")
        self.assertEqual(
            [path for path, _ in captured],
            [
                "/api/proposals/proposal-1/visual-plan",
                "/api/proposals/proposal-1/visual-test",
                "/api/proposals/proposal-1/screenshot",
            ],
        )
        self.assertTrue(all(body == {"layer": "Bus Stops", "locale": "cy"} for _, body in captured))

    def test_candidate_visual_rejects_unbound_response_and_preserves_failure(self):
        routes = standard_routes()
        routes[("POST", "/api/proposals/proposal-1/visual-plan")] = (
            200,
            {
                "source": "live",
                "proposalId": "proposal-1",
                "candidateHash": "sha256:candidate",
                "plan": {"layer": "Bus Stops"},
            },
        )
        evidence = {
            "error": "Candidate visual verification did not pass.",
            "source": "candidate",
            "proposalId": "proposal-1",
            "candidateHash": "sha256:candidate",
            "plan": {"layer": "Bus Stops"},
            "visual": {"passed": False, "artifacts": ["/api/artifacts/report"]},
        }
        routes[("POST", "/api/proposals/proposal-1/visual-test")] = (422, evidence)
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            invalid = self.invoke(
                ["proposals", "preview-plan", "proposal-1", "--layer", "Bus Stops"],
                store,
            )
            failed = self.invoke(
                ["proposals", "preview-test", "proposal-1", "--layer", "Bus Stops"],
                store,
            )
        self.assertEqual(invalid[0], EXIT_CONNECTIVITY)
        self.assertEqual(
            json.loads(invalid[2])["code"],
            "visual.candidate_identity_invalid",
        )
        self.assertEqual(failed[0], EXIT_VISUAL)
        failed_payload = json.loads(failed[2])
        self.assertEqual(failed_payload["details"]["source"], "candidate")
        self.assertEqual(
            failed_payload["details"]["visual"]["artifacts"],
            ["/api/artifacts/report"],
        )

    def test_failed_visual_can_fetch_returned_artifacts_before_exiting(self):
        routes = standard_routes()
        routes[("POST", "/api/visual-test")] = (
            422,
            {
                "error": "Browser validation did not pass.",
                "plan": {"layer": "Bus Stops"},
                "visual": {
                    "passed": False,
                    "artifacts": {"afterPage": "run-2/after-page.png"},
                },
            },
        )
        routes[("GET", "/api/artifacts/run-2/after-page.png")] = (
            200,
            b"failed",
            {"Content-Type": "image/png"},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            output = Path(directory) / "artifacts"
            store = self.configured_store(directory, server.endpoint)
            code, _, stderr = self.invoke(
                [
                    "visual-test",
                    "--layer",
                    "Bus Stops",
                    "--artifact-dir",
                    str(output),
                ],
                store,
            )
            downloaded = output / "run-2/after-page.png"
            downloaded_bytes = downloaded.read_bytes()
        self.assertEqual(code, EXIT_VISUAL)
        payload = json.loads(stderr)
        self.assertEqual(
            payload["details"]["localArtifacts"]["afterPage"],
            str(downloaded),
        )
        self.assertEqual(downloaded_bytes, b"failed")

    def test_failed_proposal_preview_preserves_422_when_artifact_is_missing(self):
        routes = standard_routes()
        evidence = {
            "error": "Browser validation did not pass.",
            "source": "candidate",
            "proposalId": "proposal-1",
            "candidateHash": "sha256:candidate",
            "plan": {"layer": "Bus Stops"},
            "visual": {
                "passed": False,
                "artifacts": {
                    "beforePage": "run-3/before-page.png",
                    "afterPage": "run-3/after-page.png",
                },
            },
        }
        routes[("POST", "/api/proposals/proposal-1/screenshot")] = (422, evidence)
        routes[("GET", "/api/artifacts/run-3/before-page.png")] = (
            200,
            b"before",
            {"Content-Type": "image/png"},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            output = Path(directory) / "artifacts"
            store = self.configured_store(directory, server.endpoint)
            code, _, stderr = self.invoke(
                [
                    "proposals",
                    "preview-screenshot",
                    "proposal-1",
                    "--layer",
                    "Bus Stops",
                    "--artifact-dir",
                    str(output),
                ],
                store,
            )
            downloaded = output / "run-3/before-page.png"
            downloaded_bytes = downloaded.read_bytes()
        payload = json.loads(stderr)
        self.assertEqual(code, EXIT_VISUAL)
        self.assertEqual(payload["httpStatus"], 422)
        self.assertEqual(payload["error"], "Browser validation did not pass.")
        self.assertEqual(
            payload["details"]["localArtifacts"]["beforePage"],
            str(downloaded),
        )
        self.assertEqual(downloaded_bytes, b"before")
        self.assertEqual(
            payload["details"]["artifactDownloadErrors"][0]["artifact"],
            "afterPage",
        )
        self.assertEqual(
            payload["details"]["artifactDownloadErrors"][0]["httpStatus"],
            404,
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

    def test_layers_use_the_server_composed_effective_locale(self):
        captured = {}

        def effective_layers(request):
            captured["query"] = request["query"]
            return 200, {
                "revision": "rev-effective",
                "locale": "cy",
                "layers": {
                    "Inherited": {"format": "mvt", "display": True},
                    "Changed": {"format": "mvt", "display": False},
                },
            }

        routes = standard_routes()
        routes[("GET", "/api/layers")] = effective_layers
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["layers", "list", "--locale", "cy"],
                store,
            )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(captured["query"], "locale=cy")
        self.assertEqual(payload["revision"], "rev-effective")
        self.assertEqual(payload["locale"], "cy")
        self.assertIn("Inherited", payload["layers"])
        self.assertFalse(payload["layers"]["Changed"]["display"])

    def test_layers_can_be_filtered_by_exact_xyz_group(self):
        routes = standard_routes()
        routes[("GET", "/api/layers")] = (200, {
            "revision": "rev-1",
            "locale": "locale",
            "layers": {
                "Bus Stops": {"format": "mvt", "group": "Transport"},
                "Paths": {"format": "mvt", "group": "Transport"},
                "Boundaries": {"format": "mvt", "group": "Reference"},
                "Ungrouped": {"format": "mvt"},
            },
        })
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["layers", "list", "--group", "Transport"],
                store,
            )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(list(payload["layers"]), ["Bus Stops", "Paths"])

    def test_style_elements_reports_configured_effective_and_rendered_controls(self):
        routes = standard_routes()
        routes[("GET", "/api/layers")] = (200, {
            "revision": "rev-1",
            "locale": "locale",
            "layers": {
                "Bus Stops 2": {
                    "style": {
                        "default": {"icon": {"type": "dot"}},
                        "hover": {"display": True, "field": "stop_id"},
                        "opacitySlider": True,
                        "elements": [
                            "hover", "customPluginControl", "opacitySlider",
                        ],
                    },
                },
            },
        })
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["layers", "style-elements", "Bus Stops 2"],
                store,
            )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(
            payload["effectiveElements"],
            ["hover", "customPluginControl", "opacitySlider"],
        )
        self.assertEqual(payload["renderedElements"], ["hover", "opacitySlider"])
        self.assertFalse(payload["panelHidden"])

    def test_filters_reports_explicit_inferred_included_and_excluded_fields(self):
        routes = standard_routes()
        routes[("GET", "/api/layers")] = (200, {
            "revision": "rev-1",
            "locale": "locale",
            "layers": {
                "Bus Stops 2": {
                    "filter": {
                        "includeAll": True,
                        "exclude": ["direction"],
                        "viewport": True,
                    },
                    "infoj": [
                        {"title": "Town", "field": "town", "type": "text"},
                        {
                            "title": "Stop ID",
                            "field": "stop_id",
                            "type": "text",
                            "filter": {"type": "match"},
                        },
                        {
                            "title": "Object ID",
                            "field": "object_id",
                            "type": "integer",
                            "filter": True,
                        },
                        {"field": "direction", "type": "text", "filter": True},
                        {"field": "pin", "type": "pin"},
                    ],
                },
            },
        })
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["layers", "filters", "Bus Stops 2"],
                store,
            )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["viewport"])
        self.assertEqual(
            [(item["field"], item["type"]) for item in payload["filters"]],
            [("town", "like"), ("stop_id", "match"), ("object_id", "integer")],
        )
        self.assertEqual(payload["filters"][0]["source"], "includeAll")

    def test_unknown_locale_preserves_the_server_error_code(self):
        routes = standard_routes()
        routes[("GET", "/api/layers")] = (
            400,
            {"error": "Unknown locale: cy", "code": "locale.not_found"},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["layers", "list", "--locale", "cy"],
                store,
            )

        self.assertEqual(code, EXIT_VALIDATION)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["code"], "locale.not_found")

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
                ("GET", "/api/layers"),
                "layers.invalid_response",
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
                ["reload-xyz", "--confirm"],
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
            (
                ["reload-xyz", "--confirm"],
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

    def test_reload_xyz_alias_is_the_confirmed_xyz_reload_command(self):
        with self.assertRaises(CliError):
            parser().parse_args(["reload-xyz"])

        parsed = parser().parse_args(["reload-xyz", "--confirm"])

        self.assertEqual(parsed.command, "xyz")
        self.assertEqual(parsed.action, "reload")
        self.assertTrue(parsed.confirm)

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

    def test_setup_collects_details_without_leaking_token(self):
        routes = standard_routes()
        secret = "wizard-secret-token"
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = ConfigStore(Path(directory) / "config")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=["production", server.endpoint]),
                patch("mapp_config_cli.cli.getpass.getpass", return_value=secret),
            ):
                code = main(
                    ["setup"], stdout=stdout, stderr=stderr, store=store
                )
            selected = store.selected_profile()
            stored_token = store.token_for(selected)

        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(selected.name, "production")
        self.assertEqual(stored_token, secret)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["setupComplete"])
        self.assertEqual(payload["verification"]["workspaceKey"], "demo")
        self.assertEqual(payload["verification"]["actor"], "token:abc")
        self.assertEqual(payload["verification"]["revision"], "rev-1")
        self.assertNotIn(secret, stdout.getvalue())
        self.assertNotIn(secret, stderr.getvalue())
        self.assertIn("Profile name [default]: ", stderr.getvalue())
        self.assertIn("Configuration service URL: ", stderr.getvalue())
        self.assertNotIn("Profile name [default]: ", stdout.getvalue())

    def test_setup_shows_existing_profile_and_token_prefix_before_override(self):
        routes = standard_routes()
        old_secret = "old-token-secret"
        new_secret = "new-token-secret"
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = ConfigStore(Path(directory) / "config")
            store.save_profile(
                Profile(
                    "default",
                    server.endpoint,
                    "instance-1",
                    "1.0",
                    True,
                ),
                old_secret,
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch(
                    "builtins.input",
                    side_effect=["", "yes", server.endpoint, "yes"],
                ),
                patch(
                    "mapp_config_cli.cli.getpass.getpass",
                    return_value=new_secret,
                ),
            ):
                code = main(
                    ["setup"],
                    stdout=stdout,
                    stderr=stderr,
                    store=store,
                )
            selected, stored_token = store.connection("default")

        prompts = stderr.getvalue()
        self.assertEqual(code, 0, prompts)
        self.assertEqual(selected.endpoint, server.endpoint)
        self.assertEqual(stored_token, new_secret)
        self.assertIn("Current profile:", prompts)
        self.assertIn("Name: default", prompts)
        self.assertIn(f"Endpoint: {server.endpoint}", prompts)
        self.assertIn("Instance: instance-1", prompts)
        self.assertIn("Contract: 1.0", prompts)
        self.assertIn("Allow HTTP: yes", prompts)
        self.assertIn("Token prefix: old-to…", prompts)
        self.assertIn("Override this profile? [y/N]: ", prompts)
        self.assertIn(
            f"Checking target identity at {server.endpoint} (timeout 10s)…",
            prompts,
        )
        self.assertNotIn(old_secret, prompts)
        self.assertNotIn(new_secret, prompts)
        self.assertTrue(json.loads(stdout.getvalue())["setupComplete"])

    def test_setup_declining_existing_profile_override_preserves_it(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            previous = store.save_profile(
                Profile(
                    "default",
                    "https://config.example.com",
                    "instance-1",
                    "1.0",
                ),
                "old-token-secret",
            )
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=["", "no"]),
            ):
                code, stdout, stderr = self.invoke(["setup"], store)
            selected, token = store.connection("default")

        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr[stderr.index("{"):])["code"],
            "setup.replacement_cancelled",
        )
        self.assertEqual(selected, previous)
        self.assertEqual(token, "old-token-secret")

    def test_setup_requires_terminal_and_does_not_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            with patch("sys.stdin.isatty", return_value=False):
                code, stdout, stderr = self.invoke(["setup"], store)
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["code"], "setup.terminal_required")

    def test_setup_keyboard_interrupt_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=KeyboardInterrupt),
            ):
                code, stdout, stderr = self.invoke(["setup"], store)

        payload = json.loads(stderr[stderr.index("{"):])
        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertEqual(stdout, "")
        self.assertEqual(payload["code"], "client.interrupted")
        self.assertEqual(payload["exitCode"], EXIT_INTERRUPTED)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(store.list_profiles()["profiles"], {})

    def test_setup_closed_input_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config")
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=EOFError),
            ):
                code, stdout, stderr = self.invoke(["setup"], store)

        payload = json.loads(stderr[stderr.index("{"):])
        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertEqual(stdout, "")
        self.assertEqual(payload["code"], "client.input_closed")
        self.assertEqual(payload["exitCode"], EXIT_INTERRUPTED)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(store.list_profiles()["profiles"], {})

    def test_setup_rolls_back_profile_when_post_save_verification_fails(self):
        routes = standard_routes()
        routes[("GET", "/api/workspace")] = (200, {})
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = ConfigStore(Path(directory) / "config")
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=["production", server.endpoint]),
                patch("mapp_config_cli.cli.getpass.getpass", return_value="new-secret"),
            ):
                code, stdout, stderr = self.invoke(["setup"], store)
            profiles = store.list_profiles()
        self.assertNotEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertNotIn("production", profiles["profiles"])
        self.assertNotIn("new-secret", stderr)

    def test_setup_interrupt_during_verification_rolls_back_profile(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = ConfigStore(Path(directory) / "config")
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=["production", server.endpoint]),
                patch("mapp_config_cli.cli.getpass.getpass", return_value="new-secret"),
                patch(
                    "mapp_config_cli.cli._run_authenticated",
                    side_effect=KeyboardInterrupt,
                ),
            ):
                code, stdout, stderr = self.invoke(["setup"], store)
            profiles = store.list_profiles()

        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr[stderr.index("{"):])["code"],
            "client.interrupted",
        )
        self.assertNotIn("production", profiles["profiles"])
        self.assertNotIn("new-secret", stderr)

    def test_setup_does_not_overwrite_a_concurrent_profile_change(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = ConfigStore(Path(directory) / "config")
            store.save_profile(
                Profile("production", server.endpoint, "instance-1", "1.0", True),
                "old-token",
            )
            answers = iter(["production", server.endpoint, "yes"])
            concurrent: list[Profile] = []

            def answer_prompt():
                answer = next(answers)
                if answer == "yes":
                    concurrent.append(
                        store.save_profile(
                            Profile(
                                "production",
                                "https://concurrent.example.com",
                                "concurrent-instance",
                                "1.0",
                            ),
                            "concurrent-token",
                        )
                    )
                return answer

            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=answer_prompt),
                patch("mapp_config_cli.cli.getpass.getpass", return_value="new-token"),
            ):
                code, stdout, stderr = self.invoke(["setup", "--force"], store)

            selected, token = store.connection("production")

        self.assertEqual(code, EXIT_CONFLICT)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr[stderr.index("{"):])["code"],
            "profile.changed",
        )
        self.assertEqual(selected, concurrent[0])
        self.assertEqual(token, "concurrent-token")

    def test_setup_rejects_an_instance_change_after_confirmation(self):
        identity_calls = 0

        def changing_identity(_request):
            nonlocal identity_calls
            identity_calls += 1
            instance = "shown-instance" if identity_calls == 1 else "different-instance"
            return 200, {
                "instanceId": instance,
                "contractVersion": "1.0",
                "xyzVersion": "v4.23.4",
            }

        routes = standard_routes()
        routes[("GET", "/api/public/identity")] = changing_identity
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = ConfigStore(Path(directory) / "config")
            previous = store.save_profile(
                Profile("production", server.endpoint, "old-instance", "1.0", True),
                "old-token",
            )
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch(
                    "builtins.input",
                    side_effect=["production", server.endpoint, "yes"],
                ),
                patch("mapp_config_cli.cli.getpass.getpass", return_value="new-token"),
            ):
                code, stdout, stderr = self.invoke(["setup", "--force"], store)
            selected, token = store.connection("production")
            requests = list(server.requests)

        self.assertEqual(code, EXIT_CONFLICT)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr[stderr.index("{"):])["code"],
            "instance.confirmation_changed",
        )
        self.assertEqual((selected, token), (previous, "old-token"))
        self.assertEqual(identity_calls, 2)
        self.assertFalse(any(request["path"] == "/api/contract" for request in requests))
        self.assertTrue(
            all(request["headers"].get("Authorization") is None for request in requests)
        )

    def test_profile_show_and_confirmed_remove_are_secret_free(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint, token="profile-secret")
            code, stdout, stderr = self.invoke(["profiles", "show", "test"], store)
            remove_code, remove_stdout, remove_stderr = self.invoke(
                ["profiles", "remove", "test", "--confirm"], store
            )
        self.assertEqual(code, 0, stderr)
        self.assertTrue(json.loads(stdout)["profile"]["credentialAvailable"])
        self.assertNotIn("profile-secret", stdout + stderr)
        self.assertEqual(remove_code, 0, remove_stderr)
        self.assertFalse(json.loads(remove_stdout)["remoteTokenRevoked"])

    def test_interactive_profile_remove_keeps_stdout_as_json(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.configured_store(directory, "http://127.0.0.1:1")
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("builtins.input", return_value="yes"),
            ):
                code, stdout, stderr = self.invoke(
                    ["profiles", "remove", "test"],
                    store,
                )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["removed"], "test")
        self.assertFalse(payload["remoteTokenRevoked"])
        self.assertIn("Remove local profile 'test'? [y/N]: ", stderr)
        self.assertNotIn("Remove local profile", stdout)

    @unittest.skipUnless(os.name == "posix", "POSIX token permissions required")
    def test_auth_replace_verifies_then_rotates_without_leakage(self):
        routes = standard_routes()
        new_secret = "rotated-secret"
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint, token="old-secret")
            token_file = Path(directory) / "replacement.token"
            token_file.write_text(new_secret, encoding="utf-8")
            os.chmod(token_file, 0o600)
            code, stdout, stderr = self.invoke(
                ["auth", "replace", "--token-file", str(token_file)], store
            )
            selected = store.selected_profile("test")
            stored = store.token_for(selected)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stored, new_secret)
        self.assertTrue(json.loads(stdout)["credentialReplaced"])
        self.assertNotIn(new_secret, stdout + stderr)

    def test_completion_command_and_human_output_are_wired(self):
        with tempfile.TemporaryDirectory() as directory:
            code, script, stderr = self.invoke(
                ["completion", "bash"], ConfigStore(Path(directory) / "config")
            )
        self.assertEqual(code, 0, stderr)
        self.assertIn("complete -F _config_cli_complete config-cli", script)

        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            human_code, human, human_error = self.invoke(
                ["--output", "human", "doctor"], store
            )
        self.assertEqual(human_code, 0, human_error)
        self.assertIn("Status: healthy", human)
        self.assertFalse(human.lstrip().startswith("{"))

    def test_capability_discovery_and_operation_status_are_machine_readable(self):
        routes = standard_routes()
        routes[("GET", "/api/capabilities")] = (
            200,
            {
                "apiVersion": "1.0",
                "contractVersion": "1.0",
                "instanceId": "instance-1",
                "actions": [
                    {
                        "id": "proposals.check",
                        "method": "POST",
                        "path": "/api/proposals/check",
                        "risk": "read",
                        "inputSchema": {"type": "object"},
                    }
                ],
                "meta": {"requestId": "request-1"},
            },
        )
        routes[("GET", "/api/operations/op-1")] = (
            200,
            {
                "operation": {
                    "id": "op-1",
                    "kind": "visual.test",
                    "status": "succeeded",
                    "result": {"visual": {"passed": True}},
                },
                "meta": {"requestId": "request-2", "operationId": "op-1"},
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            capability_code, capability_out, capability_err = self.invoke(
                ["capabilities", "show", "proposals.check"],
                store,
            )
            operation_code, operation_out, operation_err = self.invoke(
                ["operations", "wait", "op-1"],
                store,
            )
        self.assertEqual(capability_code, 0, capability_err)
        self.assertEqual(
            "proposals.check",
            json.loads(capability_out)["action"]["id"],
        )
        self.assertEqual(operation_code, 0, operation_err)
        self.assertEqual(
            "succeeded",
            json.loads(operation_out)["operation"]["status"],
        )

    def test_operation_wait_returns_visual_exit_for_terminal_visual_failure(self):
        routes = standard_routes()
        routes[("GET", "/api/operations/op-failed")] = (
            200,
            {
                "operation": {
                    "id": "op-failed",
                    "kind": "visual.test",
                    "status": "failed",
                    "error": {"code": "visual.failed", "message": "No canvas."},
                }
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["operations", "wait", "op-failed"],
                store,
            )
        self.assertEqual(EXIT_VISUAL, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            "operation.failed",
            json.loads(stderr)["code"],
        )

    def test_input_extract_and_private_output_file_are_composable(self):
        routes = standard_routes()
        routes[("POST", "/api/visual-plan")] = (
            200,
            {
                "plan": {
                    "layer": "Bus Stops",
                    "locale": "locale",
                    "centre": [-1.5, 53.8],
                    "zoom": 12,
                }
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            input_path = Path(directory) / "visual.json"
            input_path.write_text(
                json.dumps({"centre": [-1.5, 53.8], "zoom": 12}),
                encoding="utf-8",
            )
            output_path = Path(directory) / "result.txt"
            code, stdout, stderr = self.invoke(
                [
                    "--input", str(input_path),
                    "--extract", "plan.zoom",
                    "--out", str(output_path),
                    "visual-plan",
                    "--layer", "Bus Stops",
                ],
                store,
            )
            mode = output_path.stat().st_mode & 0o777
            content = output_path.read_text(encoding="utf-8")
        self.assertEqual(code, 0, stderr)
        self.assertEqual("12\n", content)
        self.assertEqual(0o600, mode)
        self.assertEqual("0600", json.loads(stdout)["mode"])

    def test_device_authorization_replaces_token_only_after_verified_approval(self):
        routes = standard_routes()
        routes[("POST", "/api/auth/device")] = (
            201,
            {
                "deviceId": "opaque-device",
                "userCode": "ABCD-1234",
                "verificationUri": "/",
                "expiresIn": 60,
                "interval": 1,
                "scopes": ["inspect", "propose", "visual"],
            },
        )
        routes[("POST", "/api/auth/device/token")] = (
            200,
            {
                "status": "authorized",
                "token": "scoped-device-token",
                "record": {
                    "id": "token-device",
                    "expires": "2030-01-01T00:00:00Z",
                    "scopes": ["inspect", "propose", "visual"],
                },
            },
        )
        routes[("GET", "/api/auth/me")] = (
            200,
            {
                "actor": "token:device",
                "scopes": ["inspect", "propose", "visual"],
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(
                directory,
                server.endpoint,
                token="legacy-full-token",
            )
            code, stdout, stderr = self.invoke(
                ["auth", "device", "--no-browser"],
                store,
            )
            selected = store.selected_profile("test")
            stored = store.token_for(selected)
        self.assertEqual(code, 0, stderr)
        self.assertEqual("scoped-device-token", stored)
        self.assertNotIn("scoped-device-token", stdout + stderr)
        self.assertEqual(
            ["inspect", "propose", "visual"],
            json.loads(stdout)["scopes"],
        )


if __name__ == "__main__":
    unittest.main()
