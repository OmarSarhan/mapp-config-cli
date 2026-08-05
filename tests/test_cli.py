from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mapp_config_cli.cli import (
    _DEVICE_SCOPE_CHOICES,
    _SAFE_DEFAULT_DEVICE_SCOPES,
    MAX_LOCAL_FILE_BYTES,
    MAX_VISUAL_ARTIFACTS,
    _canonical_json_equal,
    _strict_json_file,
    _validate_requested_visual_evidence,
    input_object,
    main,
    parser,
)
from mapp_config_cli.client import ApiClient
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


DEVICE_SCOPES = (
    "inspect",
    "propose",
    "visual",
    "apply",
    "reload",
    "derive",
    "semantic:inspect",
    "semantic:source",
    "semantic:generate",
    "semantic:data",
    "semantic:propose",
    "semantic:apply",
    "semantic:admin",
)
SAFE_DEFAULT_DEVICE_SCOPES = (
    "inspect",
    "propose",
    "visual",
    "semantic:inspect",
)
WORKSPACE_CANDIDATE_HASH = "c" * 64


def map_extent_scope(locale: str = "Leeds") -> dict:
    return {
        "type": "workspace-map-extent",
        "locale": locale,
        "sourceView": {"lng": -1.549, "lat": 53.8, "z": 11},
        "scopeZoom": 10,
        "zoomOffset": -1,
        "viewport": {"width": 1920, "height": 1080, "tileSize": 256},
        "crs": "EPSG:4326",
        "envelopes": [{
            "west": -2.5,
            "south": 53.3,
            "east": -0.6,
            "north": 54.3,
        }],
        "selection": "intersects-output-geometry",
        "clipsGeometry": False,
        "guidance": (
            "This is an output-row guard only; it keeps complete output "
            "features intersecting the fixed extent. It is not a security "
            "boundary and does not scope source-side aggregates, clip "
            "geometry, or follow later map movements. Add the envelope inside "
            "source-side SQL before aggregation when metrics must be "
            "map-scoped."
        ),
    }


def materialization_probe() -> dict:
    return {
        "method": "postgresql-explain",
        "estimatedRows": 100,
        "planRowWidthBytes": 68,
        "rowOverheadBytes": 32,
        "safetyMultiplier": 1.2,
        "estimatedBytes": 12000,
        "maxEstimatedBytes": 1024 ** 3,
    }


def query_plan_limits() -> dict:
    return {
        "maxTotalCost": 50_000_000,
        "maxFinalRows": 10_000_000,
        "maxIntermediateRows": 100_000_000,
        "maxIntermediateBytes": 16 * 1024 ** 3,
        "maxJoinExpansionRatio": 1_000,
        "maxPlanNodes": 150,
        "maxPlanDepth": 32,
        "maxPlannedWorkers": 8,
    }


def query_guard() -> dict:
    return {
        "method": "postgresql-explain",
        "stages": [
            "postgresql-ast-guard",
            "postgresql-catalog-guard",
            "postgresql-explain",
        ],
        "limits": query_plan_limits(),
        "shapeLimits": {
            "maxJoins": 24,
            "maxCtes": 16,
            "maxSetOperations": 8,
            "maxGroupingSets": 64,
            "maxGeneratedRows": 1_000_000,
        },
        "h3": {
            "maxEstimatedScopeCells": 2_000_000,
            "maxEstimatedExpandedCells": 10_000_000,
            "scopeEstimateSafetyMultiplier": 1.5,
            "maxGridDistance": 25,
        },
        "errorCategories": {
            "invalid": {
                "code": "derived_layer.query_invalid",
                "httpStatus": 400,
            },
            "policy": {
                "code": "derived_layer.query_not_allowed",
                "httpStatus": 422,
            },
            "compute": {
                "code": "derived_layer.query_too_expensive",
                "httpStatus": 409,
            },
        },
    }


def query_plan_probe() -> dict:
    return {
        "method": "postgresql-explain",
        "estimatedTotalCost": 250_000.5,
        "estimatedFinalRows": 100_000,
        "maxIntermediateRows": 250_000,
        "maxIntermediateBytes": 128 * 1024 ** 2,
        "maxJoinExpansionRatio": 2.5,
        "planNodeCount": 12,
        "planDepth": 5,
        "plannedWorkers": 2,
        "recursivePlan": False,
        "h3Expansion": {
            "polygonToCellsCalls": 1,
            "resolutions": [9],
            "scopeAreaKm2": 124.25,
            "estimatedScopeCells": 250_000,
            "maxEstimatedScopeCells": 2_000_000,
            "safetyMultiplier": 1.5,
            "gridDiskCalls": 1,
            "maxGridDistance": 2,
            "maxAllowedGridDistance": 25,
            "expansionMultiplier": 19,
            "estimatedExpandedCells": 4_750_000,
            "maxEstimatedExpandedCells": 10_000_000,
        },
        "limits": query_plan_limits(),
    }


def workspace_apply_response(
    *,
    candidate_hash: str = WORKSPACE_CANDIDATE_HASH,
    target_candidate_hash: str | None = WORKSPACE_CANDIDATE_HASH,
) -> dict:
    fingerprint = "a" * 64
    proposal = {
        "id": "proposal-1",
        "status": "applied",
        "originalRevision": "rev-1",
        "appliedRevision": "rev-2",
        "candidateHash": candidate_hash,
        "appliedFingerprint": fingerprint,
        "requestedGeneration": 2,
    }
    reload_result = {
        "requestedGeneration": 2,
        "expectedWorkspaceFingerprint": fingerprint,
        "status": {
            "requestedGeneration": 2,
            "appliedGeneration": 2,
            "workspaceFingerprint": fingerprint,
            "healthy": True,
            "completed": True,
        },
    }
    target = {"proposalId": "proposal-1"}
    if target_candidate_hash is not None:
        target["candidateHash"] = target_candidate_hash
    return {
        "proposal": proposal,
        "reload": reload_result,
        "operation": {
            "id": "c" * 32,
            "kind": "proposal.apply",
            "status": "succeeded",
            "target": target,
            "result": {
                "proposal": dict(proposal),
                "reload": reload_result,
            },
            "error": None,
        },
    }


class CliTests(unittest.TestCase):
    def configured_store(
        self,
        directory: str,
        endpoint: str,
        *,
        instance_id: str = "instance-1",
        token: str = "stored-token",
        contract_version: str = "1.0",
    ) -> ConfigStore:
        store = ConfigStore(Path(directory) / "config")
        store.save_profile(
            Profile("test", endpoint, instance_id, contract_version),
            token,
        )
        return store

    def invoke(self, arguments, store):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(arguments, stdout=stdout, stderr=stderr, store=store)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_input_and_validation_reads_are_bounded_before_decoding(self):
        with tempfile.TemporaryDirectory() as directory:
            oversized = Path(directory) / "oversized.json"
            with oversized.open("wb") as stream:
                stream.truncate(MAX_LOCAL_FILE_BYTES + 1)

            arguments = parser().parse_args(
                ["--input", str(oversized), "describe"]
            )
            with self.assertRaises(CliError) as input_error:
                input_object(arguments)
            with self.assertRaises(CliError) as validation_error:
                _strict_json_file(str(oversized))

        self.assertEqual(input_error.exception.error_code, "input.too_large")
        self.assertEqual(
            validation_error.exception.error_code,
            "validation.file_too_large",
        )

    @unittest.skipUnless(
        os.name == "posix" and bool(getattr(os, "O_NOFOLLOW", 0)),
        "O_NOFOLLOW swap protection required",
    )
    def test_input_read_rejects_a_check_open_symlink_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "input.json"
            replacement = Path(directory) / "replacement.json"
            target.write_text("{}", encoding="utf-8")
            replacement.write_text('{"unexpected": true}', encoding="utf-8")
            arguments = parser().parse_args(
                ["--input", str(target), "describe"]
            )
            original_open = os.open
            swapped = False

            def swap_before_open(path, flags, *args, **kwargs):
                nonlocal swapped
                if not swapped and os.fspath(path) == str(target):
                    target.unlink()
                    target.symlink_to(replacement)
                    swapped = True
                return original_open(path, flags, *args, **kwargs)

            with (
                patch("mapp_config_cli.cli.os.open", side_effect=swap_before_open),
                self.assertRaises(CliError) as raised,
            ):
                input_object(arguments)

        self.assertTrue(swapped)
        self.assertEqual(raised.exception.error_code, "input.invalid_file")

    def test_validation_read_rejects_symlinks_and_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory) / "actual.json"
            linked = Path(directory) / "linked.json"
            invalid = Path(directory) / "invalid.json"
            actual.write_text("{}", encoding="utf-8")
            linked.symlink_to(actual)
            invalid.write_bytes(b"\xff")

            with self.assertRaises(CliError) as symlink_error:
                _strict_json_file(str(linked))
            with self.assertRaises(CliError) as encoding_error:
                _strict_json_file(str(invalid))

        self.assertEqual(
            symlink_error.exception.error_code,
            "validation.file_unavailable",
        )
        self.assertEqual(
            encoding_error.exception.error_code,
            "validation.invalid_json",
        )

    def test_canonical_json_equality_is_type_sensitive_and_finite(self):
        self.assertFalse(_canonical_json_equal(True, 1))
        self.assertFalse(_canonical_json_equal(1, 1.0))
        self.assertFalse(_canonical_json_equal(float("nan"), float("nan")))
        self.assertTrue(
            _canonical_json_equal(
                {"operation": {"value": True}},
                {"operation": {"value": True}},
            )
        )

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

    def test_describe_connects_without_inspect_and_reports_token_capabilities(self):
        routes = standard_routes()
        routes[("GET", "/api/connect")] = (
            200,
            {
                "authenticated": True,
                "actor": "token:visual-only",
                "tokenId": "visual-only",
                "scopes": ["visual"],
                "expires": "2030-01-01T00:00:00Z",
            },
        )
        requests = []

        def record_workspace(request):
            requests.append(request)
            return 500, {"error": "workspace must not be inspected"}

        routes[("GET", "/api/workspace")] = record_workspace
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(["describe"], store)

        self.assertEqual(code, 0, stderr)
        self.assertEqual([], requests)
        payload = json.loads(stdout)
        self.assertEqual(payload["actor"], "token:visual-only")
        self.assertEqual(payload["scopes"], ["visual"])
        self.assertEqual(payload["expires"], "2030-01-01T00:00:00Z")
        self.assertFalse(payload["workspaceAccessible"])
        self.assertIsNone(payload["workspaceKey"])
        self.assertIsNone(payload["revision"])

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

    def test_derived_layer_create_requires_confirmation_before_input_or_request(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke([
                "derived-layers", "create", "places",
                "--query-file", str(Path(directory) / "missing.sql"),
                "--source", "leeds.places",
                "--id-column", "id",
                "--geometry-column", "geom",
            ], store)

        self.assertEqual(EXIT_USAGE, code)
        self.assertEqual("", stdout)
        failure = json.loads(stderr)
        self.assertEqual("usage.invalid_arguments", failure["code"])
        self.assertIn("--confirm", failure["error"])
        self.assertEqual([], server.requests)

    def test_derived_layer_create_forwards_definition(self):
        captured = {}
        resolved_scope = map_extent_scope()

        def create(request):
            captured.update(request["body"])
            return 201, {"derivedLayer": {
                "name": request["body"]["name"],
                "kind": request["body"]["kind"],
                "spatialScope": resolved_scope,
                "materializationProbe": materialization_probe(),
                "queryPlanProbe": query_plan_probe(),
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
                "--confirm",
            ], store)
        self.assertEqual(code, 0, stderr)
        self.assertIs(captured["confirmed"], True)
        self.assertEqual(captured["query"], query)
        self.assertEqual(captured["kind"], "materialized")
        self.assertNotIn("background", captured)
        self.assertEqual(captured["sources"], [
            "leeds.h3_cells", "leeds.definitive_paths"
        ])
        self.assertEqual(captured["spatialScope"], {
            "type": "workspace-map-extent",
        })
        self.assertEqual(
            json.loads(stdout)["derivedLayer"]["name"],
            "paths_h3_r9",
        )
        self.assertEqual(
            json.loads(stdout)["derivedLayer"]["materializationProbe"],
            materialization_probe(),
        )
        self.assertEqual(
            json.loads(stdout)["derivedLayer"]["queryPlanProbe"],
            query_plan_probe(),
        )

    def test_derived_query_file_rejects_oversize_and_symlink_inputs(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            oversized = Path(directory) / "oversized.sql"
            with oversized.open("wb") as stream:
                stream.truncate(MAX_LOCAL_FILE_BYTES + 1)
            actual = Path(directory) / "actual.sql"
            linked = Path(directory) / "linked.sql"
            actual.write_text("SELECT id, geom FROM source", encoding="utf-8")
            linked.symlink_to(actual)
            store = self.configured_store(directory, server.endpoint)

            results = []
            for query_path in (oversized, linked):
                results.append(self.invoke(
                    [
                        "derived-layers", "create", "bounded_input",
                        "--query-file", str(query_path),
                        "--source", "source",
                        "--id-column", "id",
                        "--geometry-column", "geom",
                        "--confirm",
                    ],
                    store,
                ))
            mutation_requests = [
                request
                for request in server.requests
                if request["method"] == "POST"
            ]

        self.assertEqual(mutation_requests, [])
        self.assertEqual(results[0][0], EXIT_USAGE)
        self.assertEqual(
            json.loads(results[0][2])["code"],
            "derived_layer.query_file_too_large",
        )
        self.assertEqual(results[1][0], EXIT_USAGE)
        self.assertEqual(
            json.loads(results[1][2])["code"],
            "derived_layer.query_file",
        )

    def test_derived_layer_capabilities_validate_materialization_guard(self):
        guard = {
            "method": "postgresql-explain",
            "maxEstimatedBytes": 1024 ** 3,
            "rowOverheadBytes": 32,
            "safetyMultiplier": 1.2,
        }
        routes = standard_routes()
        routes[("GET", "/api/derived-layers/capabilities")] = (
            200,
            {
                "configured": True,
                "schema": "derived_layers",
                "kinds": ["view", "materialized"],
                "materializationGuard": guard,
                "queryGuard": query_guard(),
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["derived-layers", "capabilities"],
                store,
            )

        self.assertEqual(0, code, stderr)
        self.assertEqual(
            guard,
            json.loads(stdout)["materializationGuard"],
        )
        self.assertEqual(
            query_guard(),
            json.loads(stdout)["queryGuard"],
        )

    def test_derived_layer_capabilities_validate_hardened_query_guard(self):
        invalid_stages = query_guard()
        invalid_stages["stages"] = list(reversed(invalid_stages["stages"]))
        invalid_shape_limit = query_guard()
        invalid_shape_limit["shapeLimits"]["maxJoins"] = 0
        invalid_category = query_guard()
        invalid_category["errorCategories"]["policy"]["httpStatus"] = 409
        unknown_field = query_guard()
        unknown_field["futureGuard"] = True

        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            for guard in (
                invalid_stages,
                invalid_shape_limit,
                invalid_category,
                unknown_field,
            ):
                with self.subTest(guard=guard):
                    routes[("GET", "/api/derived-layers/capabilities")] = (
                        200,
                        {
                            "configured": True,
                            "schema": "derived_layers",
                            "kinds": ["view", "materialized"],
                            "queryGuard": guard,
                        },
                    )
                    code, stdout, stderr = self.invoke(
                        ["derived-layers", "capabilities"],
                        store,
                    )
                    self.assertEqual(EXIT_CONNECTIVITY, code)
                    self.assertEqual("", stdout)
                    self.assertEqual(
                        "derived_layer.invalid_response",
                        json.loads(stderr)["code"],
                    )

    def test_derived_layer_capabilities_accept_legacy_query_guard(self):
        guard = query_guard()
        for key in ("stages", "shapeLimits", "errorCategories"):
            guard.pop(key)
        routes = standard_routes()
        routes[("GET", "/api/derived-layers/capabilities")] = (
            200,
            {
                "configured": True,
                "schema": "derived_layers",
                "kinds": ["view", "materialized"],
                "queryGuard": guard,
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["derived-layers", "capabilities"],
                store,
            )

        self.assertEqual(0, code, stderr)
        self.assertEqual(guard, json.loads(stdout)["queryGuard"])

    def test_derived_layer_rejects_malformed_materialization_evidence(self):
        routes = standard_routes()
        routes[("GET", "/api/derived-layers/capabilities")] = (
            200,
            {
                "configured": True,
                "schema": "derived_layers",
                "kinds": ["view", "materialized"],
                "materializationGuard": {
                    "method": "postgresql-explain",
                    "maxEstimatedBytes": 1024 ** 3,
                    "rowOverheadBytes": 32,
                    "safetyMultiplier": True,
                },
            },
        )
        malformed_probe = materialization_probe()
        malformed_probe["estimatedBytes"] += 1
        routes[("POST", "/api/derived-layers/places/refresh")] = (
            200,
            {
                "derivedLayer": {
                    "name": "places",
                    "kind": "materialized",
                    "spatialScope": map_extent_scope(),
                    "materializationProbe": malformed_probe,
                }
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            capability_result = self.invoke(
                ["derived-layers", "capabilities"],
                store,
            )
            refresh_result = self.invoke(
                ["derived-layers", "refresh", "places", "--confirm"],
                store,
            )

        code, stdout, stderr = capability_result
        self.assertEqual(EXIT_CONNECTIVITY, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            "derived_layer.invalid_response",
            json.loads(stderr)["code"],
        )
        code, stdout, stderr = refresh_result
        self.assertEqual(EXIT_CONNECTIVITY, code)
        self.assertEqual("", stdout)
        failure = json.loads(stderr)
        self.assertEqual(
            "derived_layer.mutation_indeterminate",
            failure["code"],
        )
        self.assertFalse(
            failure["details"]["reconciliation"]["automaticRetry"]
        )

    def test_derived_layer_rejects_nonclosed_query_plan_evidence(self):
        malformed_guard = query_guard()
        malformed_guard["limits"]["futureLimit"] = 1
        malformed_probe = query_plan_probe()
        malformed_probe["futureMetric"] = 1
        oversized_h3_probe = query_plan_probe()
        oversized_h3_probe["h3Expansion"]["estimatedExpandedCells"] = (
            oversized_h3_probe["h3Expansion"]["maxEstimatedExpandedCells"]
            + 1
        )
        inconsistent_h3_probe = query_plan_probe()
        inconsistent_h3_probe["h3Expansion"]["expansionMultiplier"] = 18
        routes = standard_routes()
        routes[("GET", "/api/derived-layers/capabilities")] = (
            200,
            {
                "configured": True,
                "schema": "derived_layers",
                "kinds": ["view", "materialized"],
                "queryGuard": malformed_guard,
            },
        )
        routes[("POST", "/api/derived-layers/places/refresh")] = (
            200,
            {"derivedLayer": {
                "name": "places",
                "kind": "materialized",
                "spatialScope": map_extent_scope(),
                "queryPlanProbe": malformed_probe,
            }},
        )
        routes[("POST", "/api/derived-layers/expanded/refresh")] = (
            200,
            {"derivedLayer": {
                "name": "expanded",
                "kind": "materialized",
                "spatialScope": map_extent_scope(),
                "queryPlanProbe": oversized_h3_probe,
            }},
        )
        routes[("POST", "/api/derived-layers/inconsistent/refresh")] = (
            200,
            {"derivedLayer": {
                "name": "inconsistent",
                "kind": "materialized",
                "spatialScope": map_extent_scope(),
                "queryPlanProbe": inconsistent_h3_probe,
            }},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            results = (
                self.invoke(["derived-layers", "capabilities"], store),
                self.invoke(
                    ["derived-layers", "refresh", "places", "--confirm"],
                    store,
                ),
                self.invoke(
                    ["derived-layers", "refresh", "expanded", "--confirm"],
                    store,
                ),
                self.invoke(
                    [
                        "derived-layers", "refresh", "inconsistent",
                        "--confirm",
                    ],
                    store,
                ),
            )

        code, stdout, stderr = results[0]
        self.assertEqual(EXIT_CONNECTIVITY, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            "derived_layer.invalid_response",
            json.loads(stderr)["code"],
        )
        for code, stdout, stderr in results[1:]:
            self.assertEqual(EXIT_CONNECTIVITY, code)
            self.assertEqual("", stdout)
            failure = json.loads(stderr)
            self.assertEqual(
                "derived_layer.mutation_indeterminate",
                failure["code"],
            )
            self.assertFalse(
                failure["details"]["reconciliation"]["automaticRetry"]
            )

    def test_derived_layer_refresh_preserves_query_plan_probe(self):
        probe = query_plan_probe()
        routes = standard_routes()
        routes[("POST", "/api/derived-layers/places/refresh")] = (
            200,
            {"derivedLayer": {
                "name": "places",
                "kind": "materialized",
                "spatialScope": map_extent_scope(),
                "materializationProbe": materialization_probe(),
                "queryPlanProbe": probe,
            }},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["derived-layers", "refresh", "places", "--confirm"],
                store,
            )

        self.assertEqual(0, code, stderr)
        self.assertEqual(
            probe,
            json.loads(stdout)["derivedLayer"]["queryPlanProbe"],
        )

    def test_derived_layer_create_forwards_map_extent_scope(self):
        captured = {}
        resolved_scope = map_extent_scope()

        def create(request):
            captured.update(request["body"])
            return 201, {"derivedLayer": {
                "name": "bounded_places",
                "kind": "view",
                "spatialScope": resolved_scope,
            }}

        routes = standard_routes()
        routes[("POST", "/api/derived-layers")] = create
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            query_file = Path(directory) / "query.sql"
            query_file.write_text(
                "SELECT id, geom FROM leeds.places",
                encoding="utf-8",
            )
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke([
                "derived-layers", "create", "bounded_places",
                "--kind", "view",
                "--query-file", str(query_file),
                "--source", "leeds.places",
                "--id-column", "id",
                "--geometry-column", "geom",
                "--map-extent",
                "--locale", "Leeds",
                "--confirm",
            ], store)

        self.assertEqual(code, 0, stderr)
        self.assertEqual(captured["spatialScope"], {
            "type": "workspace-map-extent",
            "locale": "Leeds",
        })
        self.assertEqual(
            json.loads(stdout)["derivedLayer"]["spatialScope"],
            resolved_scope,
        )

    def test_derived_layer_map_extent_encodes_locale_and_preserves_plan(self):
        locale = "Leeds city / north & west"
        resolved_scope = map_extent_scope(locale)
        captured = {}

        def preview(request):
            captured["query"] = request["query"]
            return 200, {"spatialScope": resolved_scope}

        routes = standard_routes()
        routes[("GET", "/api/derived-layers/map-extent")] = preview
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke([
                "derived-layers", "map-extent", "--locale", locale,
            ], store)

        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            captured["query"],
            "locale=Leeds+city+%2F+north+%26+west",
        )
        self.assertEqual(json.loads(stdout)["spatialScope"], resolved_scope)

    def test_derived_layer_map_extent_accepts_clamped_low_zoom(self):
        resolved_scope = map_extent_scope()
        resolved_scope["sourceView"]["z"] = 0.5
        resolved_scope["scopeZoom"] = 0
        resolved_scope["zoomOffset"] = -0.5
        routes = standard_routes()
        routes[("GET", "/api/derived-layers/map-extent")] = (
            200,
            {"spatialScope": resolved_scope},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["derived-layers", "map-extent"],
                store,
            )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["spatialScope"], resolved_scope)

    def test_derived_layer_map_extent_rejects_malformed_plan(self):
        routes = standard_routes()
        routes[("GET", "/api/derived-layers/map-extent")] = (
            200,
            {"spatialScope": {"type": "workspace-map-extent"}},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["derived-layers", "map-extent"],
                store,
            )

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["code"],
            "derived_layer.invalid_response",
        )

    def test_derived_layer_map_extent_rejects_different_locale(self):
        routes = standard_routes()
        routes[("GET", "/api/derived-layers/map-extent")] = (
            200,
            {"spatialScope": map_extent_scope("Bradford")},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["derived-layers", "map-extent", "--locale", "Leeds"],
                store,
            )

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["code"],
            "derived_layer.invalid_response",
        )

    def test_derived_layer_list_preserves_resolved_map_extent(self):
        resolved_scope = map_extent_scope()
        routes = standard_routes()
        routes[("GET", "/api/derived-layers")] = (
            200,
            {"derivedLayers": [{
                "name": "bounded_places",
                "spatialScope": resolved_scope,
            }]},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["derived-layers", "list"],
                store,
            )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            json.loads(stdout)["derivedLayers"][0]["spatialScope"],
            resolved_scope,
        )

    def test_derived_layer_show_rejects_malformed_map_extent(self):
        routes = standard_routes()
        routes[("GET", "/api/derived-layers/bounded_places")] = (
            200,
            {"derivedLayer": {
                "name": "bounded_places",
                "spatialScope": {"type": "workspace-map-extent"},
            }},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["derived-layers", "show", "bounded_places"],
                store,
            )

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["code"],
            "derived_layer.invalid_response",
        )

    def test_derived_layer_show_rejects_a_different_returned_name(self):
        routes = standard_routes()
        routes[("GET", "/api/derived-layers/requested")] = (
            200,
            {"derivedLayer": {"name": "substituted"}},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["derived-layers", "show", "requested"],
                store,
            )

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["code"],
            "derived_layer.invalid_response",
        )

    def test_derived_layer_mutation_locale_selects_scope_without_compatibility_flag(self):
        captured = {}
        resolved_scope = map_extent_scope()

        def create(request):
            captured.update(request["body"])
            return 201, {"derivedLayer": {
                "name": "places",
                "kind": "view",
                "spatialScope": resolved_scope,
            }}

        routes = standard_routes()
        routes[("POST", "/api/derived-layers")] = create
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            query_file = Path(directory) / "query.sql"
            query_file.write_text(
                "SELECT id, geom FROM leeds.places",
                encoding="utf-8",
            )
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke([
                "derived-layers", "create", "places",
                "--kind", "view",
                "--query-file", str(query_file),
                "--source", "leeds.places",
                "--id-column", "id",
                "--geometry-column", "geom",
                "--locale", "Leeds",
                "--confirm",
            ], store)

        self.assertEqual(code, 0, stderr)
        self.assertEqual(captured["spatialScope"], {
            "type": "workspace-map-extent",
            "locale": "Leeds",
        })
        self.assertEqual(
            json.loads(stdout)["derivedLayer"]["spatialScope"],
            resolved_scope,
        )

    def test_derived_create_requires_resolved_scope(self):
        routes = standard_routes()
        routes[("POST", "/api/derived-layers")] = (
            201,
            {"derivedLayer": {
                "name": "places",
                "spatialScope": None,
            }},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            query_file = Path(directory) / "query.sql"
            query_file.write_text(
                "SELECT id, geom FROM leeds.places",
                encoding="utf-8",
            )
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke([
                "derived-layers", "create", "places",
                "--query-file", str(query_file),
                "--source", "leeds.places",
                "--id-column", "id",
                "--geometry-column", "geom",
                "--confirm",
            ], store)

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        failure = json.loads(stderr)
        self.assertEqual(
            failure["code"],
            "derived_layer.mutation_indeterminate",
        )
        self.assertFalse(
            failure["details"]["reconciliation"]["automaticRetry"],
        )

    def test_derived_layer_replace_forwards_complete_confirmed_definition(self):
        captured = {}
        resolved_scope = map_extent_scope()

        def replace(request):
            captured.update(request["body"])
            return 200, {"derivedLayer": {
                "name": "paths_h3_r9",
                "kind": "materialized",
                "replacedKind": "view",
                "spatialScope": resolved_scope,
                "materializationProbe": materialization_probe(),
                "queryPlanProbe": query_plan_probe(),
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
        self.assertEqual(captured["spatialScope"], {
            "type": "workspace-map-extent",
        })
        derived_layer = json.loads(stdout)["derivedLayer"]
        self.assertEqual(derived_layer["replacedKind"], "view")
        self.assertEqual(
            derived_layer["materializationProbe"],
            materialization_probe(),
        )
        self.assertEqual(
            derived_layer["queryPlanProbe"],
            query_plan_probe(),
        )

    def test_derived_layer_replace_forwards_map_extent_scope(self):
        captured = {}
        resolved_scope = map_extent_scope()

        def replace(request):
            captured.update(request["body"])
            return 200, {"derivedLayer": {
                "name": "paths_h3_r9",
                "kind": "materialized",
                "replacedKind": "view",
                "spatialScope": resolved_scope,
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
                "--map-extent",
                "--locale", "Leeds",
                "--confirm",
            ], store)

        self.assertEqual(code, 0, stderr)
        self.assertTrue(captured["confirmed"])
        self.assertEqual(captured["spatialScope"], {
            "type": "workspace-map-extent",
            "locale": "Leeds",
        })
        derived_layer = json.loads(stdout)["derivedLayer"]
        self.assertEqual(derived_layer["replacedKind"], "view")
        self.assertEqual(derived_layer["spatialScope"], resolved_scope)

    def test_derived_create_waits_for_background_operation(self):
        polls = {"count": 0}
        resolved_scope = map_extent_scope()

        def create(request):
            self.assertTrue(request["body"]["background"])
            self.assertEqual(request["body"]["spatialScope"], {
                "type": "workspace-map-extent",
            })
            return 202, {"operation": {
                "id": "derived-op-1",
                "kind": "derived-layer.create",
                "status": "running",
            }}

        def status(_request):
            polls["count"] += 1
            return 200, {"operation": {
                "id": "derived-op-1",
                "kind": "derived-layer.create",
                "status": "succeeded",
                "result": {"derivedLayer": {
                    "name": "slow_places",
                    "kind": "materialized",
                    "spatialScope": resolved_scope,
                }},
            }}

        routes = standard_routes()
        routes[("POST", "/api/derived-layers")] = create
        routes[("GET", "/api/operations/derived-op-1")] = status
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            query_file = Path(directory) / "query.sql"
            query_file.write_text(
                "SELECT id, geom FROM etl.places", encoding="utf-8"
            )
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke([
                "derived-layers", "create", "slow_places",
                "--kind", "materialized",
                "--query-file", str(query_file),
                "--source", "etl.places",
                "--id-column", "id",
                "--geometry-column", "geom",
                "--background",
                "--interval", "0.001",
                "--confirm",
            ], store)

        self.assertEqual(0, code, stderr)
        self.assertEqual(1, polls["count"])
        derived_layer = json.loads(stdout)["derivedLayer"]
        self.assertEqual("slow_places", derived_layer["name"])
        self.assertEqual(resolved_scope, derived_layer["spatialScope"])

    def test_derived_mutations_reject_wrong_names_after_sync_and_background_success(self):
        routes = standard_routes()
        routes[("POST", "/api/derived-layers/requested/refresh")] = (
            200,
            {"derivedLayer": {"name": "substituted"}},
        )
        routes[("POST", "/api/derived-layers/background/refresh")] = (
            202,
            {"operation": {
                "id": "derived-op-wrong-name",
                "kind": "derived-layer.refresh",
                "status": "running",
            }},
        )
        routes[("GET", "/api/operations/derived-op-wrong-name")] = (
            200,
            {"operation": {
                "id": "derived-op-wrong-name",
                "kind": "derived-layer.refresh",
                "status": "succeeded",
                "result": {"derivedLayer": {"name": "substituted"}},
            }},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            synchronous = self.invoke(
                ["derived-layers", "refresh", "requested", "--confirm"],
                store,
            )
            background = self.invoke(
                [
                    "derived-layers", "refresh", "background", "--confirm",
                    "--background", "--interval", "0.001",
                ],
                store,
            )

        self.assertEqual(synchronous[0], EXIT_CONNECTIVITY)
        self.assertEqual(synchronous[1], "")
        self.assertEqual(
            json.loads(synchronous[2])["code"],
            "derived_layer.mutation_indeterminate",
        )
        self.assertEqual(background[0], EXIT_CONNECTIVITY)
        self.assertEqual(background[1], "")
        background_error = json.loads(background[2])
        self.assertEqual(background_error["code"], "operation.poll_failed")
        self.assertEqual(
            background_error["details"]["cause"]["code"],
            "derived_layer.invalid_response",
        )
        self.assertEqual(
            background_error["details"]["operationId"],
            "derived-op-wrong-name",
        )

    def test_derived_create_surfaces_structured_background_guidance(self):
        reason = {
            "code": "custom_routine",
            "message": "The query calls an unapproved database routine.",
            "suggestedAction": (
                "Use an approved PostgreSQL, PostGIS, or H3 routine directly."
            ),
        }
        operation_error = {
            "error": "The derived query is not allowed.",
            "userMessage": (
                "This query uses a database object outside the derived-layer "
                "allowlist."
            ),
            "suggestedAction": reason["suggestedAction"],
            "code": "derived_layer.query_not_allowed",
            "category": "policy",
            "status": 422,
            "blocked": True,
            "stateUnchanged": True,
            "safeState": "No derived layer was created.",
            "reasons": [reason],
        }
        routes = standard_routes()
        routes[("POST", "/api/derived-layers")] = (
            202,
            {"operation": {
                "id": "derived-op-policy",
                "kind": "derived-layer.create",
                "status": "failed",
                "error": operation_error,
            }},
        )

        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            query_file = Path(directory) / "query.sql"
            query_file.write_text(
                "SELECT id, geom FROM etl.places", encoding="utf-8"
            )
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke([
                "derived-layers", "create", "unsafe_places",
                "--kind", "view",
                "--query-file", str(query_file),
                "--source", "etl.places",
                "--id-column", "id",
                "--geometry-column", "geom",
                "--background",
                "--confirm",
            ], store)

        self.assertEqual(EXIT_VALIDATION, code)
        self.assertEqual("", stdout)
        payload = json.loads(stderr)
        self.assertEqual("derived_layer.query_not_allowed", payload["code"])
        self.assertEqual(operation_error["userMessage"], payload["error"])
        preserved = payload["details"]["operation"]["error"]
        self.assertEqual(operation_error["suggestedAction"], preserved["suggestedAction"])
        self.assertEqual([reason], preserved["reasons"])

    def test_invalid_background_waits_stop_before_the_mutation_request(self):
        for option, value in (
            ("--wait-timeout", "0"),
            ("--wait-timeout", "-1"),
            ("--interval", "0"),
            ("--interval", "-0.5"),
        ):
            with self.subTest(option=option, value=value):
                routes = standard_routes()
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        [
                            "derived-layers", "refresh", "slow_places",
                            "--confirm", "--background", option, value,
                        ],
                        store,
                    )
                    mutation_requests = [
                        request
                        for request in server.requests
                        if request["method"] == "POST"
                    ]

                self.assertEqual(code, EXIT_USAGE)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    json.loads(stderr)["code"],
                    "operation.invalid_wait",
                )
                self.assertEqual(mutation_requests, [])

    def test_background_poll_failure_retains_reconciliation_identity(self):
        routes = standard_routes()
        routes[("POST", "/api/derived-layers/slow_places/refresh")] = (
            202,
            {"operation": {
                "id": "derived-op-poll",
                "kind": "derived-layer.refresh",
                "status": "running",
            }},
        )
        routes[("GET", "/api/operations/derived-op-poll")] = (
            503,
            {"error": "Polling is temporarily unavailable."},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            with patch("mapp_config_cli.cli.time.sleep", return_value=None):
                code, stdout, stderr = self.invoke(
                    [
                        "derived-layers", "refresh", "slow_places",
                        "--confirm", "--background", "--interval", "0.001",
                    ],
                    store,
                )

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "operation.poll_failed")
        self.assertEqual(payload["details"]["operationId"], "derived-op-poll")
        reconciliation = payload["details"]["reconciliation"]
        self.assertTrue(reconciliation["required"])
        self.assertFalse(reconciliation["automaticRetry"])
        self.assertEqual(
            reconciliation["commands"][0]["arguments"],
            ["derived-op-poll"],
        )

    def test_background_poll_interruption_retains_reconciliation_identity(self):
        routes = standard_routes()
        routes[("POST", "/api/derived-layers/slow_places/refresh")] = (
            202,
            {"operation": {
                "id": "derived-op-interrupted",
                "kind": "derived-layer.refresh",
                "status": "running",
            }},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            with patch(
                "mapp_config_cli.cli.time.sleep",
                side_effect=KeyboardInterrupt,
            ):
                code, stdout, stderr = self.invoke(
                    [
                        "derived-layers", "refresh", "slow_places",
                        "--confirm", "--background",
                    ],
                    store,
                )

        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "operation.poll_interrupted")
        self.assertEqual(
            payload["details"]["operationId"],
            "derived-op-interrupted",
        )
        self.assertTrue(payload["details"]["reconciliation"]["required"])

    def test_background_malformed_poll_retains_reconciliation_identity(self):
        operation_id = "derived-op-malformed"
        poll_responses = (
            {"operation": {"id": "different-operation", "status": "running"}},
            {
                "operation": {
                    "id": operation_id,
                    "kind": "derived-layer.refresh",
                    "status": "succeeded",
                    "result": [],
                }
            },
            {
                "operation": {
                    "id": operation_id,
                    "kind": "derived-layer.refresh",
                    "status": "succeeded",
                    "result": {},
                }
            },
        )
        for poll_response in poll_responses:
            with self.subTest(poll_response=poll_response):
                routes = standard_routes()
                routes[("POST", "/api/derived-layers/slow_places/refresh")] = (
                    202,
                    {"operation": {
                        "id": operation_id,
                        "kind": "derived-layer.refresh",
                        "status": "running",
                    }},
                )
                routes[("GET", f"/api/operations/{operation_id}")] = (
                    200,
                    poll_response,
                )
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(directory, server.endpoint)
                    with patch(
                        "mapp_config_cli.cli.time.sleep",
                        return_value=None,
                    ):
                        code, stdout, stderr = self.invoke(
                            [
                                "derived-layers", "refresh", "slow_places",
                                "--confirm", "--background", "--interval", "0.001",
                            ],
                            store,
                        )
                    mutation_requests = [
                        request
                        for request in server.requests
                        if request["method"] == "POST"
                    ]

                self.assertEqual(code, EXIT_CONNECTIVITY)
                self.assertEqual(stdout, "")
                payload = json.loads(stderr)
                self.assertEqual(payload["code"], "operation.poll_failed")
                self.assertEqual(payload["details"]["operationId"], operation_id)
                self.assertIn(
                    payload["details"]["cause"]["code"],
                    {
                        "operation.invalid_response",
                        "derived_layer.invalid_response",
                    },
                )
                reconciliation = payload["details"]["reconciliation"]
                self.assertTrue(reconciliation["required"])
                self.assertFalse(reconciliation["automaticRetry"])
                self.assertEqual(
                    reconciliation["commands"][0]["arguments"],
                    [operation_id],
                )
                self.assertEqual(len(mutation_requests), 1)

    def test_background_malformed_initial_operation_prohibits_resubmission(self):
        route = ("POST", "/api/derived-layers/slow_places/refresh")
        malformed_operations = (
            {"status": "running"},
            {"id": "", "status": "running"},
            {"id": 7, "status": "running"},
        )
        for operation in malformed_operations:
            with self.subTest(operation=operation):
                routes = standard_routes()
                routes[route] = (202, {"operation": operation})
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        [
                            "derived-layers", "refresh", "slow_places",
                            "--confirm", "--background",
                        ],
                        store,
                    )
                    mutation_requests = [
                        request
                        for request in server.requests
                        if (request["method"], request["path"]) == route
                    ]

                self.assertEqual(code, EXIT_CONNECTIVITY)
                self.assertEqual(stdout, "")
                failure = json.loads(stderr)
                self.assertEqual(
                    failure["code"],
                    "derived_layer.mutation_indeterminate",
                )
                self.assertEqual(
                    failure["details"]["cause"]["code"],
                    "operation.invalid_response",
                )
                self.assertFalse(
                    failure["details"]["reconciliation"]["automaticRetry"]
                )
                self.assertEqual(len(mutation_requests), 1)

    def test_derived_mutation_ambiguous_http_status_is_indeterminate(self):
        route = ("POST", "/api/derived-layers/slow_places/refresh")
        responses = (
            (
                408,
                {
                    "error": "Refresh response timed out after submission.",
                    "code": "derived_layer.refresh_timeout",
                },
            ),
            (
                307,
                {"error": "Refresh endpoint moved."},
                {"Location": "/moved"},
            ),
        )
        for response in responses:
            with self.subTest(status=response[0]):
                routes = standard_routes()
                routes[route] = response
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        [
                            "derived-layers", "refresh", "slow_places",
                            "--confirm",
                        ],
                        store,
                    )
                    attempts = [
                        request
                        for request in server.requests
                        if (request["method"], request["path"]) == route
                    ]

                self.assertEqual(code, EXIT_CONNECTIVITY)
                self.assertEqual(stdout, "")
                failure = json.loads(stderr)
                self.assertEqual(
                    failure["code"],
                    "derived_layer.mutation_indeterminate",
                )
                self.assertEqual(
                    failure["details"]["cause"]["httpStatus"],
                    response[0],
                )
                self.assertFalse(
                    failure["details"]["reconciliation"]["automaticRetry"]
                )
                self.assertEqual(len(attempts), 1)

    def test_derived_mutation_interruption_is_indeterminate(self):
        routes = standard_routes()
        original_request = ApiClient.request
        mutation_attempts = 0

        def interrupt_refresh(client, path, *args, **kwargs):
            nonlocal mutation_attempts
            if path == "/api/derived-layers/slow_places/refresh":
                mutation_attempts += 1
                raise KeyboardInterrupt
            return original_request(client, path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            with patch.object(ApiClient, "request", new=interrupt_refresh):
                code, stdout, stderr = self.invoke(
                    [
                        "derived-layers", "refresh", "slow_places",
                        "--confirm",
                    ],
                    store,
                )

        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertEqual(stdout, "")
        failure = json.loads(stderr)
        self.assertEqual(
            failure["code"],
            "derived_layer.mutation_indeterminate",
        )
        self.assertTrue(failure["details"]["interrupted"])
        self.assertFalse(
            failure["details"]["reconciliation"]["automaticRetry"]
        )
        self.assertEqual(mutation_attempts, 1)

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
                        "operations": request["body"]["operations"],
                        "explanation": request["body"].get("explanation"),
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
                    "explanation": request["body"].get("explanation"),
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

    def test_proposal_check_accepts_a_generated_explanation_when_omitted(self):
        captured = {}
        generated_explanation = "Set locale view zoom to 12."

        def check(request):
            captured.update(request["body"])
            return 200, {"check": {
                "valid": True,
                "proposalCreated": False,
                "checkFingerprint": "e" * 64,
                "originalRevision": request["body"]["revision"],
                "operations": request["body"]["operations"],
                "explanation": generated_explanation,
                "diff": [],
                "warnings": [],
            }}

        routes = standard_routes()
        routes[("POST", "/api/proposals/check")] = check
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "proposals", "check", "--base-revision", "rev-1",
                    "--set", "/locale/view/z=12",
                ],
                store,
            )
            cached = store.load_check(
                store.selected_profile("test"),
                "e" * 64,
            )

        self.assertEqual(code, 0, stderr)
        self.assertNotIn("explanation", captured)
        self.assertEqual(
            json.loads(stdout)["check"]["explanation"],
            generated_explanation,
        )
        self.assertEqual(cached["explanation"], generated_explanation)

    def test_direct_proposal_create_accepts_a_generated_explanation_when_omitted(self):
        captured = {}
        generated_explanation = "Set locale view zoom to 12."

        def create(request):
            captured.update(request["body"])
            return 201, {"proposal": {
                "id": "proposal-generated",
                "status": "pending",
                "originalRevision": request["body"]["revision"],
                "operations": request["body"]["operations"],
                "explanation": generated_explanation,
            }}

        routes = standard_routes()
        routes[("POST", "/api/proposals")] = create
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "proposals", "create", "--base-revision", "rev-1",
                    "--set", "/locale/view/z=12",
                ],
                store,
            )

        self.assertEqual(code, 0, stderr)
        self.assertNotIn("explanation", captured)
        self.assertEqual(
            json.loads(stdout)["proposal"]["explanation"],
            generated_explanation,
        )

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
                "explanation": "Set locale view zoom to 12.",
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
                "operations": request["body"]["operations"],
                "explanation": request["body"].get("explanation"),
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
        self.assertEqual(captured["explanation"], "Set locale view zoom to 12.")
        check_request = next(
            request
            for request in server.requests
            if request["path"] == "/api/proposals/check"
        )
        self.assertNotIn("explanation", check_request["body"])
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

    def test_proposal_create_rejects_a_returned_mismatched_check_fingerprint(self):
        fingerprint = "b" * 64
        routes = standard_routes()
        routes[("POST", "/api/proposals/check")] = (
            200,
            {"check": {
                "valid": True,
                "proposalCreated": False,
                "originalRevision": "rev-1",
                "checkFingerprint": fingerprint,
                "operations": [
                    {"op": "set", "path": "/locale/view/z", "value": 12}
                ],
                "explanation": "Set locale view zoom to 12.",
                "diff": [{"path": "/locale/view/z", "old": 10, "value": 12}],
                "warnings": [],
            }},
        )

        def create(request):
            return 201, {"proposal": {
                "id": "proposal-checked",
                "status": "pending",
                "originalRevision": request["body"]["revision"],
                "operations": request["body"]["operations"],
                "explanation": request["body"].get("explanation"),
                "checkFingerprint": "c" * 64,
            }}

        routes[("POST", "/api/proposals")] = create
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            check_code, _, check_error = self.invoke(
                [
                    "proposals", "check", "--base-revision", "rev-1",
                    "--set", "/locale/view/z=12",
                ],
                store,
            )
            code, stdout, stderr = self.invoke(
                ["proposals", "create", "--from-check", fingerprint],
                store,
            )

        self.assertEqual(check_code, 0, check_error)
        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["code"], "proposal.invalid_response")

    def test_proposal_check_rejects_a_substituted_explicit_explanation(self):
        def check(request):
            return 200, {"check": {
                "valid": True,
                "proposalCreated": False,
                "originalRevision": request["body"]["revision"],
                "operations": request["body"]["operations"],
                "explanation": "Change every field.",
                "checkFingerprint": "d" * 64,
                "diff": [],
            }}

        routes = standard_routes()
        routes[("POST", "/api/proposals/check")] = check
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "proposals", "check", "--base-revision", "rev-1",
                    "--set", "/locale/view/z=12",
                    "--explanation", "Change only the zoom.",
                ],
                store,
            )

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["code"], "proposal.invalid_response")

    def test_workspace_proposal_echoes_reject_bool_number_substitution(self):
        cases = (
            (
                "check",
                [
                    "proposals", "check", "--base-revision", "rev-1",
                    "--set", "/enabled=true", "--explanation", "Enable it.",
                ],
                ("POST", "/api/proposals/check"),
            ),
            (
                "create",
                [
                    "proposals", "create", "--base-revision", "rev-1",
                    "--set", "/enabled=true", "--explanation", "Enable it.",
                ],
                ("POST", "/api/proposals"),
            ),
        )
        for action, arguments, route in cases:
            with self.subTest(action=action):
                def substitute(request):
                    operations = json.loads(json.dumps(request["body"]["operations"]))
                    operations[0]["value"] = 1
                    if action == "check":
                        return 200, {"check": {
                            "valid": True,
                            "proposalCreated": False,
                            "originalRevision": request["body"]["revision"],
                            "operations": operations,
                            "explanation": request["body"]["explanation"],
                            "diff": [],
                        }}
                    return 201, {"proposal": {
                        "id": "proposal-bool-substitution",
                        "status": "pending",
                        "originalRevision": request["body"]["revision"],
                        "operations": operations,
                        "explanation": request["body"]["explanation"],
                    }}

                routes = standard_routes()
                routes[route] = substitute
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(arguments, store)

                self.assertEqual(code, EXIT_CONNECTIVITY)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    json.loads(stderr)["code"],
                    "proposal.invalid_response",
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
                    "candidateHash": WORKSPACE_CANDIDATE_HASH,
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
                    "candidateHash": WORKSPACE_CANDIDATE_HASH,
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
        self.assertEqual(error["code"], "proposal.apply_indeterminate")
        reconciliation = error["details"]["reconciliation"]
        self.assertTrue(reconciliation["required"])
        self.assertFalse(reconciliation["automaticRetry"])
        self.assertEqual(error["details"]["proposalId"], "proposal-1")
        cause = error["details"]["cause"]
        self.assertEqual(cause["httpStatus"], 504)
        self.assertTrue(cause["details"]["saved"])
        self.assertEqual(cause["details"]["revision"], "rev-2")
        self.assertEqual(cause["details"]["proposal"]["status"], "applied")
        self.assertEqual(
            cause["details"]["proposal"]["appliedRevision"],
            "rev-2",
        )
        self.assertEqual(len(apply_requests), 1)
        self.assertEqual({"approved": True}, apply_requests[0]["body"])

    def test_apply_http_408_is_indeterminate_and_is_not_retried(self):
        routes = standard_routes()
        routes[("GET", "/api/proposals/proposal-1")] = (
            200,
            {"proposal": {
                "id": "proposal-1",
                "status": "pending",
                "originalRevision": "rev-1",
                "candidateHash": WORKSPACE_CANDIDATE_HASH,
            }},
        )
        routes[("POST", "/api/proposals/proposal-1/apply")] = (
            408,
            {
                "error": "The apply request timed out after it was submitted.",
                "code": "proposal.apply_timeout",
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
        self.assertEqual(error["code"], "proposal.apply_indeterminate")
        self.assertEqual(error["details"]["cause"]["httpStatus"], 408)
        self.assertFalse(
            error["details"]["reconciliation"]["automaticRetry"]
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
                    "candidateHash": WORKSPACE_CANDIDATE_HASH,
                }
            },
        )
        fingerprint = "a" * 64
        applied_proposal = {
            "id": "proposal-1",
            "status": "applied",
            "originalRevision": "rev-1",
            "appliedRevision": "rev-2",
            "candidateHash": WORKSPACE_CANDIDATE_HASH,
            "appliedFingerprint": fingerprint,
            "requestedGeneration": 2,
        }
        reload_result = {
            "requestedGeneration": 2,
            "expectedWorkspaceFingerprint": fingerprint,
            "status": {
                "requestedGeneration": 2,
                "appliedGeneration": 2,
                "workspaceFingerprint": fingerprint,
                "healthy": True,
                "completed": True,
            },
        }
        routes[("POST", "/api/proposals/proposal-1/apply")] = (
            200,
            {
                "proposal": applied_proposal,
                "reload": reload_result,
                "operation": {
                    "id": "c" * 32,
                    "kind": "proposal.apply",
                    "status": "succeeded",
                    "target": {
                        "proposalId": "proposal-1",
                        "candidateHash": WORKSPACE_CANDIDATE_HASH,
                    },
                    "result": {
                        "proposal": applied_proposal,
                        "reload": reload_result,
                    },
                    "error": None,
                },
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

    def test_apply_requires_and_binds_a_lowercase_candidate_hash(self):
        fetched_cases = (None, "A" * 64)
        for candidate_hash in fetched_cases:
            with self.subTest(fetched_candidate_hash=candidate_hash):
                proposal = {
                    "id": "proposal-1",
                    "status": "pending",
                    "originalRevision": "rev-1",
                }
                if candidate_hash is not None:
                    proposal["candidateHash"] = candidate_hash
                routes = standard_routes()
                routes[("GET", "/api/proposals/proposal-1")] = (
                    200,
                    {"proposal": proposal},
                )
                routes[("POST", "/api/proposals/proposal-1/apply")] = (
                    200,
                    workspace_apply_response(),
                )
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        ["proposals", "apply", "proposal-1", "--confirm"],
                        store,
                    )
                    apply_requests = [
                        request
                        for request in server.requests
                        if request["path"].endswith("/apply")
                    ]

                self.assertEqual(code, EXIT_CONNECTIVITY)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    json.loads(stderr)["code"],
                    "proposal.invalid_response",
                )
                self.assertEqual(apply_requests, [])

        response_cases = (
            workspace_apply_response(candidate_hash="d" * 64),
            workspace_apply_response(target_candidate_hash=None),
            workspace_apply_response(target_candidate_hash="d" * 64),
        )
        for response in response_cases:
            with self.subTest(response=response):
                routes = standard_routes()
                routes[("GET", "/api/proposals/proposal-1")] = (
                    200,
                    {"proposal": {
                        "id": "proposal-1",
                        "status": "pending",
                        "originalRevision": "rev-1",
                        "candidateHash": WORKSPACE_CANDIDATE_HASH,
                    }},
                )
                routes[("POST", "/api/proposals/proposal-1/apply")] = (
                    200,
                    response,
                )
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        ["proposals", "apply", "proposal-1", "--confirm"],
                        store,
                    )

                self.assertEqual(code, EXIT_CONNECTIVITY)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    json.loads(stderr)["code"],
                    "proposal.apply_indeterminate",
                )

    def test_apply_durable_result_uses_type_sensitive_json_binding(self):
        response = workspace_apply_response()
        response["proposal"]["requestedGeneration"] = 1
        response["operation"]["result"]["proposal"][
            "requestedGeneration"
        ] = True
        routes = standard_routes()
        routes[("GET", "/api/proposals/proposal-1")] = (
            200,
            {"proposal": {
                "id": "proposal-1",
                "status": "pending",
                "originalRevision": "rev-1",
                "candidateHash": WORKSPACE_CANDIDATE_HASH,
            }},
        )
        routes[("POST", "/api/proposals/proposal-1/apply")] = (
            200,
            response,
        )
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
            "proposal.apply_indeterminate",
        )

    def test_apply_partial_success_is_indeterminate_and_is_not_retried(self):
        routes = standard_routes()
        routes[("GET", "/api/proposals/proposal-1")] = (
            200,
            {
                "proposal": {
                    "id": "proposal-1",
                    "status": "pending",
                    "originalRevision": "rev-1",
                    "candidateHash": WORKSPACE_CANDIDATE_HASH,
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
                if request["method"] == "POST"
                and request["path"] == "/api/proposals/proposal-1/apply"
            ]

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        error = json.loads(stderr)
        self.assertEqual(error["code"], "proposal.apply_indeterminate")
        self.assertFalse(error["details"]["reconciliation"]["automaticRetry"])
        self.assertEqual(len(apply_requests), 1)

    def test_apply_redirect_is_indeterminate_and_is_not_retried(self):
        routes = standard_routes()
        routes[("GET", "/api/proposals/proposal-1")] = (
            200,
            {
                "proposal": {
                    "id": "proposal-1",
                    "status": "pending",
                    "originalRevision": "rev-1",
                    "candidateHash": WORKSPACE_CANDIDATE_HASH,
                }
            },
        )
        routes[("POST", "/api/proposals/proposal-1/apply")] = (
            307,
            {"error": "Apply endpoint moved."},
            {"Location": "/moved"},
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
        self.assertEqual(error["code"], "proposal.apply_indeterminate")
        self.assertEqual(error["details"]["cause"]["httpStatus"], 307)
        self.assertFalse(error["details"]["reconciliation"]["automaticRetry"])
        self.assertEqual(len(apply_requests), 1)

    def test_apply_interruption_is_indeterminate_and_is_not_retried(self):
        routes = standard_routes()
        routes[("GET", "/api/proposals/proposal-1")] = (
            200,
            {
                "proposal": {
                    "id": "proposal-1",
                    "status": "pending",
                    "originalRevision": "rev-1",
                    "candidateHash": WORKSPACE_CANDIDATE_HASH,
                }
            },
        )
        original_request = ApiClient.request
        apply_attempts = 0

        def interrupt_apply(client, path, *args, **kwargs):
            nonlocal apply_attempts
            if path == "/api/proposals/proposal-1/apply":
                apply_attempts += 1
                raise KeyboardInterrupt
            return original_request(client, path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            with patch.object(ApiClient, "request", new=interrupt_apply):
                code, stdout, stderr = self.invoke(
                    ["proposals", "apply", "proposal-1", "--confirm"],
                    store,
                )

        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertEqual(stdout, "")
        error = json.loads(stderr)
        self.assertEqual(error["code"], "proposal.apply_indeterminate")
        self.assertTrue(error["details"]["interrupted"])
        self.assertFalse(error["details"]["reconciliation"]["automaticRetry"])
        self.assertEqual(apply_attempts, 1)

    def test_apply_preserves_a_known_server_rejection(self):
        routes = standard_routes()
        routes[("GET", "/api/proposals/proposal-1")] = (
            200,
            {"proposal": {
                "id": "proposal-1",
                "status": "pending",
                "originalRevision": "rev-1",
                "candidateHash": WORKSPACE_CANDIDATE_HASH,
            }},
        )
        routes[("POST", "/api/proposals/proposal-1/apply")] = (
            409,
            {
                "error": "Proposal cannot be applied.",
                "code": "proposal.blocked",
                "stateUnchanged": True,
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["proposals", "apply", "proposal-1", "--confirm"],
                store,
            )

        self.assertEqual(code, EXIT_CONFLICT)
        self.assertEqual(stdout, "")
        error = json.loads(stderr)
        self.assertEqual(error["code"], "proposal.blocked")
        self.assertTrue(error["details"]["stateUnchanged"])

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
            root_mode = output.stat().st_mode & 0o777
            run_mode = before.parent.stat().st_mode & 0o777
            before_mode = before.stat().st_mode & 0o777
            after_mode = after.stat().st_mode & 0o777
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["localArtifacts"]["beforePage"], str(before))
        self.assertEqual(payload["localArtifacts"]["afterPage"], str(after))
        self.assertEqual(before_bytes, b"before")
        self.assertEqual(after_bytes, b"after")
        self.assertEqual(root_mode, 0o700)
        self.assertEqual(run_mode, 0o700)
        self.assertEqual(before_mode, 0o600)
        self.assertEqual(after_mode, 0o600)

    def test_explicit_artifact_export_requires_a_nonempty_artifact_map(self):
        cases = (
            (
                ["visual-test", "--layer", "Bus Stops"],
                ("POST", "/api/visual-test"),
                {
                    "plan": {"layer": "Bus Stops"},
                    "visual": {"passed": True},
                },
            ),
            (
                ["visual-test", "--layer", "Bus Stops"],
                ("POST", "/api/visual-test"),
                {
                    "plan": {"layer": "Bus Stops"},
                    "visual": {"passed": True, "artifacts": []},
                },
            ),
            (
                [
                    "proposals", "preview-test", "proposal-1",
                    "--layer", "Bus Stops",
                ],
                ("POST", "/api/proposals/proposal-1/visual-test"),
                {
                    "source": "candidate",
                    "proposalId": "proposal-1",
                    "candidateHash": "candidate",
                    "plan": {"layer": "Bus Stops"},
                    "visual": {"passed": True, "artifacts": {}},
                },
            ),
        )
        for arguments, route, response in cases:
            with self.subTest(arguments=arguments, response=response):
                routes = standard_routes()
                routes[route] = (200, response)
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    output = Path(directory) / "artifacts"
                    store = self.configured_store(directory, server.endpoint)
                    without_export = self.invoke(arguments, store)
                    with_export = self.invoke(
                        [*arguments, "--artifact-dir", str(output)],
                        store,
                    )
                    artifact_requests = [
                        request
                        for request in server.requests
                        if request["path"].startswith("/api/artifacts/")
                    ]
                    output_exists = output.exists()

                self.assertEqual(without_export[0], 0, without_export[2])
                self.assertEqual(with_export[0], EXIT_CONNECTIVITY)
                self.assertEqual(with_export[1], "")
                self.assertEqual(
                    json.loads(with_export[2])["code"],
                    "visual.artifacts_unavailable",
                )
                self.assertEqual(artifact_requests, [])
                self.assertFalse(output_exists)

    def test_visual_artifact_export_rejects_excessive_artifact_count(self):
        routes = standard_routes()
        routes[("POST", "/api/visual-test")] = (
            200,
            {
                "plan": {"layer": "Bus Stops"},
                "visual": {
                    "passed": True,
                    "artifacts": {
                        f"evidence{index}": f"run/{index}.png"
                        for index in range(MAX_VISUAL_ARTIFACTS + 1)
                    },
                },
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            output = Path(directory) / "artifacts"
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "visual-test", "--layer", "Bus Stops",
                    "--artifact-dir", str(output),
                ],
                store,
            )
            artifact_requests = [
                request
                for request in server.requests
                if request["path"].startswith("/api/artifacts/")
            ]
            output_exists = output.exists()

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        error = json.loads(stderr)
        self.assertEqual(error["code"], "visual.artifact_count_exceeded")
        self.assertEqual(
            error["details"]["maxArtifacts"],
            MAX_VISUAL_ARTIFACTS,
        )
        self.assertEqual(artifact_requests, [])
        self.assertFalse(output_exists)

    def test_visual_artifact_total_limit_fails_without_partial_writes(self):
        routes = standard_routes()
        routes[("POST", "/api/visual-test")] = (
            200,
            {
                "plan": {"layer": "Bus Stops"},
                "visual": {
                    "passed": True,
                    "artifacts": {
                        "first": "run/first.png",
                        "second": "run/second.png",
                    },
                },
            },
        )
        routes[("GET", "/api/artifacts/run/first.png")] = (
            200,
            b"abc",
            {"Content-Type": "image/png"},
        )
        routes[("GET", "/api/artifacts/run/second.png")] = (
            200,
            b"def",
            {"Content-Type": "image/png"},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            output = Path(directory) / "artifacts"
            store = self.configured_store(directory, server.endpoint)
            with patch(
                "mapp_config_cli.cli.MAX_VISUAL_ARTIFACT_TOTAL_BYTES",
                5,
            ):
                code, stdout, stderr = self.invoke(
                    [
                        "visual-test", "--layer", "Bus Stops",
                        "--artifact-dir", str(output),
                    ],
                    store,
                )
            output_exists = output.exists()
            artifact_requests = [
                request["path"]
                for request in server.requests
                if request["path"].startswith("/api/artifacts/")
            ]

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["code"],
            "visual.artifact_total_too_large",
        )
        self.assertEqual(
            artifact_requests,
            [
                "/api/artifacts/run/first.png",
                "/api/artifacts/run/second.png",
            ],
        )
        self.assertFalse(output_exists)

    def test_visual_artifacts_reject_unsafe_server_paths(self):
        unsafe_paths = (
            "../outside.png",
            "/absolute.png",
            "C:/drive.png",
            "run\\windows.png",
            "run//empty.png",
            "run/./dot.png",
        )
        for artifact_path in unsafe_paths:
            with self.subTest(path=artifact_path):
                routes = standard_routes()
                routes[("POST", "/api/visual-test")] = (
                    200,
                    {
                        "plan": {"layer": "Bus Stops"},
                        "visual": {
                            "passed": True,
                            "artifacts": {"afterPage": artifact_path},
                        },
                    },
                )
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    output = Path(directory) / "artifacts"
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        [
                            "visual-test", "--layer", "Bus Stops",
                            "--artifact-dir", str(output),
                        ],
                        store,
                    )
                    artifact_requests = [
                        request
                        for request in server.requests
                        if request["path"].startswith("/api/artifacts/")
                    ]

                self.assertEqual(code, EXIT_CONNECTIVITY)
                self.assertEqual(stdout, "")
                error = json.loads(stderr)
                self.assertEqual(error["code"], "visual.artifact_path_invalid")
                self.assertEqual(artifact_requests, [])

    @unittest.skipUnless(os.name == "posix", "POSIX path safety required")
    def test_visual_artifacts_do_not_overwrite_files_or_follow_symlinks(self):
        routes = standard_routes()
        routes[("POST", "/api/visual-test")] = (
            200,
            {
                "plan": {"layer": "Bus Stops"},
                "visual": {
                    "passed": True,
                    "artifacts": {"afterPage": "run/after-page.png"},
                },
            },
        )
        routes[("GET", "/api/artifacts/run/after-page.png")] = (
            200,
            b"replacement",
            {"Content-Type": "image/png"},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            output = Path(directory) / "artifacts"
            target = output / "run/after-page.png"
            target.parent.mkdir(parents=True, mode=0o700)
            os.chmod(output, 0o700)
            os.chmod(target.parent, 0o700)
            target.write_bytes(b"sentinel")
            os.chmod(target, 0o600)
            store = self.configured_store(directory, server.endpoint)

            code, stdout, stderr = self.invoke(
                [
                    "visual-test", "--layer", "Bus Stops",
                    "--artifact-dir", str(output),
                ],
                store,
            )
            retained = target.read_bytes()

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["code"], "visual.artifact_exists")
        self.assertEqual(retained, b"sentinel")

    @unittest.skipUnless(os.name == "posix", "POSIX path safety required")
    def test_visual_artifacts_reject_a_symlinked_directory_component(self):
        routes = standard_routes()
        routes[("POST", "/api/visual-test")] = (
            200,
            {
                "plan": {"layer": "Bus Stops"},
                "visual": {
                    "passed": True,
                    "artifacts": {"afterPage": "run/after-page.png"},
                },
            },
        )
        routes[("GET", "/api/artifacts/run/after-page.png")] = (
            200,
            b"hostile",
            {"Content-Type": "image/png"},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            output = Path(directory) / "artifacts"
            outside = Path(directory) / "outside"
            output.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            (output / "run").symlink_to(outside, target_is_directory=True)
            store = self.configured_store(directory, server.endpoint)

            code, stdout, stderr = self.invoke(
                [
                    "visual-test", "--layer", "Bus Stops",
                    "--artifact-dir", str(output),
                ],
                store,
            )

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["code"], "visual.artifact_write_failed")
        self.assertFalse((outside / "after-page.png").exists())

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
            if request["path"].endswith("/screenshot"):
                result["plan"]["featureInfoEvidence"] = {
                    "original": {"requested": False},
                    "candidate": {
                        "requested": True,
                        "expectedText": ["ONS Census 2021"],
                    },
                }
                panel_result = {"passed": True}
                result["visual"] = {
                    "passed": True,
                    "artifacts": {
                        "beforeFilteringPanel": "run-before/filtering.png",
                        "afterFilteringPanel": "run-after/filtering.png",
                        "beforeStylingPanel": "run-before/styling.png",
                        "afterStylingPanel": "run-after/styling.png",
                        "afterInfoPanel": "run-after/info.png",
                        "beforeHoverTooltip": "run-before/hover.png",
                        "afterHoverTooltip": "run-after/hover.png",
                    },
                    "comparison": {
                        "original": {
                            "hover": {
                                "requested": True,
                                "attempted": True,
                                "opened": True,
                                "passed": True,
                            },
                            "panels": {
                                "filtering": panel_result,
                                "styling": panel_result,
                            },
                        },
                        "candidate": {
                            "hover": {
                                "requested": True,
                                "attempted": True,
                                "opened": True,
                                "passed": True,
                            },
                            "panels": {
                                "filtering": panel_result,
                                "styling": panel_result,
                            },
                        },
                        "featureInfoEvidence": {
                            "candidate": {
                                "captured": True,
                                "passed": True,
                            },
                        },
                    },
                }
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
                        *(
                            [
                                "--view-mode",
                                "default",
                                "--panel",
                                "filtering",
                                "--panel",
                                "styling",
                                "--expect-panel-text",
                                "Cost",
                                "--expect-info-text",
                                "ONS Census 2021",
                                "--hover",
                                "--expect-hover-text",
                                "Arrival percentage",
                            ]
                            if action == "preview-screenshot"
                            else []
                        ),
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
        self.assertEqual(
            [body for _, body in captured],
            [
                {"layer": "Bus Stops", "locale": "cy"},
                {"layer": "Bus Stops", "locale": "cy"},
                {
                    "layer": "Bus Stops",
                    "locale": "cy",
                    "viewMode": "default",
                    "panels": ["filtering", "styling"],
                    "expectedPanelText": ["Cost"],
                    "expectedInfoPanelText": ["ONS Census 2021"],
                    "hover": True,
                    "expectedHoverText": ["Arrival percentage"],
                },
            ],
        )

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

    def test_candidate_visual_rejects_missing_requested_panel_evidence(self):
        routes = standard_routes()
        routes[("POST", "/api/proposals/proposal-1/screenshot")] = (
            200,
            {
                "source": "candidate",
                "proposalId": "proposal-1",
                "candidateHash": "sha256:candidate",
                "plan": {"layer": "Bus Stops"},
                "visual": {
                    "passed": True,
                    "artifacts": {},
                    "comparison": {
                        "original": {
                            "panels": {
                                "filtering": {
                                    "passed": False,
                                    "failureReason": "panel-not-found",
                                },
                            },
                        },
                        "candidate": {
                            "panels": {
                                "filtering": {
                                    "passed": True,
                                },
                            },
                        },
                    },
                },
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, _, stderr = self.invoke(
                [
                    "proposals",
                    "preview-screenshot",
                    "proposal-1",
                    "--layer",
                    "Bus Stops",
                    "--panel",
                    "filtering",
                ],
                store,
            )
        self.assertEqual(code, EXIT_VISUAL)
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "visual.evidence_incomplete")
        self.assertEqual(
            payload["details"]["missingEvidence"][0]["evidence"][
                "failureReason"
            ],
            "panel-not-found",
        )

    def test_preview_screenshot_skips_panel_and_hover_for_added_layer_original(self):
        data = {
            "plan": {
                "evidenceApplicability": {
                    "original": False,
                    "candidate": True,
                },
            },
            "visual": {
                "artifacts": {
                    "afterFilteringPanel": "/api/artifacts/after-filtering.png",
                    "afterHoverTooltip": "/api/artifacts/after-hover.png",
                },
                "comparison": {
                    "original": {
                        "panels": {},
                        "hover": {"requested": False},
                    },
                    "candidate": {
                        "panels": {"filtering": {"passed": True}},
                        "hover": {
                            "requested": True,
                            "attempted": True,
                            "opened": True,
                            "passed": True,
                        },
                    },
                },
            },
        }
        self.assertEqual(
            _validate_requested_visual_evidence(
                data,
                action="preview-screenshot",
                panels=["filtering"],
                hover=True,
            ),
            data,
        )

    def test_candidate_visual_rejects_missing_requested_hover_evidence(self):
        routes = standard_routes()
        routes[("POST", "/api/proposals/proposal-1/visual-test")] = (
            200,
            {
                "source": "candidate",
                "proposalId": "proposal-1",
                "candidateHash": "sha256:candidate",
                "plan": {"layer": "Bus Stops"},
                "visual": {
                    "passed": True,
                    "artifacts": {},
                    "hover": {
                        "requested": True,
                        "configured": True,
                        "attempted": True,
                        "opened": False,
                        "passed": False,
                        "reason": "No visible hover tooltip was observed.",
                    },
                },
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, _, stderr = self.invoke(
                [
                    "proposals",
                    "preview-test",
                    "proposal-1",
                    "--layer",
                    "Bus Stops",
                    "--hover",
                ],
                store,
            )
        self.assertEqual(code, EXIT_VISUAL)
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "visual.evidence_incomplete")
        self.assertEqual(
            payload["details"]["missingEvidence"][0]["kind"],
            "hover",
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

    def test_failed_visual_reports_an_invalid_artifact_path_explicitly(self):
        routes = standard_routes()
        routes[("POST", "/api/visual-test")] = (
            422,
            {
                "error": "Browser validation did not pass.",
                "plan": {"layer": "Bus Stops"},
                "visual": {
                    "passed": False,
                    "artifacts": {"afterPage": "../outside.png"},
                },
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            output = Path(directory) / "artifacts"
            store = self.configured_store(directory, server.endpoint)
            code, _, stderr = self.invoke(
                [
                    "visual-test", "--layer", "Bus Stops",
                    "--artifact-dir", str(output),
                ],
                store,
            )

        self.assertEqual(code, EXIT_VISUAL)
        payload = json.loads(stderr)
        download_error = payload["details"]["artifactDownloadErrors"][0]
        self.assertEqual(download_error["code"], "visual.artifact_path_invalid")
        self.assertEqual(
            download_error["details"]["invalidArtifacts"][0]["path"],
            "../outside.png",
        )

    def test_visual_planning_timeout_is_machine_readable(self):
        routes = standard_routes()
        routes[("POST", "/api/visual-test")] = (
            422,
            {
                "error": (
                    "Visual planning timed out before browser validation began."
                ),
                "code": "visual.planning_timeout",
                "planningStage": "layer-summary",
                "queryPurpose": "feature-count-and-extent",
                "timeoutMilliseconds": 5000,
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(
            routes
        ) as server:
            store = self.configured_store(directory, server.endpoint)
            code, _, stderr = self.invoke(
                [
                    "visual-test",
                    "--layer",
                    "Bus Stops",
                    "--lng",
                    "-1.532",
                    "--lat",
                    "53.814",
                    "--zoom",
                    "14",
                ],
                store,
            )

        payload = json.loads(stderr)
        self.assertEqual(EXIT_VISUAL, code)
        self.assertEqual("visual.planning_timeout", payload["code"])
        self.assertEqual(
            "layer-summary",
            payload["details"]["planningStage"],
        )
        self.assertEqual(
            "feature-count-and-extent",
            payload["details"]["queryPurpose"],
        )

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
                        {
                            "title": "Rounded cost",
                            "field": "cost_rounded",
                            "fieldfx": "round(cost)::bigint",
                            "type": "integer",
                        },
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
            [
                ("town", "like"),
                ("stop_id", "match"),
                ("object_id", "integer"),
                ("cost_rounded", "integer"),
            ],
        )
        self.assertEqual(payload["filters"][0]["source"], "includeAll")
        self.assertFalse(payload["filters"][3]["safe"])
        self.assertIn("fieldfx", payload["filters"][3]["warning"])

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

    def test_contract_1_4_proposal_list_uses_one_bounded_page(self):
        routes = standard_routes()
        routes[("GET", "/api/public/identity")][1]["contractVersion"] = "1.4"
        contract = routes[("GET", "/api/contract")][1]
        contract.update({
            "apiVersion": "1.4",
            "contractVersion": "1.4",
            "pagination": {
                "version": "1",
                "defaultLimit": 100,
                "maxLimit": 100,
                "cursor": "opaque",
            },
        })
        routes[("GET", "/api/proposals")] = (
            200,
            {
                "proposals": [{"id": "proposal-1", "status": "pending"}],
                "pagination": {"limit": 1, "nextCursor": None},
            },
        )
        cursor = "b" * 64
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(
                directory,
                server.endpoint,
                contract_version="1.4",
            )
            code, stdout, stderr = self.invoke(
                ["proposals", "list", "--limit", "1", "--cursor", cursor],
                store,
            )
            request = next(
                item for item in server.requests
                if item["path"] == "/api/proposals"
            )

        self.assertEqual(0, code, stderr)
        self.assertEqual(
            {"limit": 1, "nextCursor": None},
            json.loads(stdout)["pagination"],
        )
        self.assertEqual(f"limit=1&cursor={cursor}", request["query"])

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
                    "candidateHash": WORKSPACE_CANDIDATE_HASH,
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
            "proposal.apply_indeterminate",
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
                ("GET", "/api/connect"),
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

    def test_setup_shows_existing_profile_without_token_material_before_override(self):
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
        self.assertNotIn("Token prefix:", prompts)
        self.assertNotIn(old_secret[:6], prompts)
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

    def test_plugin_manifest_list_and_show_are_server_authoritative(self):
        routes = standard_routes()
        routes[("GET", "/api/plugins")] = (200, {"plugins": {
            "xyzVersion": "v4.23.4",
            "registrySource": "GEOLYTIX XYZ lib/plugins/_plugins.mjs",
            "loading": {"failure": "continues"},
            "dispatch": {"sync": "sequential"},
            "security": ["arbitrary browser JavaScript"],
            "xyzCommit": "a6f03c07dd7aaae2e9ab04087143ee0400e15cb9",
            "fingerprint": "catalogue-1",
            "valid": True,
            "workspaceErrors": [],
            "usage": [{"pluginId": "viewport-layer-count", "scope": "layer", "path": "locale.layers.Places"}],
            "bundled": [
                {"key": "zoomBtn", "configuration": "object", "execution": "locale"},
            ],
            "external": [{
                "id": "viewport-layer-count",
                "registrationKey": "viewport_layer_count",
                "configurationKey": "viewport_layer_count",
                "entryUrl": "/instance/plugins/viewport-layer-count/index.mjs",
                "available": True,
                "diagnostics": [],
            }],
        }})
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            list_code, list_out, list_err = self.invoke(["plugins", "list"], store)
            show_code, show_out, show_err = self.invoke(
                ["plugins", "show", "zoomBtn"], store
            )
            external_code, external_out, external_err = self.invoke(
                ["plugins", "show", "viewport-layer-count"], store
            )
            validate_code, validate_out, validate_err = self.invoke(
                ["plugins", "validate"], store
            )
            usage_code, usage_out, usage_err = self.invoke(
                ["plugins", "usage", "viewport-layer-count"], store
            )
        self.assertEqual(list_code, 0, list_err)
        self.assertEqual(show_code, 0, show_err)
        self.assertEqual(external_code, 0, external_err)
        self.assertEqual(validate_code, 0, validate_err)
        self.assertEqual(usage_code, 0, usage_err)
        self.assertEqual(json.loads(list_out)["plugins"]["xyzVersion"], "v4.23.4")
        shown = json.loads(show_out)
        self.assertEqual(shown["plugin"]["key"], "zoomBtn")
        self.assertIn("loading", shown)
        self.assertIn("dispatch", shown)
        self.assertEqual(json.loads(external_out)["plugin"]["id"], "viewport-layer-count")
        self.assertTrue(json.loads(validate_out)["valid"])
        self.assertEqual(len(json.loads(usage_out)["usage"]), 1)

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
                        "scope": "propose",
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

    def test_capability_discovery_rejects_target_identity_and_version_drift(self):
        valid_action = {
            "id": "proposals.check",
            "method": "POST",
            "path": "/api/proposals/check",
            "risk": "read",
            "scope": "propose",
        }
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            cases = {
                "instance": {"instanceId": "different-instance"},
                "contract": {"contractVersion": "1.1"},
                "api": {"apiVersion": "1.1"},
            }
            for name, override in cases.items():
                with self.subTest(name=name):
                    response = {
                        "apiVersion": "1.0",
                        "contractVersion": "1.0",
                        "instanceId": "instance-1",
                        "actions": [valid_action],
                        **override,
                    }
                    routes[("GET", "/api/capabilities")] = (200, response)
                    code, stdout, stderr = self.invoke(
                        ["capabilities", "list"],
                        store,
                    )

                    self.assertEqual(EXIT_CONFLICT, code)
                    self.assertEqual("", stdout)
                    self.assertEqual(
                        "capability.target_mismatch",
                        json.loads(stderr)["code"],
                    )

    def test_capability_discovery_rejects_duplicate_and_malformed_actions(self):
        valid_action = {
            "id": "proposals.check",
            "method": "POST",
            "path": "/api/proposals/check",
            "risk": "read",
            "scope": "propose",
            "inputSchema": {"type": "object"},
        }
        without_scope = dict(valid_action)
        without_scope.pop("scope")
        with_both_paths = {
            **valid_action,
            "pathTemplate": "/api/proposals/{id}/check",
        }
        malformed_cases = {
            "blank-id": [{**valid_action, "id": " "}],
            "duplicate-id": [valid_action, dict(valid_action)],
            "missing-scope": [without_scope],
            "invalid-method": [{**valid_action, "method": "post"}],
            "ambiguous-path": [with_both_paths],
            "invalid-input-schema": [{**valid_action, "inputSchema": []}],
        }
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            for name, actions in malformed_cases.items():
                with self.subTest(name=name):
                    routes[("GET", "/api/capabilities")] = (
                        200,
                        {
                            "apiVersion": "1.0",
                            "contractVersion": "1.0",
                            "instanceId": "instance-1",
                            "actions": actions,
                        },
                    )
                    code, stdout, stderr = self.invoke(
                        ["capabilities", "list"],
                        store,
                    )

                    self.assertEqual(EXIT_CONNECTIVITY, code)
                    self.assertEqual("", stdout)
                    self.assertEqual(
                        "capability.invalid_response",
                        json.loads(stderr)["code"],
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

    def test_operation_wait_preserves_derived_failure_code_and_guidance(self):
        operation_error = {
            "error": "Derived query exceeds the compute budget.",
            "userMessage": "The planned join creates too many intermediate rows.",
            "suggestedAction": "Reduce the join fan-out before trying again.",
            "code": "derived_layer.query_too_expensive",
            "category": "compute",
            "status": 409,
            "blocked": True,
            "stateUnchanged": True,
            "safeState": "The existing materialized data is unchanged.",
            "reasons": [{
                "code": "intermediate_rows",
                "message": "An intermediate plan node exceeds the row limit.",
                "suggestedAction": "Filter or pre-aggregate before the join.",
            }],
        }
        routes = standard_routes()
        routes[("GET", "/api/operations/op-derived-failed")] = (
            200,
            {"operation": {
                "id": "op-derived-failed",
                "kind": "derived-layer.refresh",
                "status": "failed",
                "error": operation_error,
            }},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["operations", "wait", "op-derived-failed"],
                store,
            )

        self.assertEqual(EXIT_CONFLICT, code)
        self.assertEqual("", stdout)
        payload = json.loads(stderr)
        self.assertEqual("derived_layer.query_too_expensive", payload["code"])
        self.assertEqual(operation_error["userMessage"], payload["error"])
        self.assertEqual(
            operation_error["suggestedAction"],
            payload["details"]["operation"]["error"]["suggestedAction"],
        )

    def test_operation_wait_preserves_indeterminate_derived_guidance(self):
        operation_error = {
            "error": "The result of the derived-layer operation is uncertain.",
            "userMessage": (
                "The server could not confirm whether the derived-layer "
                "operation committed."
            ),
            "suggestedAction": (
                "Inspect the operation, derived layer, and catalog before retrying."
            ),
            "code": "derived_layer.operation_failed",
            "category": "operation",
            "status": 500,
            "blocked": True,
        }
        routes = standard_routes()
        routes[("GET", "/api/operations/op-derived-uncertain")] = (
            200,
            {"operation": {
                "id": "op-derived-uncertain",
                "kind": "derived-layer.create",
                "status": "indeterminate",
                "error": operation_error,
            }},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["operations", "wait", "op-derived-uncertain"],
                store,
            )

        self.assertEqual(EXIT_CONNECTIVITY, code)
        self.assertEqual("", stdout)
        payload = json.loads(stderr)
        self.assertEqual("derived_layer.operation_failed", payload["code"])
        self.assertEqual(operation_error["userMessage"], payload["error"])
        self.assertNotIn("stateUnchanged", operation_error)

    def test_operation_wait_keeps_database_detail_diagnostic(self):
        operation_error = {
            "error": "The database could not apply this derived-layer change.",
            "userMessage": "The database could not apply this derived-layer change.",
            "suggestedAction": (
                "Check the query, sources, ID, and geometry fields."
            ),
            "code": "derived_layer.database_error",
            "status": 422,
            "blocked": True,
            "technicalDetail": {
                "sqlstate": "42703",
                "message": "column missing_field does not exist",
            },
        }
        routes = standard_routes()
        routes[("GET", "/api/operations/op-derived-database")] = (
            200,
            {"operation": {
                "id": "op-derived-database",
                "kind": "derived-layer.create",
                "status": "failed",
                "error": operation_error,
            }},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["operations", "wait", "op-derived-database"],
                store,
            )

        self.assertEqual(EXIT_VALIDATION, code)
        self.assertEqual("", stdout)
        payload = json.loads(stderr)
        self.assertEqual("derived_layer.database_error", payload["code"])
        self.assertEqual(operation_error["userMessage"], payload["error"])
        self.assertEqual(
            operation_error["technicalDetail"],
            payload["details"]["operation"]["error"]["technicalDetail"],
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

    def test_extract_sanitizes_only_terminal_output(self):
        class TerminalBuffer(io.StringIO):
            def isatty(self):
                return True

        actor = "agent\n\x1b]8;;https://evil.invalid\x07name\u009b"
        routes = standard_routes()
        routes[("GET", "/api/connect")] = (
            200,
            {
                "authenticated": True,
                "actor": actor,
                "tokenId": "abc",
                "scopes": ["full"],
                "expires": None,
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            terminal = TerminalBuffer()
            terminal_error = io.StringIO()
            terminal_code = main(
                ["--extract", "actor", "describe"],
                stdout=terminal,
                stderr=terminal_error,
                store=store,
            )
            pipe_code, pipe_output, pipe_error = self.invoke(
                ["--extract", "actor", "describe"],
                store,
            )
            output_path = Path(directory) / "actor.txt"
            file_code, file_stdout, file_error = self.invoke(
                [
                    "--extract", "actor", "--out", str(output_path),
                    "describe",
                ],
                store,
            )
            file_value = output_path.read_text(encoding="utf-8")
            file_mode = output_path.stat().st_mode & 0o777

        self.assertEqual(terminal_code, 0, terminal_error.getvalue())
        self.assertEqual(
            terminal.getvalue(),
            "agent\\u000a\\u001b]8;;https://evil.invalid\\u0007name\\u009b\n",
        )
        self.assertEqual(pipe_code, 0, pipe_error)
        self.assertEqual(pipe_output, actor + "\n")
        self.assertEqual(file_code, 0, file_error)
        self.assertEqual(file_value, actor + "\n")
        self.assertEqual(json.loads(file_stdout)["mode"], "0600")
        self.assertEqual(file_mode, 0o600)

    def test_json_output_sanitizes_only_the_terminal_boundary(self):
        class TerminalBuffer(io.StringIO):
            def isatty(self):
                return True

        actor = "agent\n\x1b]8;;https://evil.invalid\x07name\u009b"
        routes = standard_routes()
        routes[("GET", "/api/connect")] = (
            200,
            {
                "authenticated": True,
                "actor": actor,
                "tokenId": "abc",
                "scopes": ["full"],
                "expires": None,
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            terminal = TerminalBuffer()
            terminal_error = io.StringIO()
            terminal_code = main(
                ["describe"],
                stdout=terminal,
                stderr=terminal_error,
                store=store,
            )
            pipe_code, pipe_output, pipe_error = self.invoke(
                ["describe"],
                store,
            )
            output_path = Path(directory) / "describe.json"
            file_code, file_stdout, file_error = self.invoke(
                ["--out", str(output_path), "describe"],
                store,
            )
            file_output = output_path.read_text(encoding="utf-8")

        self.assertEqual(terminal_code, 0, terminal_error.getvalue())
        self.assertEqual(json.loads(terminal.getvalue())["actor"], actor)
        self.assertFalse(
            any(
                (
                    ord(character) < 0x20 and character != "\n"
                    or ord(character) == 0x7F
                    or 0x80 <= ord(character) <= 0x9F
                )
                for character in terminal.getvalue()
            )
        )
        self.assertIn("\\u009b", terminal.getvalue())
        self.assertEqual(pipe_code, 0, pipe_error)
        self.assertEqual(json.loads(pipe_output)["actor"], actor)
        self.assertIn("\u009b", pipe_output)
        self.assertEqual(file_code, 0, file_error)
        self.assertEqual(json.loads(file_output)["actor"], actor)
        self.assertIn("\u009b", file_output)
        self.assertEqual(json.loads(file_stdout)["mode"], "0600")

    def test_error_json_sanitizes_only_the_terminal_boundary(self):
        class TerminalBuffer(io.StringIO):
            def isatty(self):
                return True

        message = "invalid\n\x1b]8;;https://evil.invalid\x07request\u009b"
        routes = standard_routes()
        routes[("GET", "/api/layers")] = (
            400,
            {"error": message, "code": "locale.not_found"},
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            terminal_output = io.StringIO()
            terminal_error = TerminalBuffer()
            terminal_code = main(
                ["layers", "list"],
                stdout=terminal_output,
                stderr=terminal_error,
                store=store,
            )
            pipe_code, pipe_output, pipe_error = self.invoke(
                ["layers", "list"],
                store,
            )

        self.assertEqual(terminal_code, EXIT_VALIDATION)
        self.assertEqual(terminal_output.getvalue(), "")
        self.assertEqual(json.loads(terminal_error.getvalue())["error"], message)
        self.assertFalse(
            any(
                (
                    ord(character) < 0x20 and character != "\n"
                    or ord(character) == 0x7F
                    or 0x80 <= ord(character) <= 0x9F
                )
                for character in terminal_error.getvalue()
            )
        )
        self.assertIn("\\u009b", terminal_error.getvalue())
        self.assertEqual(pipe_code, EXIT_VALIDATION)
        self.assertEqual(pipe_output, "")
        self.assertEqual(json.loads(pipe_error)["error"], message)
        self.assertIn("\u009b", pipe_error)

    def test_device_authorization_replaces_token_only_after_verified_approval(self):
        routes = standard_routes()
        hostile_user_code = "ABCD\x1b]8;;https://evil.invalid\x07-1234\u009b"
        routes[("GET", "/api/contract")][1]["authentication"] = {
            "scopes": [
                "inspect",
                "propose",
                "visual",
                "semantic:inspect",
            ],
            "defaultDeviceScopes": [
                "inspect",
                "propose",
                "visual",
                "semantic:inspect",
            ],
        }
        routes[("POST", "/api/auth/device")] = (
            201,
            {
                "deviceId": "opaque-device",
                "userCode": hostile_user_code,
                "verificationUri": "/",
                "expiresIn": 60,
                "interval": 1,
                "scopes": [
                    "inspect",
                    "propose",
                    "visual",
                    "semantic:inspect",
                ],
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
                    "scopes": [
                        "inspect",
                        "propose",
                        "visual",
                        "semantic:inspect",
                    ],
                },
            },
        )
        routes[("GET", "/api/auth/me")] = (
            200,
            {
                "actor": "token:device",
                "scopes": [
                    "inspect",
                    "propose",
                    "visual",
                    "semantic:inspect",
                ],
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
            started = next(
                request
                for request in server.requests
                if request["path"] == "/api/auth/device"
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual("scoped-device-token", stored)
        self.assertNotIn("scoped-device-token", stdout + stderr)
        self.assertNotIn("\x1b", stderr)
        self.assertNotIn("\x07", stderr)
        self.assertNotIn("\u009b", stderr)
        self.assertIn("\\u001b", stderr)
        self.assertIn("\\u0007", stderr)
        self.assertIn("\\u009b", stderr)
        self.assertEqual(
            ["inspect", "propose", "visual", "semantic:inspect"],
            json.loads(stdout)["scopes"],
        )
        self.assertEqual(
            started["body"]["scopes"],
            ["inspect", "propose", "visual", "semantic:inspect"],
        )

    def test_device_authorization_requires_the_exact_contract_command(self):
        routes = standard_routes()
        contract = routes[("GET", "/api/contract")][1]
        contract["commands"] = [
            command
            for command in contract["commands"]
            if command != "auth device"
        ]
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["auth", "device", "--no-browser"],
                store,
            )
            device_requests = [
                request
                for request in server.requests
                if request["path"] == "/api/auth/device"
            ]

        self.assertEqual(code, EXIT_CONFLICT)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], "capability.missing")
        self.assertEqual(payload["details"]["requiredCommand"], "auth device")
        self.assertEqual(device_requests, [])

    def test_device_authorization_rejects_untrusted_verification_uris(self):
        cases = (
            (
                "cross-origin",
                "https://evil.invalid/approve",
                EXIT_CONFLICT,
                "auth.device_origin_mismatch",
            ),
            (
                "non-http",
                "javascript:alert(1)",
                EXIT_CONFLICT,
                "auth.device_origin_mismatch",
            ),
            (
                "terminal-control",
                "/approve\x1b]8;;https://evil.invalid\x07",
                EXIT_CONNECTIVITY,
                "auth.device_invalid_response",
            ),
        )
        for label, verification_uri, expected_exit, expected_code in cases:
            with self.subTest(label=label):
                routes = standard_routes()
                routes[("POST", "/api/auth/device")] = (
                    201,
                    {
                        "deviceId": "opaque-device",
                        "userCode": "ABCD-1234",
                        "verificationUri": verification_uri,
                        "expiresIn": 60,
                        "interval": 1,
                        "scopes": ["inspect", "propose", "visual"],
                    },
                )
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        ["auth", "device", "--no-browser"],
                        store,
                    )
                    polls = [
                        request
                        for request in server.requests
                        if request["path"] == "/api/auth/device/token"
                    ]

                self.assertEqual(code, expected_exit)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["code"], expected_code)
                self.assertEqual(polls, [])

    def test_device_authorization_uses_legacy_defaults_and_accepts_every_supported_scope(self):
        self.assertEqual(DEVICE_SCOPES, _DEVICE_SCOPE_CHOICES)
        self.assertEqual(
            frozenset(SAFE_DEFAULT_DEVICE_SCOPES),
            _SAFE_DEFAULT_DEVICE_SCOPES,
        )
        cases = [
            (
                ["auth", "device", "--no-browser"],
                ["inspect", "propose", "visual"],
            ),
            *[
                (
                    [
                        "auth",
                        "device",
                        "--scope",
                        scope,
                        "--no-browser",
                    ],
                    [scope],
                )
                for scope in DEVICE_SCOPES
            ],
        ]
        for arguments, expected_scopes in cases:
            with self.subTest(arguments=arguments):
                routes = standard_routes()
                routes[("POST", "/api/auth/device")] = (
                    201,
                    {
                        "deviceId": "opaque-device",
                        "userCode": "ABCD-1234",
                        "verificationUri": "/",
                        "expiresIn": 60,
                        "interval": 1,
                        "scopes": expected_scopes,
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
                            "scopes": expected_scopes,
                        },
                    },
                )
                routes[("GET", "/api/auth/me")] = (
                    200,
                    {
                        "actor": "token:device",
                        "scopes": expected_scopes,
                    },
                )
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(
                        directory,
                        server.endpoint,
                        token="legacy-full-token",
                    )
                    code, stdout, stderr = self.invoke(arguments, store)
                    started = next(
                        request
                        for request in server.requests
                        if request["path"] == "/api/auth/device"
                    )

                self.assertEqual(0, code, stderr)
                self.assertEqual(expected_scopes, started["body"]["scopes"])
                self.assertEqual(
                    expected_scopes,
                    json.loads(stdout)["scopes"],
                )

    def test_device_authorization_rejects_unknown_and_legacy_full_explicit_scopes(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            results = [
                self.invoke(
                    [
                        "auth",
                        "device",
                        "--scope",
                        scope,
                        "--no-browser",
                    ],
                    store,
                )
                for scope in ("full", "future:scope")
            ]

        for code, stdout, stderr in results:
            self.assertEqual(EXIT_USAGE, code)
            self.assertEqual("", stdout)
            self.assertEqual(
                "usage.invalid_arguments",
                json.loads(stderr)["code"],
            )
        self.assertEqual([], server.requests)

    def test_device_authorization_rejects_unsafe_advertised_defaults(self):
        unsafe_scopes = tuple(
            scope
            for scope in DEVICE_SCOPES
            if scope not in SAFE_DEFAULT_DEVICE_SCOPES
        )
        cases = (
            (
                "empty",
                ["inspect", "propose", "visual"],
                [],
            ),
            (
                "duplicate",
                ["inspect", "propose", "visual"],
                ["inspect", "inspect"],
            ),
            (
                "padded",
                ["inspect", "propose", "visual"],
                ["inspect", " propose", "visual"],
            ),
            *(
                (
                    f"elevated {scope}",
                    [*SAFE_DEFAULT_DEVICE_SCOPES, scope],
                    [scope],
                )
                for scope in unsafe_scopes
            ),
            (
                "unknown",
                ["inspect", "propose", "visual", "future:scope"],
                ["inspect", "propose", "visual", "future:scope"],
            ),
            (
                "unsupported",
                ["inspect", "propose", "visual"],
                ["inspect", "propose", "visual", "semantic:inspect"],
            ),
            (
                "full",
                ["full", "inspect", "propose", "visual"],
                ["full"],
            ),
        )
        for label, supported, defaults in cases:
            with self.subTest(label=label):
                routes = standard_routes()
                routes[("GET", "/api/contract")][1]["authentication"] = {
                    "scopes": supported,
                    "defaultDeviceScopes": defaults,
                }
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        ["auth", "device", "--no-browser"],
                        store,
                    )
                    device_requests = [
                        request
                        for request in server.requests
                        if request["path"] == "/api/auth/device"
                    ]

                self.assertEqual(EXIT_CONFLICT, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "auth.device_invalid_contract",
                    json.loads(stderr)["code"],
                )
                self.assertEqual([], device_requests)

    def test_device_authorization_rejects_scope_substitution_at_every_stage(self):
        requested = [
            "inspect",
            "propose",
            "visual",
            "semantic:inspect",
        ]
        cases = (
            (
                "start overgrant",
                [*requested, "apply"],
                requested,
                requested,
                0,
                0,
            ),
            (
                "record duplicate",
                requested,
                [*requested, "inspect"],
                requested,
                1,
                0,
            ),
            (
                "record overgrant",
                requested,
                [*requested, "apply"],
                requested,
                1,
                0,
            ),
            (
                "authenticated undergrant",
                requested,
                requested,
                requested[:-1],
                1,
                1,
            ),
            (
                "authenticated unknown",
                requested,
                requested,
                [*requested[:-1], "future:scope"],
                1,
                1,
            ),
        )
        for (
            label,
            start_scopes,
            record_scopes,
            authenticated_scopes,
            expected_polls,
            expected_me,
        ) in cases:
            with self.subTest(label=label):
                routes = standard_routes()
                routes[("GET", "/api/contract")][1]["authentication"] = {
                    "scopes": requested,
                    "defaultDeviceScopes": requested,
                }
                routes[("POST", "/api/auth/device")] = (
                    201,
                    {
                        "deviceId": "opaque-device",
                        "userCode": "ABCD-1234",
                        "verificationUri": "/",
                        "expiresIn": 60,
                        "interval": 1,
                        "scopes": start_scopes,
                    },
                )
                routes[("POST", "/api/auth/device/token")] = (
                    200,
                    {
                        "status": "authorized",
                        "token": "new-device-secret",
                        "record": {
                            "id": "token-device",
                            "expires": "2030-01-01T00:00:00Z",
                            "scopes": record_scopes,
                        },
                    },
                )
                routes[("GET", "/api/auth/me")] = (
                    200,
                    {
                        "actor": "token:device",
                        "scopes": authenticated_scopes,
                    },
                )
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
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
                    polls = [
                        request
                        for request in server.requests
                        if request["path"] == "/api/auth/device/token"
                    ]
                    me_requests = [
                        request
                        for request in server.requests
                        if request["path"] == "/api/auth/me"
                    ]

                self.assertEqual(EXIT_CONNECTIVITY, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "auth.device_invalid_response",
                    json.loads(stderr[stderr.index("{"):])["code"],
                )
                self.assertEqual("legacy-full-token", stored)
                self.assertNotIn("new-device-secret", stdout + stderr)
                self.assertEqual(expected_polls, len(polls))
                self.assertEqual(expected_me, len(me_requests))

    def test_device_authorization_validates_issued_token_metadata(self):
        requested = ["inspect", "propose", "visual"]
        cases = (
            ("blank id", "", "2030-01-01T00:00:00Z"),
            ("missing expiry", "token-device", None),
            ("blank expiry", "token-device", " "),
        )
        for label, token_id, expires in cases:
            with self.subTest(label=label):
                routes = standard_routes()
                routes[("POST", "/api/auth/device")] = (
                    201,
                    {
                        "deviceId": "opaque-device",
                        "userCode": "ABCD-1234",
                        "verificationUri": "/",
                        "expiresIn": 60,
                        "interval": 1,
                        "scopes": requested,
                    },
                )
                routes[("POST", "/api/auth/device/token")] = (
                    200,
                    {
                        "status": "authorized",
                        "token": "new-device-secret",
                        "record": {
                            "id": token_id,
                            "expires": expires,
                            "scopes": requested,
                        },
                    },
                )
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
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
                    me_requests = [
                        request
                        for request in server.requests
                        if request["path"] == "/api/auth/me"
                    ]

                self.assertEqual(EXIT_CONNECTIVITY, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "auth.device_invalid_response",
                    json.loads(stderr[stderr.index("{"):])["code"],
                )
                self.assertEqual("legacy-full-token", stored)
                self.assertNotIn("new-device-secret", stdout + stderr)
                self.assertEqual([], me_requests)

    def test_device_authorization_rejects_duplicate_explicit_scopes(self):
        routes = standard_routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "auth",
                    "device",
                    "--scope",
                    "inspect",
                    "--scope",
                    "inspect",
                    "--no-browser",
                ],
                store,
            )
            device_requests = [
                request
                for request in server.requests
                if request["path"] == "/api/auth/device"
            ]

        self.assertEqual(EXIT_USAGE, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            "auth.device_invalid_scopes",
            json.loads(stderr)["code"],
        )
        self.assertEqual([], device_requests)


if __name__ == "__main__":
    unittest.main()
