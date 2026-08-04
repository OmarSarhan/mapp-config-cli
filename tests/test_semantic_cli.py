from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mapp_config_cli.cli import main, parser
from mapp_config_cli.client import ApiClient
from mapp_config_cli.completion import generate_completion
from mapp_config_cli.config import ConfigStore, Profile
from mapp_config_cli.errors import (
    EXIT_AUTHENTICATION,
    EXIT_CONFLICT,
    EXIT_CONNECTIVITY,
    EXIT_INTERRUPTED,
    EXIT_USAGE,
)

from tests.support import JsonServer, standard_routes


SEMANTIC_COMMANDS = [
    "semantic status",
    "semantic catalog export",
    "semantic catalog search",
    "semantic catalog show",
    "semantic catalog history",
    "semantic catalog archive",
    "semantic source relations",
    "semantic source sync",
    "semantic source archive-excluded",
    "semantic generate table",
    "semantic generate field",
    "semantic derived-profiles list",
    "semantic derived-profiles show",
    "semantic derived-profiles repair",
    "semantic proposals check",
    "semantic proposals create",
    "semantic proposals list",
    "semantic proposals show",
    "semantic proposals apply",
    "semantic proposals decline",
]


class SemanticCliTests(unittest.TestCase):
    def configured_store(
        self,
        directory: str,
        endpoint: str,
        *,
        contract_version: str = "1.0",
    ) -> ConfigStore:
        store = ConfigStore(Path(directory) / "config")
        store.save_profile(
            Profile("test", endpoint, "instance-1", contract_version),
            "stored-token",
        )
        return store

    def invoke(self, arguments, store):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(arguments, stdout=stdout, stderr=stderr, store=store)
        return code, stdout.getvalue(), stderr.getvalue()

    def routes(self):
        routes = standard_routes()
        routes[("GET", "/api/contract")][1]["commands"].extend(SEMANTIC_COMMANDS)
        return routes

    @staticmethod
    def proposal_response(**updates):
        proposal = {
            "id": "semantic-proposal-1",
            "assetId": "asset:derived:bus_stops",
            "baseVersion": 2,
            "state": "pending",
            "operations": [
                {
                    "op": "set",
                    "path": "/curated/description",
                    "value": "Bus stops",
                }
            ],
            "actor": "token:author",
            "decidedBy": None,
            "decidedAt": None,
        }
        proposal.update(updates)
        return proposal

    @staticmethod
    def generation_response(
        *,
        asset_id="asset:derived:bus_stops",
        target=None,
        operations=None,
        context_options=None,
    ):
        target = target or {"kind": "table"}
        operations = operations or [
            {
                "op": "set",
                "path": "/curated/displayName",
                "value": "Bus stops",
            },
            {
                "op": "set",
                "path": "/curated/description",
                "value": "Locations where passengers can board buses.",
            },
            {
                "op": "set",
                "path": "/curated/tags",
                "value": ["transport", "bus"],
            },
            {
                "op": "set",
                "path": "/curated/caveats",
                "value": [],
            },
        ]
        generation = {
            "provider": "gemini",
            "model": "gemini-test",
            "metadataOnly": not (
                isinstance(context_options, dict)
                and any(context_options.values())
            ),
            "proposalCreated": False,
        }
        if context_options is not None:
            generation["contextOptions"] = context_options
        return {
            "draft": {
                "assetId": asset_id,
                "baseVersion": 2,
                "target": target,
                "operations": operations,
                "explanation": (
                    "Gemini-generated metadata-only semantic draft; "
                    "review every operation."
                ),
            },
            "generation": generation,
        }

    def test_generate_table_and_field_return_review_only_drafts(self):
        asset_id = "asset:derived:bus_stops"
        field_id = "source/name~public"
        field_target = {"kind": "field", "fieldId": field_id}
        field_operations = [
            {
                "op": "set",
                "path": (
                    "/curated/fields/"
                    "source~1name~0public/displayName"
                ),
                "value": "Stop name",
            },
            {
                "op": "set",
                "path": (
                    "/curated/fields/"
                    "source~1name~0public/description"
                ),
                "value": "The public-facing bus stop name.",
            },
        ]
        captured = []

        def generate(request):
            captured.append(request["body"])
            target = request["body"]["target"]
            if target["kind"] == "field":
                return 200, self.generation_response(
                    asset_id=asset_id,
                    target=field_target,
                    operations=field_operations,
                )
            table_response = self.generation_response(
                asset_id=asset_id,
                context_options={
                    "sampleRows": False,
                    "statistics": False,
                },
            )
            table_response["draft"]["operations"] = (
                table_response["draft"]["operations"][:2]
            )
            return 200, table_response

        routes = self.routes()
        routes[("POST", "/api/semantic/generate")] = generate
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            table = self.invoke(
                ["semantic", "generate", "table", asset_id],
                store,
            )
            field = self.invoke(
                [
                    "--output",
                    "human",
                    "semantic",
                    "generate",
                    "field",
                    asset_id,
                    field_id,
                ],
                store,
            )
            generation_requests = [
                request
                for request in server.requests
                if request["path"] == "/api/semantic/generate"
            ]

        self.assertEqual(0, table[0], table[2])
        self.assertEqual(0, field[0], field[2])
        table_payload = json.loads(table[1])
        self.assertEqual(
            {
                "assetId": asset_id,
                "target": {"kind": "table"},
            },
            captured[0],
        )
        self.assertEqual(
            {
                "assetId": asset_id,
                "target": field_target,
            },
            captured[1],
        )
        self.assertFalse(
            table_payload["generation"]["proposalCreated"]
        )
        self.assertTrue(table_payload["generation"]["metadataOnly"])
        self.assertEqual(
            {
                "sampleRows": False,
                "statistics": False,
            },
            table_payload["generation"]["contextOptions"],
        )
        self.assertNotIn("proposal", table_payload)
        self.assertNotIn("check", table_payload)
        next_action = table_payload["nextActions"][0]
        self.assertFalse(next_action["automatic"])
        self.assertEqual(
            "config-cli semantic proposals check",
            next_action["command"],
        )
        self.assertEqual("draft.operations", next_action["operationSource"])
        self.assertIn("Semantic draft", field[1])
        self.assertIn("proposalCreated: no", field[1])
        self.assertIn("did not check, create, or apply", field[1])
        self.assertEqual(2, len(generation_requests))

    def test_generate_context_flags_send_exact_opt_ins_and_surface_response(self):
        asset_id = "asset:derived:bus_stops"
        field_id = "field:name"
        captured = []

        def generate(request):
            captured.append(request["body"])
            target = request["body"]["target"]
            context_options = request["body"]["contextOptions"]
            operations = (
                [{
                    "op": "set",
                    "path": "/curated/fields/field:name/description",
                    "value": "A name informed by sampled field statistics.",
                }]
                if target["kind"] == "field"
                else [{
                    "op": "set",
                    "path": "/curated/description",
                    "value": "A table informed by bounded sample context.",
                }]
            )
            return 200, self.generation_response(
                asset_id=asset_id,
                target=target,
                operations=operations,
                context_options=context_options,
            )

        routes = self.routes()
        routes[("POST", "/api/semantic/generate")] = generate
        with (
            tempfile.TemporaryDirectory() as directory,
            JsonServer(routes) as server,
        ):
            store = self.configured_store(directory, server.endpoint)
            table = self.invoke(
                [
                    "semantic",
                    "generate",
                    "table",
                    asset_id,
                    "--sample-rows",
                    "--statistics",
                ],
                store,
            )
            field = self.invoke(
                [
                    "--output",
                    "human",
                    "semantic",
                    "generate",
                    "field",
                    asset_id,
                    field_id,
                    "--statistics",
                ],
                store,
            )

        self.assertEqual(0, table[0], table[2])
        self.assertEqual(0, field[0], field[2])
        self.assertEqual(
            {
                "assetId": asset_id,
                "target": {"kind": "table"},
                "contextOptions": {
                    "sampleRows": True,
                    "statistics": True,
                },
            },
            captured[0],
        )
        self.assertEqual(
            {
                "assetId": asset_id,
                "target": {"kind": "field", "fieldId": field_id},
                "contextOptions": {
                    "sampleRows": False,
                    "statistics": True,
                },
            },
            captured[1],
        )
        table_payload = json.loads(table[1])
        self.assertFalse(table_payload["generation"]["metadataOnly"])
        self.assertEqual(
            {
                "sampleRows": True,
                "statistics": True,
            },
            table_payload["generation"]["contextOptions"],
        )
        self.assertIn("metadataOnly: no", field[1])
        self.assertIn(
            'contextOptions: {"sampleRows":false,"statistics":true}',
            field[1],
        )

    def test_generate_context_flags_require_matching_response_context(self):
        asset_id = "asset:derived:bus_stops"
        response = self.generation_response(asset_id=asset_id)
        routes = self.routes()
        routes[("POST", "/api/semantic/generate")] = (200, response)
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "semantic",
                    "generate",
                    "table",
                    asset_id,
                    "--sample-rows",
                ],
                store,
            )

        self.assertEqual(EXIT_CONNECTIVITY, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            "semantic.invalid_response",
            json.loads(stderr)["code"],
        )

    def test_generate_rejects_malformed_success_envelopes(self):
        asset_id = "asset:derived:bus_stops"
        valid = self.generation_response(asset_id=asset_id)
        cases = {
            "wrong asset": {
                **valid,
                "draft": {**valid["draft"], "assetId": "asset:other"},
            },
            "wrong target": {
                **valid,
                "draft": {
                    **valid["draft"],
                    "target": {"kind": "field", "fieldId": "name"},
                },
            },
            "zero base version": {
                **valid,
                "draft": {**valid["draft"], "baseVersion": 0},
            },
            "not metadata only": {
                **valid,
                "generation": {
                    **valid["generation"],
                    "metadataOnly": False,
                },
            },
            "unrequested data context": {
                **valid,
                "generation": {
                    **valid["generation"],
                    "metadataOnly": False,
                    "contextOptions": {
                        "sampleRows": True,
                        "statistics": False,
                    },
                },
            },
            "proposal created": {
                **valid,
                "generation": {
                    **valid["generation"],
                    "proposalCreated": True,
                },
            },
            "unexpected proposal": {
                **valid,
                "proposal": {"id": "must-not-exist"},
            },
            "generated mutation": {
                **valid,
                "draft": {
                    **valid["draft"],
                    "operations": [{
                        "op": "set",
                        "path": "/generated/description",
                        "value": "unsafe",
                    }],
                },
            },
            "wrong table value type": {
                **valid,
                "draft": {
                    **valid["draft"],
                    "operations": [
                        *valid["draft"]["operations"][:-1],
                        {
                            "op": "set",
                            "path": "/curated/caveats",
                            "value": "not-an-array",
                        },
                    ],
                },
            },
            "oversized display name": {
                **valid,
                "draft": {
                    **valid["draft"],
                    "operations": [
                        {
                            **operation,
                            "value": "x" * 121,
                        }
                        if operation["path"] == "/curated/displayName"
                        else operation
                        for operation in valid["draft"]["operations"]
                    ],
                },
            },
            "case-insensitive duplicate tags": {
                **valid,
                "draft": {
                    **valid["draft"],
                    "operations": [
                        {
                            **operation,
                            "value": ["Transport", "transport"],
                        }
                        if operation["path"] == "/curated/tags"
                        else operation
                        for operation in valid["draft"]["operations"]
                    ],
                },
            },
            "too many caveats": {
                **valid,
                "draft": {
                    **valid["draft"],
                    "operations": [
                        {
                            **operation,
                            "value": [
                                f"caveat-{index}"
                                for index in range(13)
                            ],
                        }
                        if operation["path"] == "/curated/caveats"
                        else operation
                        for operation in valid["draft"]["operations"]
                    ],
                },
            },
        }
        for label, response in cases.items():
            with self.subTest(label=label):
                routes = self.routes()
                routes[("POST", "/api/semantic/generate")] = (200, response)
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        ["semantic", "generate", "table", asset_id],
                        store,
                    )
                    generation_requests = [
                        request
                        for request in server.requests
                        if request["path"] == "/api/semantic/generate"
                    ]
                self.assertEqual(EXIT_CONNECTIVITY, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "semantic.invalid_response",
                    json.loads(stderr)["code"],
                )
                self.assertEqual(1, len(generation_requests))

    def test_generate_provider_failure_is_preserved_and_not_retried(self):
        routes = self.routes()
        routes[("POST", "/api/semantic/generate")] = (
            502,
            {
                "error": "Semantic generation failed.",
                "code": "semantic.generation_failed",
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "semantic",
                    "generate",
                    "field",
                    "asset:derived:bus_stops",
                    "name",
                ],
                store,
            )
            generation_requests = [
                request
                for request in server.requests
                if request["path"] == "/api/semantic/generate"
            ]

        self.assertEqual(EXIT_CONNECTIVITY, code)
        self.assertEqual("", stdout)
        failure = json.loads(stderr)
        self.assertEqual("semantic.generation_failed", failure["code"])
        self.assertEqual(502, failure["httpStatus"])
        self.assertEqual(1, len(generation_requests))

    def test_generate_scope_matrix_is_server_authoritative_and_never_mutates(self):
        single_scopes = (
            "full",
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
        cases = [
            ("no scopes", [], "semantic:generate"),
            *[
                (
                    scope,
                    [scope],
                    (
                        None
                        if scope == "full"
                        else (
                            "semantic:inspect"
                            if scope == "semantic:generate"
                            else "semantic:generate"
                        )
                    ),
                )
                for scope in single_scopes
            ],
            (
                "generation pair",
                ["semantic:inspect", "semantic:generate"],
                None,
            ),
            (
                "generation pair plus admin",
                [
                    "semantic:inspect",
                    "semantic:generate",
                    "semantic:admin",
                ],
                None,
            ),
            (
                "generate plus admin",
                ["semantic:generate", "semantic:admin"],
                "semantic:inspect",
            ),
            (
                "inspect plus admin",
                ["semantic:inspect", "semantic:admin"],
                "semantic:generate",
            ),
        ]
        active_scopes: list[str] = []

        def generate(request):
            if (
                "full" not in active_scopes
                and "semantic:generate" not in active_scopes
            ):
                required_scope = "semantic:generate"
            elif (
                "full" not in active_scopes
                and "semantic:inspect" not in active_scopes
            ):
                required_scope = "semantic:inspect"
            else:
                return 200, self.generation_response(
                    asset_id=request["body"]["assetId"],
                )
            return 403, {
                "error": "The credential does not grant the required scope.",
                "code": "auth.scope_required",
                "requiredScope": required_scope,
                "grantedScopes": sorted(active_scopes),
            }

        routes = self.routes()
        routes[("POST", "/api/semantic/generate")] = generate
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            for label, scopes, required_scope in cases:
                with self.subTest(label=label, scopes=scopes):
                    active_scopes[:] = scopes
                    request_offset = len(server.requests)
                    code, stdout, stderr = self.invoke(
                        [
                            "semantic",
                            "generate",
                            "table",
                            "asset:derived:bus_stops",
                        ],
                        store,
                    )
                    requests = server.requests[request_offset:]
                    post_paths = [
                        request["path"]
                        for request in requests
                        if request["method"] == "POST"
                    ]

                    self.assertEqual(
                        ["/api/semantic/generate"],
                        post_paths,
                    )
                    if required_scope is None:
                        self.assertEqual(0, code, stderr)
                        response = json.loads(stdout)
                        self.assertFalse(
                            response["generation"]["proposalCreated"]
                        )
                        self.assertNotIn("proposal", response)
                        self.assertNotIn("check", response)
                    else:
                        self.assertEqual(EXIT_AUTHENTICATION, code)
                        self.assertEqual("", stdout)
                        failure = json.loads(stderr)
                        self.assertEqual(
                            "auth.scope_required",
                            failure["code"],
                        )
                        self.assertEqual(403, failure["httpStatus"])
                        self.assertEqual(
                            required_scope,
                            failure["details"]["requiredScope"],
                        )
                        self.assertEqual(
                            sorted(scopes),
                            failure["details"]["grantedScopes"],
                        )

            profile = store.selected_profile("test")
            self.assertEqual("stored-token", store.token_for(profile))
            self.assertFalse(store.checks_path.exists())

    def test_generate_data_context_preserves_semantic_data_scope_failure(self):
        routes = self.routes()
        routes[("POST", "/api/semantic/generate")] = (
            403,
            {
                "error": "The credential does not grant the required scope.",
                "code": "auth.scope_required",
                "requiredScope": "semantic:data",
                "grantedScopes": [
                    "semantic:generate",
                    "semantic:inspect",
                ],
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "semantic",
                    "generate",
                    "table",
                    "asset:derived:bus_stops",
                    "--statistics",
                ],
                store,
            )
            requests = [
                request
                for request in server.requests
                if request["path"] == "/api/semantic/generate"
            ]

        self.assertEqual(EXIT_AUTHENTICATION, code)
        self.assertEqual("", stdout)
        failure = json.loads(stderr)
        self.assertEqual("auth.scope_required", failure["code"])
        self.assertEqual(
            "semantic:data",
            failure["details"]["requiredScope"],
        )
        self.assertEqual(1, len(requests))
        self.assertEqual(
            {
                "sampleRows": False,
                "statistics": True,
            },
            requests[0]["body"]["contextOptions"],
        )

    def test_generate_field_rejects_operations_for_another_target(self):
        asset_id = "asset:derived:bus_stops"
        field_id = "source/name"
        target = {"kind": "field", "fieldId": field_id}
        cases = {
            "table path": "/curated/description",
            "other field": "/curated/fields/other/description",
            "whole annotation": "/curated/fields/source~1name",
        }
        for label, path in cases.items():
            with self.subTest(label=label):
                response = self.generation_response(
                    asset_id=asset_id,
                    target=target,
                    operations=[{
                        "op": "set",
                        "path": path,
                        "value": "Generated description",
                    }],
                )
                routes = self.routes()
                routes[("POST", "/api/semantic/generate")] = (
                    200,
                    response,
                )
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(
                        directory,
                        server.endpoint,
                    )
                    code, stdout, stderr = self.invoke(
                        [
                            "semantic",
                            "generate",
                            "field",
                            asset_id,
                            field_id,
                        ],
                        store,
                    )
                self.assertEqual(EXIT_CONNECTIVITY, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "semantic.invalid_response",
                    json.loads(stderr)["code"],
                )

    def test_semantic_source_and_generate_are_parser_and_completion_visible(self):
        source = parser().parse_args(
            ["semantic", "source", "relations"]
        )
        sync = parser().parse_args([
            "semantic",
            "source",
            "sync",
            "--alias",
            "main",
            "--schema",
            "leeds",
            "--relation",
            "census_2021_england_oa",
            "--confirm",
        ])
        table = parser().parse_args(
            [
                "semantic",
                "generate",
                "table",
                "asset-1",
                "--sample-rows",
                "--statistics",
            ]
        )
        field = parser().parse_args(
            ["semantic", "generate", "field", "asset-1", "field-1"]
        )
        archive = parser().parse_args([
            "semantic", "catalog", "archive", "asset-1", "--confirm",
        ])
        archive_excluded = parser().parse_args([
            "semantic", "source", "archive-excluded", "--confirm",
        ])
        device = parser().parse_args(
            [
                "auth",
                "device",
                "--scope",
                "semantic:source",
                "--scope",
                "semantic:generate",
            ]
        )
        completion = generate_completion(parser(), "bash")

        self.assertEqual("relations", source.semantic_action)
        self.assertEqual("main", sync.alias)
        self.assertEqual("leeds", sync.schema)
        self.assertEqual("census_2021_england_oa", sync.relation)
        self.assertEqual("table", table.semantic_action)
        self.assertTrue(table.sample_rows)
        self.assertTrue(table.statistics)
        self.assertEqual("field", field.semantic_action)
        self.assertFalse(field.sample_rows)
        self.assertFalse(field.statistics)
        self.assertEqual("archive", archive.semantic_action)
        self.assertTrue(archive.confirm)
        self.assertEqual("archive-excluded", archive_excluded.semantic_action)
        self.assertTrue(archive_excluded.confirm)
        self.assertEqual(
            ["semantic:source", "semantic:generate"],
            device.device_scopes,
        )
        self.assertIn("semantic source:relations", completion)
        self.assertIn("semantic source:sync", completion)
        self.assertIn("semantic generate:table", completion)
        self.assertIn("semantic generate:field", completion)
        self.assertIn("semantic catalog:archive", completion)
        self.assertIn("semantic source:archive-excluded", completion)

    def test_catalog_commands_use_exact_nested_routes_and_revision_context(self):
        routes = self.routes()
        routes[("GET", "/api/semantic/status")] = (
            200,
            {
                "ok": True,
                "schemaVersion": 1,
                "catalogRevision": 7,
                "capabilities": {"catalog": True, "proposals": True},
            },
        )
        routes[("GET", "/api/semantic/catalog")] = (
            200,
            {
                "catalogRevision": 7,
                "assets": [
                    {
                        "id": "asset:derived:bus_stops",
                        "version": 2,
                        "status": "ready",
                        "generated": {},
                        "curated": {},
                    }
                ],
            },
        )
        routes[("GET", "/api/semantic/catalog/search")] = (
            200,
            {
                "catalogRevision": 7,
                "query": "bus stops",
                "results": [
                    {
                        "id": "asset:derived:bus_stops",
                        "version": 2,
                        "score": 1.0,
                    }
                ],
            },
        )
        routes[("GET", "/api/semantic/catalog/objects/asset%3Aderived%3Abus_stops")] = (
            200,
            {
                "catalogRevision": 7,
                "asset": {
                    "id": "asset:derived:bus_stops",
                    "version": 2,
                    "status": "ready",
                    "generated": {},
                    "curated": {},
                },
            },
        )
        routes[(
            "GET",
            (
                "/api/semantic/catalog/objects/"
                "asset%3Aderived%3Abus_stops/history"
            ),
        )] = (
            200,
            {
                "assetId": "asset:derived:bus_stops",
                "catalogRevision": 7,
                "history": [{
                    "version": 2,
                    "generation": 1,
                    "catalogRevision": 7,
                    "changeType": "register",
                    "eventId": "event-1",
                    "proposalId": None,
                    "actor": "token:author",
                    "changedAt": "2026-07-26T10:00:00Z",
                    "asset": {
                        "id": "asset:derived:bus_stops",
                        "version": 2,
                        "generation": 1,
                        "catalogRevision": 7,
                        "status": "ready",
                        "generated": {},
                        "curated": {},
                    },
                }],
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            invocations = [
                ["semantic", "status"],
                ["semantic", "catalog", "export"],
                ["semantic", "catalog", "search", "bus stops", "--limit", "5"],
                [
                    "semantic",
                    "catalog",
                    "show",
                    "asset:derived:bus_stops",
                ],
                [
                    "semantic",
                    "catalog",
                    "history",
                    "asset:derived:bus_stops",
                ],
            ]
            results = [self.invoke(arguments, store) for arguments in invocations]
            requests = list(server.requests)

        for code, _, stderr in results:
            self.assertEqual(code, 0, stderr)
        self.assertTrue(all(json.loads(stdout)["catalogRevision"] == 7 for _, stdout, _ in results))
        search = next(
            request
            for request in requests
            if request["path"] == "/api/semantic/catalog/search"
        )
        self.assertEqual(search["query"], "q=bus+stops&limit=5")
        self.assertIn(
            "/api/semantic/catalog/objects/asset%3Aderived%3Abus_stops",
            [request["path"] for request in requests],
        )
        self.assertIn(
            (
                "/api/semantic/catalog/objects/"
                "asset%3Aderived%3Abus_stops/history"
            ),
            [request["path"] for request in requests],
        )

    def test_contract_1_4_collection_commands_use_one_bounded_page(self):
        routes = self.routes()
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
        pagination = {"limit": 1, "nextCursor": None}
        routes[("GET", "/api/semantic/catalog")] = (
            200,
            {"catalogRevision": 7, "assets": [], "pagination": pagination},
        )
        routes[("GET", "/api/semantic/catalog/search")] = (
            200,
            {
                "catalogRevision": 7,
                "query": "roads",
                "results": [],
                "pagination": pagination,
            },
        )
        history_path = "/api/semantic/catalog/objects/asset-1/history"
        routes[("GET", history_path)] = (
            200,
            {
                "assetId": "asset-1",
                "catalogRevision": 7,
                "history": [],
                "pagination": pagination,
            },
        )
        routes[("GET", "/api/semantic/source/relations")] = (
            200,
            {"relations": [], "pagination": pagination},
        )
        routes[("GET", "/api/semantic/derived-profiles")] = (
            200,
            {
                "catalogRevision": 7,
                "derivedProfiles": [],
                "pagination": pagination,
            },
        )
        routes[("GET", "/api/semantic/proposals")] = (
            200,
            {
                "catalogRevision": 7,
                "proposals": [],
                "pagination": pagination,
            },
        )
        cursor = "a" * 64
        invocations = (
            ["semantic", "catalog", "export", "--limit", "1", "--cursor", cursor],
            ["semantic", "catalog", "search", "roads", "--limit", "1", "--cursor", cursor],
            ["semantic", "catalog", "history", "asset-1", "--limit", "1", "--cursor", cursor],
            ["semantic", "source", "relations", "--limit", "1", "--cursor", cursor],
            ["semantic", "derived-profiles", "list", "--limit", "1", "--cursor", cursor],
            ["semantic", "proposals", "list", "--limit", "1", "--cursor", cursor],
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(
                directory,
                server.endpoint,
                contract_version="1.4",
            )
            results = [self.invoke(arguments, store) for arguments in invocations]
            collection_requests = [
                request
                for request in server.requests
                if request["path"] in {
                    "/api/semantic/catalog",
                    "/api/semantic/catalog/search",
                    history_path,
                    "/api/semantic/source/relations",
                    "/api/semantic/derived-profiles",
                    "/api/semantic/proposals",
                }
            ]

        for code, stdout, stderr in results:
            self.assertEqual(0, code, stderr)
            self.assertEqual(pagination, json.loads(stdout)["pagination"])
        self.assertEqual(6, len(collection_requests))
        for request in collection_requests:
            self.assertIn("limit=1", request["query"])
            self.assertIn(f"cursor={cursor}", request["query"])

    def test_pagination_flags_fail_closed_against_legacy_contract(self):
        routes = self.routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["semantic", "catalog", "export", "--limit", "1"],
                store,
            )

        self.assertEqual(EXIT_CONFLICT, code)
        self.assertEqual("", stdout)
        self.assertEqual("pagination.unsupported", json.loads(stderr)["code"])

    def test_confirmed_archive_commands_validate_and_preserve_metadata(self):
        asset_id = "b08f4fb6-e4ac-5963-982f-843ee00d21f3"
        binding = {
            "adapter": "postgresql",
            "alias": "main",
            "schema": "leeds",
            "relation": "internal_table",
        }
        routes = self.routes()
        routes[(
            "POST",
            f"/api/semantic/catalog/objects/{asset_id}/archive",
        )] = (
            200,
            {
                "asset": {
                    "id": asset_id,
                    "version": 3,
                    "generation": 2,
                    "status": "archived",
                    "generated": {"binding": binding},
                    "curated": {"description": "Historical annotation"},
                },
                "meta": {"requestId": "archive-one"},
            },
        )
        routes[("POST", "/api/semantic/source/archive-excluded")] = (
            200,
            {
                "archived": [{"id": asset_id, "binding": binding}],
                "meta": {"requestId": "archive-excluded"},
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            one = self.invoke(
                [
                    "semantic", "catalog", "archive", asset_id, "--confirm",
                ],
                store,
            )
            excluded = self.invoke(
                ["semantic", "source", "archive-excluded", "--confirm"],
                store,
            )
            archive_requests = [
                request
                for request in server.requests
                if request["method"] == "POST" and "archive" in request["path"]
            ]

        self.assertEqual(0, one[0], one[2])
        self.assertEqual(0, excluded[0], excluded[2])
        self.assertEqual("archived", json.loads(one[1])["asset"]["status"])
        self.assertEqual(asset_id, json.loads(excluded[1])["archived"][0]["id"])
        self.assertEqual(
            [{"confirmed": True}, {"confirmed": True}],
            [request["body"] for request in archive_requests],
        )

    def test_catalog_archive_rejects_a_non_archived_success_response(self):
        asset_id = "asset-1"
        routes = self.routes()
        routes[(
            "POST",
            f"/api/semantic/catalog/objects/{asset_id}/archive",
        )] = (
            200,
            {
                "asset": {
                    "id": asset_id,
                    "version": 1,
                    "status": "ready",
                    "generated": {},
                    "curated": {},
                },
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "semantic", "catalog", "archive", asset_id, "--confirm",
                ],
                store,
            )

        self.assertEqual(EXIT_CONNECTIVITY, code)
        self.assertEqual("", stdout)
        self.assertEqual("semantic.invalid_response", json.loads(stderr)["code"])

    def test_source_relations_and_confirmed_sync_use_closed_contracts(self):
        source = {
            "alias": "main",
            "schema": "leeds",
            "relation": "census_2021_england_oa",
            "kind": "table",
            "assetId": "44092ae6-e314-5a4b-b77d-82135c6e9ac5",
        }
        asset = {
            "id": source["assetId"],
            "version": 1,
            "generation": 1,
            "status": "ready",
            "generated": {
                "name": source["relation"],
                "kind": source["kind"],
                "binding": {
                    "adapter": "postgresql",
                    "alias": source["alias"],
                    "schema": source["schema"],
                    "relation": source["relation"],
                },
                "fields": [],
            },
            "curated": {},
        }
        captured = []
        routes = self.routes()
        routes[("GET", "/api/semantic/source/relations")] = (
            200,
            {"relations": [source], "meta": {"requestId": "relations-1"}},
        )

        def sync(request):
            captured.append(request["body"])
            return 200, {
                "catalogRevision": 8,
                "operation": (
                    "register"
                    if len(captured) == 1
                    else "unchanged"
                ),
                "source": source,
                "asset": asset,
                "meta": {"requestId": f"sync-{len(captured)}"},
            }

        routes[("POST", "/api/semantic/source/sync")] = sync
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            discovered = self.invoke(
                ["semantic", "source", "relations"],
                store,
            )
            synchronized = self.invoke(
                [
                    "semantic",
                    "source",
                    "sync",
                    "--alias",
                    source["alias"],
                    "--schema",
                    source["schema"],
                    "--relation",
                    source["relation"],
                    "--confirm",
                ],
                store,
            )
            unchanged = self.invoke(
                [
                    "semantic",
                    "source",
                    "sync",
                    "--alias",
                    source["alias"],
                    "--schema",
                    source["schema"],
                    "--relation",
                    source["relation"],
                    "--confirm",
                ],
                store,
            )

        self.assertEqual(0, discovered[0], discovered[2])
        self.assertEqual(0, synchronized[0], synchronized[2])
        self.assertEqual(0, unchanged[0], unchanged[2])
        self.assertEqual(source, json.loads(discovered[1])["relations"][0])
        self.assertEqual("register", json.loads(synchronized[1])["operation"])
        self.assertEqual("unchanged", json.loads(unchanged[1])["operation"])
        self.assertEqual(
            [{
                "alias": source["alias"],
                "schema": source["schema"],
                "relation": source["relation"],
            }] * 2,
            captured,
        )

    def test_source_responses_bind_identity_and_reject_open_or_malformed_data(self):
        source = {
            "alias": "main",
            "schema": "leeds",
            "relation": "census",
            "kind": "table",
            "assetId": "0d264685-f0cb-59d9-b42f-68fef5dbed0c",
        }
        asset = {
            "id": source["assetId"],
            "version": 1,
            "generation": 1,
            "status": "ready",
            "generated": {
                "kind": source["kind"],
                "binding": {
                    "adapter": "postgresql",
                    "alias": source["alias"],
                    "schema": source["schema"],
                    "relation": source["relation"],
                },
            },
            "curated": {},
        }
        sync_arguments = [
            "semantic",
            "source",
            "sync",
            "--alias",
            source["alias"],
            "--schema",
            source["schema"],
            "--relation",
            source["relation"],
            "--confirm",
        ]
        invalid_discovery = (
            {"relations": [{**source, "columns": []}]},
            {"relations": [source, dict(source)]},
            {"relations": [{**source, "kind": " \t"}]},
            {"relations": [{**source, "kind": "foreign-table"}]},
            {"relations": [source], "catalogRevision": 1},
        )
        invalid_sync = (
            {
                "catalogRevision": True,
                "operation": "register",
                "source": source,
                "asset": asset,
            },
            {
                "catalogRevision": 1,
                "operation": "replace",
                "source": source,
                "asset": asset,
            },
            {
                "catalogRevision": 1,
                "operation": "register",
                "source": {**source, "schema": "other"},
                "asset": asset,
            },
            {
                "catalogRevision": 1,
                "operation": "register",
                "source": source,
                "asset": {
                    **asset,
                    "generated": {
                        "kind": source["kind"],
                        "binding": {
                            **asset["generated"]["binding"],
                            "adapter": "other",
                        },
                    },
                },
            },
            {
                "catalogRevision": 1,
                "operation": "register",
                "source": source,
                "asset": asset,
                "unexpected": True,
            },
        )
        results = []
        for response in invalid_discovery:
            routes = self.routes()
            routes[("GET", "/api/semantic/source/relations")] = (
                200,
                response,
            )
            with tempfile.TemporaryDirectory() as directory, JsonServer(
                routes
            ) as server:
                store = self.configured_store(directory, server.endpoint)
                results.append(self.invoke(
                    ["semantic", "source", "relations"],
                    store,
                ))
        for response in invalid_sync:
            routes = self.routes()
            routes[("POST", "/api/semantic/source/sync")] = (
                200,
                response,
            )
            with tempfile.TemporaryDirectory() as directory, JsonServer(
                routes
            ) as server:
                store = self.configured_store(directory, server.endpoint)
                results.append(self.invoke(sync_arguments, store))

        for code, stdout, stderr in results:
            self.assertEqual(EXIT_CONNECTIVITY, code)
            self.assertEqual("", stdout)
            self.assertEqual(
                "semantic.invalid_response",
                json.loads(stderr)["code"],
            )

    def test_catalog_show_encodes_one_segment_and_binds_response_identity(self):
        asset_id = "asset:roads/active?view#one%raw"
        encoded_path = (
            "/api/semantic/catalog/objects/"
            "asset%3Aroads%2Factive%3Fview%23one%25raw"
        )
        returned_ids = [asset_id, "asset:roads/other"]

        def show(_request):
            return 200, {
                "catalogRevision": 4,
                "asset": {
                    "id": returned_ids.pop(0),
                    "version": 1,
                    "status": "ready",
                    "generated": {},
                    "curated": {},
                },
            }

        routes = self.routes()
        routes[("GET", encoded_path)] = show
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            first = self.invoke(
                ["semantic", "catalog", "show", asset_id],
                store,
            )
            second = self.invoke(
                ["semantic", "catalog", "show", asset_id],
                store,
            )
            requests = [
                request
                for request in server.requests
                if request["path"] == encoded_path
            ]

        self.assertEqual(0, first[0], first[2])
        self.assertEqual(asset_id, json.loads(first[1])["asset"]["id"])
        self.assertEqual(EXIT_CONNECTIVITY, second[0])
        self.assertEqual(
            "semantic.invalid_response",
            json.loads(second[2])["code"],
        )
        self.assertEqual(2, len(requests))
        self.assertTrue(all(request["query"] == "" for request in requests))

    def test_catalog_history_binds_each_snapshot_to_asset_and_revision(self):
        asset_id = "asset:derived:paths"
        path = (
            "/api/semantic/catalog/objects/"
            "asset%3Aderived%3Apaths/history"
        )
        responses = [
            {
                "assetId": asset_id,
                "catalogRevision": 9,
                "history": [{
                    "version": 2,
                    "generation": 1,
                    "catalogRevision": 8,
                    "changeType": "curated",
                    "eventId": None,
                    "proposalId": "proposal-1",
                    "actor": "token:curator",
                    "changedAt": "2026-07-26T11:00:00Z",
                    "asset": {
                        "id": asset_id,
                        "version": 2,
                        "generation": 1,
                        "catalogRevision": 8,
                        "status": "ready",
                        "generated": {},
                        "curated": {"description": "Paths"},
                    },
                }],
            },
            {
                "assetId": asset_id,
                "catalogRevision": 9,
                "history": [{
                    "version": 2,
                    "generation": 1,
                    "catalogRevision": 8,
                    "changeType": "curated",
                    "eventId": None,
                    "proposalId": "proposal-1",
                    "actor": "token:curator",
                    "changedAt": "2026-07-26T11:00:00Z",
                    "asset": {
                        "id": "asset:derived:other",
                        "version": 2,
                        "generation": 1,
                        "catalogRevision": 8,
                        "status": "ready",
                        "generated": {},
                        "curated": {},
                    },
                }],
            },
        ]

        def history(_request):
            return 200, responses.pop(0)

        routes = self.routes()
        routes[("GET", path)] = history
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            first = self.invoke(
                ["semantic", "catalog", "history", asset_id],
                store,
            )
            second = self.invoke(
                ["semantic", "catalog", "history", asset_id],
                store,
            )

        self.assertEqual(0, first[0], first[2])
        self.assertEqual(
            asset_id,
            json.loads(first[1])["history"][0]["asset"]["id"],
        )
        self.assertEqual(EXIT_CONNECTIVITY, second[0])
        self.assertEqual(
            "semantic.invalid_response",
            json.loads(second[2])["code"],
        )

    def test_blank_semantic_command_identifiers_stop_before_network(self):
        commands = [
            ["semantic", "catalog", "show", " \t"],
            ["semantic", "catalog", "history", " \t"],
            ["semantic", "generate", "table", " \t"],
            ["semantic", "generate", "field", "asset-1", " \t"],
            ["semantic", "derived-profiles", "show", " \t"],
            [
                "semantic",
                "derived-profiles",
                "repair",
                " \t",
                "--confirm",
            ],
            ["semantic", "proposals", "show", " \t"],
            ["semantic", "proposals", "apply", " \t", "--confirm"],
            ["semantic", "proposals", "decline", " \t", "--confirm"],
        ]
        for option in ("--alias", "--schema", "--relation"):
            arguments = [
                "semantic",
                "source",
                "sync",
                "--alias",
                "main",
                "--schema",
                "leeds",
                "--relation",
                "census",
                "--confirm",
            ]
            arguments[arguments.index(option) + 1] = " \t"
            commands.append(arguments)
        routes = self.routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            results = [self.invoke(command, store) for command in commands]

        for command, (code, stdout, stderr) in zip(commands, results):
            with self.subTest(command=command):
                self.assertEqual(EXIT_USAGE, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "usage.invalid_arguments",
                    json.loads(stderr)["code"],
                )
        self.assertEqual([], server.requests)

    def test_describe_and_doctor_report_advertised_semantic_readiness(self):
        routes = self.routes()
        routes[("GET", "/api/semantic/status")] = (
            200,
            {
                "ok": True,
                "schemaVersion": 1,
                "catalogRevision": 9,
                "capabilities": {"catalog": True, "proposals": True},
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            describe = self.invoke(["describe"], store)
            doctor = self.invoke(["doctor"], store)

        self.assertEqual(describe[0], 0, describe[2])
        self.assertEqual(doctor[0], 0, doctor[2])
        describe_semantic = json.loads(describe[1])["semantic"]
        doctor_payload = json.loads(doctor[1])
        self.assertTrue(describe_semantic["available"])
        self.assertEqual(describe_semantic["catalogRevision"], 9)
        self.assertEqual(doctor_payload["semantic"]["catalogRevision"], 9)
        self.assertTrue(
            doctor_payload["capabilities"]["semantic"]["proposals"]
        )
        self.assertTrue(
            doctor_payload["capabilities"]["semantic"]["generation"]
        )

    def test_describe_and_doctor_semantic_preflight_has_no_implicit_scope_grants(self):
        scope_cases = [
            ["full"],
            ["inspect"],
            ["propose"],
            ["visual"],
            ["apply"],
            ["reload"],
            ["derive"],
            ["semantic:inspect"],
            ["semantic:source"],
            ["semantic:generate"],
            ["semantic:data"],
            ["semantic:propose"],
            ["semantic:apply"],
            ["semantic:admin"],
            ["semantic:inspect", "semantic:admin"],
        ]
        routes = self.routes()
        auth_response = {
            "authenticated": True,
            "actor": "workspace-reader",
            "tokenId": "scope-test",
            "scopes": ["inspect"],
            "expires": None,
        }
        routes[("GET", "/api/connect")] = (
            200,
            auth_response,
        )
        routes[("GET", "/api/semantic/status")] = (
            200,
            {
                "ok": True,
                "schemaVersion": 1,
                "catalogRevision": 9,
                "capabilities": {"catalog": True, "proposals": True},
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            for command in ("describe", "doctor"):
                for scopes in scope_cases:
                    with self.subTest(command=command, scopes=scopes):
                        auth_response["scopes"] = scopes
                        request_offset = len(server.requests)
                        code, stdout, stderr = self.invoke([command], store)
                        semantic_requests = [
                            request
                            for request in server.requests[request_offset:]
                            if request["path"] == "/api/semantic/status"
                        ]
                        expected_authorized = (
                            "full" in scopes
                            or "semantic:inspect" in scopes
                        )

                        self.assertEqual(0, code, stderr)
                        self.assertEqual(
                            1 if expected_authorized else 0,
                            len(semantic_requests),
                        )
                        semantic = json.loads(stdout)["semantic"]
                        self.assertTrue(semantic["advertised"])
                        self.assertEqual(
                            expected_authorized,
                            semantic["authorized"],
                        )
                        self.assertEqual(
                            expected_authorized,
                            semantic["available"],
                        )

    def test_derived_profiles_list_show_and_confirmed_repair(self):
        profile = {
            "name": "bus_stops",
            "assetId": "asset:derived:bus_stops",
            "generation": 3,
            "status": "repair_required",
            "revision": "7",
        }
        routes = self.routes()
        routes[("GET", "/api/semantic/derived-profiles")] = (
            200,
            {"catalogRevision": 7, "derivedProfiles": [profile]},
        )
        routes[("GET", "/api/semantic/derived-profiles/bus_stops")] = (
            200,
            {"catalogRevision": 7, "derivedProfile": profile},
        )

        def repair(request):
            self.assertEqual(request["body"], {"confirmed": True})
            return 200, {
                "catalogRevision": 8,
                "derivedProfile": {**profile, "status": "registering"},
            }

        routes[("POST", "/api/semantic/derived-profiles/bus_stops/repair")] = repair
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            list_result = self.invoke(
                ["semantic", "derived-profiles", "list"],
                store,
            )
            show_result = self.invoke(
                ["semantic", "derived-profiles", "show", "bus_stops"],
                store,
            )
            repair_result = self.invoke(
                [
                    "semantic",
                    "derived-profiles",
                    "repair",
                    "bus_stops",
                    "--confirm",
                ],
                store,
            )

        for code, _, stderr in (list_result, show_result, repair_result):
            self.assertEqual(code, 0, stderr)
        self.assertEqual(
            json.loads(repair_result[1])["derivedProfile"]["status"],
            "registering",
        )
        with self.assertRaises(Exception):
            parser().parse_args(
                ["semantic", "derived-profiles", "repair", "bus_stops"]
            )

    def test_derived_profile_list_exposes_and_repairs_dropped_archive_blocker(self):
        blocker = {
            "name": "dropped_stops",
            "relation": "derived_layers.dropped_stops",
            "assetId": "95f2d503-3661-4eca-8a1d-4ff0f16cb719",
            "eventId": "4fc43692-bd6a-48c9-bd84-eb90fdebb642",
            "operation": "archive",
            "generation": 4,
            "status": "repair_required",
            "attempts": 8,
            "lastError": "Permanent semantic archive conflict.",
        }
        repaired = {
            "name": blocker["name"],
            "assetId": blocker["assetId"],
            "generation": blocker["generation"],
            "status": "pending_archive",
            "revision": None,
            "operation": "archive",
        }
        routes = self.routes()
        routes[("GET", "/api/semantic/derived-profiles")] = (
            200,
            {
                "catalogRevision": 7,
                "derivedProfiles": [],
                "deliveryBlockers": [blocker],
                "deliveryBlockersMore": True,
            },
        )

        def repair(request):
            self.assertEqual({"confirmed": True}, request["body"])
            return 200, {
                "catalogRevision": 7,
                "derivedProfile": repaired,
            }

        routes[(
            "POST",
            "/api/semantic/derived-profiles/dropped_stops/repair",
        )] = repair
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            listed = self.invoke(
                [
                    "--output",
                    "human",
                    "semantic",
                    "derived-profiles",
                    "list",
                ],
                store,
            )
            repair_result = self.invoke(
                [
                    "semantic",
                    "derived-profiles",
                    "repair",
                    "dropped_stops",
                    "--confirm",
                ],
                store,
            )

        self.assertEqual(0, listed[0], listed[2])
        self.assertIn("dropped_stops", listed[1])
        self.assertIn("deliveryBlockers", listed[1])
        self.assertIn("deliveryBlockersMore", listed[1])
        self.assertEqual(0, repair_result[0], repair_result[2])
        self.assertEqual(
            "pending_archive",
            json.loads(repair_result[1])["derivedProfile"]["status"],
        )

    def test_derived_delivery_blockers_follow_the_closed_admin_contract(self):
        valid = {
            "name": "dropped_stops",
            "relation": "derived_layers.dropped_stops",
            "assetId": "95f2d503-3661-4eca-8a1d-4ff0f16cb719",
            "eventId": "4fc43692-bd6a-48c9-bd84-eb90fdebb642",
            "operation": "archive",
            "generation": 4,
            "status": "repair_required",
            "attempts": 8,
            "lastError": None,
        }
        missing = dict(valid)
        missing.pop("eventId")
        invalid = (
            {"deliveryBlockersMore": True},
            {"deliveryBlockers": {}},
            {"deliveryBlockers": [], "deliveryBlockersMore": "true"},
            {"deliveryBlockers": [missing]},
            {"deliveryBlockers": [{**valid, "unexpected": True}]},
            {
                "deliveryBlockers": [{
                    **valid,
                    "relation": "derived_layers.other",
                }]
            },
            {"deliveryBlockers": [{**valid, "operation": "drop"}]},
            {"deliveryBlockers": [{**valid, "generation": True}]},
            {"deliveryBlockers": [{**valid, "generation": 0}]},
            {"deliveryBlockers": [{**valid, "status": "delivered"}]},
            {"deliveryBlockers": [{**valid, "attempts": -1}]},
            {"deliveryBlockers": [{**valid, "lastError": " \t"}]},
        )
        results = []
        for update in invalid:
            routes = self.routes()
            routes[("GET", "/api/semantic/derived-profiles")] = (
                200,
                {
                    "catalogRevision": 7,
                    "derivedProfiles": [],
                    **update,
                },
            )
            with tempfile.TemporaryDirectory() as directory, JsonServer(
                routes
            ) as server:
                store = self.configured_store(directory, server.endpoint)
                results.append(self.invoke(
                    ["semantic", "derived-profiles", "list"],
                    store,
                ))

        for code, stdout, stderr in results:
            self.assertEqual(EXIT_CONNECTIVITY, code)
            self.assertEqual("", stdout)
            self.assertEqual(
                "semantic.invalid_response",
                json.loads(stderr)["code"],
            )

    def test_semantic_check_create_and_proposal_lifecycle(self):
        fingerprint = "a" * 64
        operations = [
            {
                "op": "set",
                "path": "/curated/description",
                "value": "Public bus stops",
            }
        ]
        captured = {}
        routes = self.routes()

        def check(request):
            captured["check"] = request["body"]
            return 200, {
                "catalogRevision": 7,
                "check": {
                    **request["body"],
                    "valid": True,
                    "proposalCreated": False,
                    "fingerprint": fingerprint,
                    "diff": [{"path": "/curated/description"}],
                },
            }

        def create(request):
            captured["create"] = request["body"]
            return 201, {
                "catalogRevision": 7,
                "proposal": {
                    "id": "semantic-proposal-1",
                    "assetId": "asset:derived:bus_stops",
                    "baseVersion": 2,
                    "state": "pending",
                    "operations": operations,
                    "explanation": "Describe the derived layer.",
                    "actor": "token:author",
                    "decidedBy": None,
                    "decidedAt": None,
                },
            }

        pending = {
            "id": "semantic-proposal-1",
            "assetId": "asset:derived:bus_stops",
            "baseVersion": 2,
            "state": "pending",
            "operations": operations,
            "explanation": "Describe the derived layer.",
            "actor": "token:author",
            "decidedBy": None,
            "decidedAt": None,
        }
        routes[("POST", "/api/semantic/proposals/check")] = check
        routes[("POST", "/api/semantic/proposals")] = create
        routes[("GET", "/api/semantic/proposals")] = (
            200,
            {"catalogRevision": 7, "proposals": [pending]},
        )
        routes[("GET", "/api/semantic/proposals/semantic-proposal-1")] = (
            200,
            {"catalogRevision": 7, "proposal": pending},
        )
        def apply(request):
            captured["apply"] = request["body"]
            return 200, {
                "catalogRevision": 8,
                "proposal": {
                    **pending,
                    "state": "applied",
                    "appliedVersion": 3,
                    "decidedBy": "token:approver",
                    "decidedAt": "2026-07-26T12:00:00.000Z",
                },
                "asset": {
                    "id": "asset:derived:bus_stops",
                    "version": 3,
                    "status": "ready",
                    "generated": {},
                    "curated": {},
                },
            }

        def decline(request):
            captured["decline"] = request["body"]
            return 200, {
                "catalogRevision": 8,
                "proposal": {
                    **pending,
                    "state": "declined",
                    "decidedBy": "token:reviewer",
                    "decidedAt": "2026-07-26T12:01:00.000Z",
                },
            }

        routes[("POST", "/api/semantic/proposals/semantic-proposal-1/apply")] = apply
        routes[("POST", "/api/semantic/proposals/semantic-proposal-1/decline")] = decline

        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            check_result = self.invoke(
                [
                    "semantic",
                    "proposals",
                    "check",
                    "--asset-id",
                    "asset:derived:bus_stops",
                    "--base-version",
                    "2",
                    "--set",
                    '/curated/description="Public bus stops"',
                    "--explanation",
                    "Describe the derived layer.",
                ],
                store,
            )
            create_result = self.invoke(
                [
                    "semantic",
                    "proposals",
                    "create",
                    "--from-check",
                    fingerprint,
                ],
                store,
            )
            list_result = self.invoke(["semantic", "proposals", "list"], store)
            show_result = self.invoke(
                ["semantic", "proposals", "show", "semantic-proposal-1"],
                store,
            )
            apply_result = self.invoke(
                [
                    "semantic",
                    "proposals",
                    "apply",
                    "semantic-proposal-1",
                    "--confirm",
                ],
                store,
            )
            decline_result = self.invoke(
                [
                    "semantic",
                    "proposals",
                    "decline",
                    "semantic-proposal-1",
                    "--reason",
                    "Superseded",
                    "--confirm",
                ],
                store,
            )

        for code, _, stderr in (
            check_result,
            create_result,
            list_result,
            show_result,
            apply_result,
            decline_result,
        ):
            self.assertEqual(code, 0, stderr)
        self.assertEqual(captured["check"]["assetId"], "asset:derived:bus_stops")
        self.assertEqual(captured["check"]["baseVersion"], 2)
        self.assertEqual(captured["check"]["operations"], operations)
        self.assertEqual(
            captured["create"],
            {
                **captured["check"],
                "fingerprint": fingerprint,
            },
        )
        self.assertEqual(captured["apply"], {"confirmed": True})
        self.assertEqual(
            captured["decline"],
            {"confirmed": True, "reason": "Superseded"},
        )

    def test_semantic_check_and_create_echoes_are_type_sensitive(self):
        fingerprint = "f" * 64
        for action in ("check", "create"):
            for substitution in ("baseVersion", "operations"):
                with self.subTest(action=action, substitution=substitution):
                    routes = self.routes()

                    def check(request):
                        response = {
                            **request["body"],
                            "valid": True,
                            "proposalCreated": False,
                            "fingerprint": fingerprint,
                            "diff": [],
                        }
                        if action == "check":
                            if substitution == "baseVersion":
                                response["baseVersion"] = True
                            else:
                                response["operations"] = [
                                    {
                                        **request["body"]["operations"][0],
                                        "value": 1,
                                    }
                                ]
                        return 200, {
                            "catalogRevision": 7,
                            "check": response,
                        }

                    def create(request):
                        base_version = request["body"]["baseVersion"]
                        operations = request["body"]["operations"]
                        if substitution == "baseVersion":
                            base_version = True
                        else:
                            operations = [{
                                **operations[0],
                                "value": 1,
                            }]
                        return 201, {
                            "catalogRevision": 7,
                            "proposal": self.proposal_response(
                                baseVersion=base_version,
                                operations=operations,
                                explanation=None,
                            ),
                        }

                    routes[("POST", "/api/semantic/proposals/check")] = check
                    routes[("POST", "/api/semantic/proposals")] = create
                    with (
                        tempfile.TemporaryDirectory() as directory,
                        JsonServer(routes) as server,
                    ):
                        store = self.configured_store(directory, server.endpoint)
                        checked = self.invoke(
                            [
                                "semantic", "proposals", "check",
                                "--asset-id", "asset:derived:bus_stops",
                                "--base-version", "1",
                                "--set", "/curated/enabled=true",
                            ],
                            store,
                        )
                        result = (
                            self.invoke(
                                [
                                    "semantic", "proposals", "create",
                                    "--from-check", fingerprint,
                                ],
                                store,
                            )
                            if action == "create" and checked[0] == 0
                            else checked
                        )

                    if action == "create":
                        self.assertEqual(checked[0], 0, checked[2])
                    self.assertEqual(result[0], EXIT_CONNECTIVITY)
                    self.assertEqual(result[1], "")
                    self.assertEqual(
                        json.loads(result[2])["code"],
                        "semantic.invalid_response",
                    )

    def test_semantic_check_accepts_bounded_json_operations(self):
        fingerprint = "b" * 64
        captured = {}

        def check(request):
            captured.update(request["body"])
            return 200, {
                "catalogRevision": 3,
                "check": {
                    **request["body"],
                    "diff": [],
                    "fingerprint": fingerprint,
                },
            }

        routes = self.routes()
        routes[("POST", "/api/semantic/proposals/check")] = check
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            input_path = Path(directory) / "semantic-check.json"
            input_path.write_text(
                json.dumps(
                    {
                        "operations": [
                            {
                                "op": "set",
                                "path": "/curated/unit",
                                "value": "metres",
                            },
                            {
                                "op": "unset",
                                "path": "/curated/obsoleteUnit",
                            },
                        ],
                        "explanation": "Adds the reviewed unit.",
                    }
                ),
                encoding="utf-8",
            )
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "--input",
                    str(input_path),
                    "semantic",
                    "proposals",
                    "check",
                    "--asset-id",
                    "asset:derived:paths",
                    "--base-version",
                    "1",
                ],
                store,
            )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["check"]["fingerprint"], fingerprint)
        self.assertEqual(captured["operations"][0]["path"], "/curated/unit")
        self.assertEqual(captured["operations"][1], {
            "op": "unset",
            "path": "/curated/obsoleteUnit",
        })
        self.assertEqual(captured["explanation"], "Adds the reviewed unit.")

    def test_semantic_apply_failure_is_preserved_and_not_retried(self):
        routes = self.routes()
        routes[("POST", "/api/semantic/proposals/proposal-1/apply")] = (
            504,
            {
                "error": "Semantic apply outcome is indeterminate.",
                "code": "semantic.apply_indeterminate",
                "proposal": {"id": "proposal-1", "state": "applying"},
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "semantic",
                    "proposals",
                    "apply",
                    "proposal-1",
                    "--confirm",
                ],
                store,
            )
            attempts = [
                request
                for request in server.requests
                if request["path"]
                == "/api/semantic/proposals/proposal-1/apply"
            ]

        self.assertEqual(code, EXIT_CONNECTIVITY)
        self.assertEqual(stdout, "")
        self.assertEqual(len(attempts), 1)
        failure = json.loads(stderr)
        self.assertEqual(failure["code"], "semantic.apply_indeterminate")
        self.assertEqual(
            failure["details"]["proposal"]["state"],
            "applying",
        )
        self.assertFalse(
            failure["details"]["reconciliation"]["automaticRetry"]
        )
        self.assertEqual(
            failure["details"]["cause"]["code"],
            "semantic.apply_indeterminate",
        )
        self.assertEqual(
            failure["details"]["response"]["proposal"]["id"],
            "proposal-1",
        )

    def test_semantic_apply_ambiguous_http_status_is_indeterminate(self):
        route = ("POST", "/api/semantic/proposals/proposal-1/apply")
        responses = (
            (
                408,
                {
                    "error": "The response timed out after submission.",
                    "code": "semantic.apply_timeout",
                },
            ),
            (
                307,
                {"error": "Apply endpoint moved."},
                {"Location": "/moved"},
            ),
        )
        for response in responses:
            with self.subTest(status=response[0]):
                routes = self.routes()
                routes[route] = response
                with (
                    tempfile.TemporaryDirectory() as directory,
                    JsonServer(routes) as server,
                ):
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        [
                            "semantic", "proposals", "apply",
                            "proposal-1", "--confirm",
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
                    "semantic.apply_indeterminate",
                )
                self.assertEqual(
                    failure["details"]["cause"]["httpStatus"],
                    response[0],
                )
                self.assertFalse(
                    failure["details"]["reconciliation"]["automaticRetry"]
                )
                self.assertEqual(len(attempts), 1)

    def test_semantic_apply_known_409_remains_authoritative(self):
        routes = self.routes()
        route = ("POST", "/api/semantic/proposals/proposal-1/apply")
        routes[route] = (
            409,
            {
                "error": "The semantic proposal is stale.",
                "code": "semantic.version_conflict",
                "stateUnchanged": True,
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                [
                    "semantic", "proposals", "apply",
                    "proposal-1", "--confirm",
                ],
                store,
            )

        self.assertEqual(code, EXIT_CONFLICT)
        self.assertEqual(stdout, "")
        self.assertEqual(
            json.loads(stderr)["code"],
            "semantic.version_conflict",
        )

    def test_semantic_apply_interruption_is_indeterminate(self):
        routes = self.routes()
        original_request = ApiClient.request
        apply_attempts = 0

        def interrupt_apply(client, path, *args, **kwargs):
            nonlocal apply_attempts
            if path == "/api/semantic/proposals/proposal-1/apply":
                apply_attempts += 1
                raise KeyboardInterrupt
            return original_request(client, path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            with patch.object(ApiClient, "request", new=interrupt_apply):
                code, stdout, stderr = self.invoke(
                    [
                        "semantic", "proposals", "apply",
                        "proposal-1", "--confirm",
                    ],
                    store,
                )

        self.assertEqual(code, EXIT_INTERRUPTED)
        self.assertEqual(stdout, "")
        failure = json.loads(stderr)
        self.assertEqual(failure["code"], "semantic.apply_indeterminate")
        self.assertTrue(failure["details"]["interrupted"])
        self.assertFalse(
            failure["details"]["reconciliation"]["automaticRetry"]
        )
        self.assertEqual(apply_attempts, 1)

    def test_every_semantic_command_requires_its_exact_contract_capability(self):
        invocations = {
            "semantic status": ["semantic", "status"],
            "semantic catalog export": [
                "semantic",
                "catalog",
                "export",
            ],
            "semantic catalog search": [
                "semantic",
                "catalog",
                "search",
                "bus",
            ],
            "semantic catalog show": [
                "semantic",
                "catalog",
                "show",
                "asset-1",
            ],
            "semantic catalog history": [
                "semantic",
                "catalog",
                "history",
                "asset-1",
            ],
            "semantic catalog archive": [
                "semantic",
                "catalog",
                "archive",
                "asset-1",
                "--confirm",
            ],
            "semantic source relations": [
                "semantic",
                "source",
                "relations",
            ],
            "semantic source archive-excluded": [
                "semantic",
                "source",
                "archive-excluded",
                "--confirm",
            ],
            "semantic source sync": [
                "semantic",
                "source",
                "sync",
                "--alias",
                "main",
                "--schema",
                "leeds",
                "--relation",
                "census_2021_england_oa",
                "--confirm",
            ],
            "semantic generate table": [
                "semantic",
                "generate",
                "table",
                "asset-1",
            ],
            "semantic generate field": [
                "semantic",
                "generate",
                "field",
                "asset-1",
                "field-1",
            ],
            "semantic derived-profiles list": [
                "semantic",
                "derived-profiles",
                "list",
            ],
            "semantic derived-profiles show": [
                "semantic",
                "derived-profiles",
                "show",
                "profile-1",
            ],
            "semantic derived-profiles repair": [
                "semantic",
                "derived-profiles",
                "repair",
                "profile-1",
                "--confirm",
            ],
            "semantic proposals check": [
                "semantic",
                "proposals",
                "check",
                "--asset-id",
                "asset-1",
                "--base-version",
                "1",
            ],
            "semantic proposals create": [
                "semantic",
                "proposals",
                "create",
                "--from-check",
                "a" * 64,
            ],
            "semantic proposals list": [
                "semantic",
                "proposals",
                "list",
            ],
            "semantic proposals show": [
                "semantic",
                "proposals",
                "show",
                "proposal-1",
            ],
            "semantic proposals apply": [
                "semantic",
                "proposals",
                "apply",
                "proposal-1",
                "--confirm",
            ],
            "semantic proposals decline": [
                "semantic",
                "proposals",
                "decline",
                "proposal-1",
                "--confirm",
            ],
        }
        self.assertEqual(set(SEMANTIC_COMMANDS), set(invocations))
        routes = self.routes()
        advertised = routes[("GET", "/api/contract")][1]["commands"]
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            for command, arguments in invocations.items():
                with self.subTest(command=command):
                    advertised.remove(command)
                    request_offset = len(server.requests)
                    code, stdout, stderr = self.invoke(arguments, store)
                    requests = server.requests[request_offset:]
                    advertised.append(command)

                    self.assertEqual(EXIT_CONFLICT, code)
                    self.assertEqual("", stdout)
                    self.assertEqual(
                        command,
                        json.loads(stderr)["details"]["requiredCommand"],
                    )
                    self.assertFalse(
                        any(
                            request["path"].startswith("/api/semantic/")
                            for request in requests
                        )
                    )

    def test_semantic_revisions_reject_bool_and_float_values(self):
        routes = self.routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            results = []
            for value in (True, 7.5):
                routes[("GET", "/api/semantic/status")] = (
                    200,
                    {
                        "ok": True,
                        "schemaVersion": 1,
                        "catalogRevision": value,
                        "capabilities": {},
                    },
                )
                results.append((
                    f"catalogRevision={value!r}",
                    self.invoke(["semantic", "status"], store),
                ))

                routes[("GET", "/api/semantic/proposals")] = (
                    200,
                    {
                        "catalogRevision": 7,
                        "proposals": [
                            self.proposal_response(baseVersion=value)
                        ],
                    },
                )
                results.append((
                    f"baseVersion={value!r}",
                    self.invoke(["semantic", "proposals", "list"], store),
                ))

                routes[("GET", "/api/semantic/catalog")] = (
                    200,
                    {
                        "catalogRevision": 7,
                        "assets": [{
                            "id": "asset:derived:bus_stops",
                            "version": value,
                            "status": "ready",
                            "generated": {},
                            "curated": {},
                        }],
                    },
                )
                results.append((
                    f"assetVersion={value!r}",
                    self.invoke(["semantic", "catalog", "export"], store),
                ))

                routes[("GET", "/api/semantic/derived-profiles")] = (
                    200,
                    {
                        "catalogRevision": 7,
                        "derivedProfiles": [{
                            "name": "bus_stops",
                            "assetId": "asset:derived:bus_stops",
                            "generation": 1,
                            "status": "ready",
                            "revision": value,
                        }],
                    },
                )
                results.append((
                    f"profileRevision={value!r}",
                    self.invoke(
                        ["semantic", "derived-profiles", "list"],
                        store,
                    ),
                ))

        for label, (code, stdout, stderr) in results:
            with self.subTest(label=label):
                self.assertEqual(EXIT_CONNECTIVITY, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "semantic.invalid_response",
                    json.loads(stderr)["code"],
                )

    def test_semantic_collections_reject_blank_response_identities(self):
        routes = self.routes()
        cases = (
            (
                "catalog asset id",
                ["semantic", "catalog", "export"],
                ("GET", "/api/semantic/catalog"),
                {
                    "catalogRevision": 7,
                    "assets": [{
                        "id": " \t",
                        "version": 1,
                        "status": "ready",
                        "generated": {},
                        "curated": {},
                    }],
                },
            ),
            (
                "search result id",
                ["semantic", "catalog", "search", "bus"],
                ("GET", "/api/semantic/catalog/search"),
                {
                    "catalogRevision": 7,
                    "query": "bus",
                    "results": [{
                        "id": " \t",
                        "version": 1,
                    }],
                },
            ),
            (
                "profile name",
                ["semantic", "derived-profiles", "list"],
                ("GET", "/api/semantic/derived-profiles"),
                {
                    "catalogRevision": 7,
                    "derivedProfiles": [{
                        "name": " \t",
                        "assetId": "asset:derived:bus_stops",
                        "generation": 1,
                        "status": "ready",
                        "revision": "7",
                    }],
                },
            ),
            (
                "profile asset id",
                ["semantic", "derived-profiles", "list"],
                ("GET", "/api/semantic/derived-profiles"),
                {
                    "catalogRevision": 7,
                    "derivedProfiles": [{
                        "name": "bus_stops",
                        "assetId": " \t",
                        "generation": 1,
                        "status": "ready",
                        "revision": "7",
                    }],
                },
            ),
            (
                "proposal id",
                ["semantic", "proposals", "list"],
                ("GET", "/api/semantic/proposals"),
                {
                    "catalogRevision": 7,
                    "proposals": [self.proposal_response(id=" \t")],
                },
            ),
            (
                "proposal asset id",
                ["semantic", "proposals", "list"],
                ("GET", "/api/semantic/proposals"),
                {
                    "catalogRevision": 7,
                    "proposals": [
                        self.proposal_response(assetId=" \t")
                    ],
                },
            ),
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            results = []
            for label, command, route, payload in cases:
                routes[route] = (200, payload)
                results.append((label, self.invoke(command, store)))

        for label, (code, stdout, stderr) in results:
            with self.subTest(label=label):
                self.assertEqual(EXIT_CONNECTIVITY, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "semantic.invalid_response",
                    json.loads(stderr)["code"],
                )

    def test_pending_decision_metadata_is_rejected_but_legacy_nulls_are_valid(self):
        routes = self.routes()
        pending_decisions = (
            {"decidedBy": "token:reviewer"},
            {"decidedAt": "2026-07-26T12:00:00.000Z"},
            {
                "decidedBy": "token:reviewer",
                "decidedAt": "2026-07-26T12:00:00.000Z",
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            rejected = []
            for metadata in pending_decisions:
                routes[("GET", "/api/semantic/proposals")] = (
                    200,
                    {
                        "catalogRevision": 7,
                        "proposals": [
                            self.proposal_response(**metadata)
                        ],
                    },
                )
                rejected.append(self.invoke(
                    ["semantic", "proposals", "list"],
                    store,
                ))

            routes[("GET", "/api/semantic/proposals")] = (
                200,
                {
                    "catalogRevision": 7,
                    "proposals": [
                        self.proposal_response(
                            state="applied",
                            appliedVersion=3,
                        ),
                        self.proposal_response(
                            id="semantic-proposal-2",
                            state="declined",
                        ),
                    ],
                },
            )
            legacy = self.invoke(["semantic", "proposals", "list"], store)

        for result in rejected:
            self.assertEqual(EXIT_CONNECTIVITY, result[0])
            self.assertEqual("", result[1])
            self.assertEqual(
                "semantic.invalid_response",
                json.loads(result[2])["code"],
            )
        self.assertEqual(0, legacy[0], legacy[2])
        self.assertEqual(
            ["applied", "declined"],
            [
                proposal["state"]
                for proposal in json.loads(legacy[1])["proposals"]
            ],
        )

    def test_semantic_proposals_reject_generated_profile_mutations_locally(self):
        routes = self.routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            input_path = Path(directory) / "generated-operation.json"
            input_path.write_text(
                json.dumps({
                    "operations": [{
                        "op": "set",
                        "path": "/generated/description",
                        "value": "unsafe",
                    }]
                }),
                encoding="utf-8",
            )
            commands = (
                [
                    "semantic",
                    "proposals",
                    "check",
                    "--asset-id",
                    "asset:derived:bus_stops",
                    "--base-version",
                    "2",
                    "--set",
                    '/generated/description="unsafe"',
                ],
                [
                    "semantic",
                    "proposals",
                    "check",
                    "--asset-id",
                    "asset:derived:bus_stops",
                    "--base-version",
                    "2",
                    "--unset",
                    "/generated/description",
                ],
                [
                    "semantic",
                    "proposals",
                    "check",
                    "--asset-id",
                    "asset:derived:bus_stops",
                    "--base-version",
                    "2",
                    "--set",
                    '/curatedness="unsafe"',
                ],
                [
                    "--input",
                    str(input_path),
                    "semantic",
                    "proposals",
                    "check",
                    "--asset-id",
                    "asset:derived:bus_stops",
                    "--base-version",
                    "2",
                ],
            )
            results = [
                self.invoke(command, store)
                for command in commands
            ]
            posted = [
                request
                for request in server.requests
                if request["path"] == "/api/semantic/proposals/check"
            ]

        for command, (code, stdout, stderr) in zip(commands, results):
            with self.subTest(command=command):
                self.assertEqual(code, EXIT_USAGE)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    json.loads(stderr)["code"],
                    "semantic.operation.generated_read_only",
                )
        self.assertEqual(posted, [])

    def test_json_semantic_operations_follow_the_closed_server_schema(self):
        invalid_operations = (
            [{"op": "set", "path": "/curated/unit"}],
            [{
                "op": "set",
                "path": "/curated/unit",
                "value": "metres",
                "extra": True,
            }],
            [{
                "op": "unset",
                "path": "/curated/unit",
                "value": "metres",
            }],
            [{"op": "merge", "path": "/curated/unit"}],
            [{"op": "set", "path": 7, "value": "metres"}],
            [{"op": "unset", "path": "/curated"}],
            [{"op": "set", "path": "/curated", "value": []}],
            [{
                "op": "set",
                "path": "/curated//unit",
                "value": "metres",
            }],
            [{
                "op": "set",
                "path": "/curated/unit~2",
                "value": "metres",
            }],
            [
                {
                    "op": "set",
                    "path": "/curated/unit",
                    "value": "metres",
                },
                {
                    "op": "unset",
                    "path": "/curated/unit",
                },
            ],
            [
                {
                    "op": "set",
                    "path": f"/curated/key{index}",
                    "value": index,
                }
                for index in range(101)
            ],
        )
        routes = self.routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            input_path = Path(directory) / "semantic-operation.json"
            results = []
            for operations in invalid_operations:
                input_path.write_text(
                    json.dumps({"operations": operations}),
                    encoding="utf-8",
                )
                results.append(self.invoke(
                    [
                        "--input",
                        str(input_path),
                        "semantic",
                        "proposals",
                        "check",
                        "--asset-id",
                        "asset:derived:bus_stops",
                        "--base-version",
                        "2",
                    ],
                    store,
                ))
            posted = [
                request
                for request in server.requests
                if request["path"] == "/api/semantic/proposals/check"
            ]

        for operations, (code, stdout, stderr) in zip(
            invalid_operations,
            results,
        ):
            with self.subTest(operations=operations):
                self.assertEqual(EXIT_USAGE, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "semantic.operation.invalid_input",
                    json.loads(stderr)["code"],
                )
        self.assertEqual([], posted)

    def test_semantic_mutations_require_confirmation_before_network(self):
        commands = (
            [
                "semantic",
                "source",
                "sync",
                "--alias",
                "main",
                "--schema",
                "leeds",
                "--relation",
                "census",
            ],
            [
                "semantic",
                "derived-profiles",
                "repair",
                "bus_stops",
            ],
            [
                "semantic",
                "proposals",
                "apply",
                "semantic-proposal-1",
            ],
            [
                "semantic",
                "proposals",
                "decline",
                "semantic-proposal-1",
            ],
        )
        routes = self.routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            results = [self.invoke(command, store) for command in commands]

        for command, (code, stdout, stderr) in zip(commands, results):
            with self.subTest(command=command):
                self.assertEqual(EXIT_USAGE, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "usage.invalid_arguments",
                    json.loads(stderr)["code"],
                )
        self.assertEqual([], server.requests)

    def test_semantic_decline_and_repair_failures_are_not_retried(self):
        cases = (
            (
                [
                    "semantic",
                    "proposals",
                    "decline",
                    "semantic-proposal-1",
                    "--confirm",
                ],
                (
                    "POST",
                    "/api/semantic/proposals/"
                    "semantic-proposal-1/decline",
                ),
            ),
            (
                [
                    "semantic",
                    "derived-profiles",
                    "repair",
                    "bus_stops",
                    "--confirm",
                ],
                (
                    "POST",
                    "/api/semantic/derived-profiles/bus_stops/repair",
                ),
            ),
        )
        routes = self.routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            results = []
            for command, route in cases:
                routes[route] = (
                    503,
                    {
                        "error": "Semantic service is unavailable.",
                        "code": "semantic.unavailable",
                    },
                )
                before = len(server.requests)
                result = self.invoke(command, store)
                attempts = [
                    request
                    for request in server.requests[before:]
                    if (request["method"], request["path"]) == route
                ]
                results.append((command, result, attempts))

        for command, (code, stdout, stderr), attempts in results:
            with self.subTest(command=command):
                self.assertEqual(EXIT_CONNECTIVITY, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "semantic.unavailable",
                    json.loads(stderr)["code"],
                )
                self.assertEqual(1, len(attempts))

    def test_semantic_nested_success_envelopes_fail_closed(self):
        cases = (
            (
                ["semantic", "catalog", "show", "asset-1"],
                ("GET", "/api/semantic/catalog/objects/asset-1"),
                {"catalogRevision": 7, "asset": []},
            ),
            (
                ["semantic", "derived-profiles", "show", "bus_stops"],
                ("GET", "/api/semantic/derived-profiles/bus_stops"),
                {"catalogRevision": 7, "derivedProfile": []},
            ),
            (
                [
                    "semantic",
                    "derived-profiles",
                    "repair",
                    "bus_stops",
                    "--confirm",
                ],
                (
                    "POST",
                    "/api/semantic/derived-profiles/bus_stops/repair",
                ),
                {"catalogRevision": 7, "derivedProfile": []},
            ),
            (
                [
                    "semantic",
                    "proposals",
                    "show",
                    "semantic-proposal-1",
                ],
                (
                    "GET",
                    "/api/semantic/proposals/semantic-proposal-1",
                ),
                {"catalogRevision": 7, "proposal": []},
            ),
            (
                [
                    "semantic",
                    "proposals",
                    "check",
                    "--asset-id",
                    "asset:derived:bus_stops",
                    "--base-version",
                    "2",
                    "--set",
                    '/curated/description="Bus stops"',
                ],
                ("POST", "/api/semantic/proposals/check"),
                {"catalogRevision": 7, "check": []},
            ),
            (
                [
                    "semantic",
                    "proposals",
                    "decline",
                    "semantic-proposal-1",
                    "--confirm",
                ],
                (
                    "POST",
                    "/api/semantic/proposals/"
                    "semantic-proposal-1/decline",
                ),
                {
                    "catalogRevision": 7,
                    "proposal": self.proposal_response(),
                },
            ),
        )
        routes = self.routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            results = []
            for command, route, payload in cases:
                routes[route] = (200, payload)
                results.append((command, self.invoke(command, store)))

        for command, (code, stdout, stderr) in results:
            with self.subTest(command=command):
                self.assertEqual(EXIT_CONNECTIVITY, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "semantic.invalid_response",
                    json.loads(stderr)["code"],
                )

    def test_malformed_semantic_apply_success_is_indeterminate_and_not_retried(self):
        applied = self.proposal_response(
            state="applied",
            appliedVersion=3,
            decidedBy="token:approver",
            decidedAt="2026-07-26T12:00:00.000Z",
        )
        asset = {
            "id": "asset:derived:bus_stops",
            "version": 3,
            "status": "ready",
            "generated": {},
            "curated": {},
        }
        payloads = (
            b"[]",
            {
                "catalogRevision": 7,
                "proposal": [],
                "asset": asset,
            },
            {
                "catalogRevision": 7,
                "proposal": applied,
                "asset": [],
            },
            {
                "catalogRevision": 7,
                "proposal": applied,
                "asset": {**asset, "version": 4},
            },
        )
        route = (
            "POST",
            "/api/semantic/proposals/semantic-proposal-1/apply",
        )
        routes = self.routes()
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            results = []
            for payload in payloads:
                routes[route] = (200, payload)
                results.append(self.invoke(
                    [
                        "semantic",
                        "proposals",
                        "apply",
                        "semantic-proposal-1",
                        "--confirm",
                    ],
                    store,
                ))
            attempts = [
                request
                for request in server.requests
                if (request["method"], request["path"]) == route
            ]

        self.assertEqual(len(payloads), len(attempts))
        for code, stdout, stderr in results:
            failure = json.loads(stderr)
            with self.subTest(failure=failure):
                self.assertEqual(EXIT_CONNECTIVITY, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "semantic.apply_indeterminate",
                    failure["code"],
                )
                reconciliation = failure["details"]["reconciliation"]
                self.assertTrue(reconciliation["required"])
                self.assertFalse(reconciliation["automaticRetry"])
                self.assertEqual(
                    {
                        "command": "config-cli semantic proposals show",
                        "arguments": ["semantic-proposal-1"],
                    },
                    reconciliation["commands"][0],
                )
                if len(reconciliation["commands"]) > 1:
                    self.assertEqual(
                        "config-cli semantic catalog show",
                        reconciliation["commands"][1]["command"],
                    )

    def test_semantic_commands_reject_malformed_success_responses(self):
        cases = [
            (["semantic", "status"], ("GET", "/api/semantic/status")),
            (
                ["semantic", "catalog", "export"],
                ("GET", "/api/semantic/catalog"),
            ),
            (
                ["semantic", "catalog", "search", "bus"],
                ("GET", "/api/semantic/catalog/search"),
            ),
            (
                ["semantic", "catalog", "show", "asset"],
                ("GET", "/api/semantic/catalog/objects/asset"),
            ),
            (
                ["semantic", "source", "relations"],
                ("GET", "/api/semantic/source/relations"),
            ),
            (
                ["semantic", "derived-profiles", "list"],
                ("GET", "/api/semantic/derived-profiles"),
            ),
            (
                ["semantic", "proposals", "list"],
                ("GET", "/api/semantic/proposals"),
            ),
        ]
        for arguments, route in cases:
            with self.subTest(arguments=arguments):
                routes = self.routes()
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
                    "semantic.invalid_response",
                )

    def test_semantic_proposals_validate_creator_and_decision_metadata(self):
        valid = {
            "id": "semantic-proposal-1",
            "assetId": "asset:derived:bus_stops",
            "baseVersion": 2,
            "state": "pending",
            "operations": [],
            "actor": "token:author",
            "decidedBy": None,
            "decidedAt": None,
        }
        missing = object()
        cases = (
            ("missing actor", "actor", missing),
            ("empty actor", "actor", ""),
            ("blank actor", "actor", " "),
            ("missing decidedBy", "decidedBy", missing),
            ("empty decidedBy", "decidedBy", ""),
            ("blank decidedBy", "decidedBy", " "),
            ("invalid decidedBy", "decidedBy", 7),
            ("missing decidedAt", "decidedAt", missing),
            ("empty decidedAt", "decidedAt", ""),
            ("blank decidedAt", "decidedAt", " "),
            ("invalid decidedAt", "decidedAt", []),
        )
        for label, key, value in cases:
            with self.subTest(label=label):
                proposal = dict(valid)
                if value is missing:
                    proposal.pop(key)
                else:
                    proposal[key] = value
                routes = self.routes()
                routes[("GET", "/api/semantic/proposals")] = (
                    200,
                    {
                        "catalogRevision": 7,
                        "proposals": [proposal],
                    },
                )
                with tempfile.TemporaryDirectory() as directory, JsonServer(
                    routes
                ) as server:
                    store = self.configured_store(directory, server.endpoint)
                    code, stdout, stderr = self.invoke(
                        ["semantic", "proposals", "list"],
                        store,
                    )

                self.assertEqual(EXIT_CONNECTIVITY, code)
                self.assertEqual("", stdout)
                self.assertEqual(
                    "semantic.invalid_response",
                    json.loads(stderr)["code"],
                )

    def test_derived_layer_output_preserves_and_validates_semantic_profile(self):
        semantic_profile = {
            "assetId": "asset:derived:bus_stops",
            "generation": 2,
            "status": "ready",
            "revision": "7",
        }
        routes = self.routes()
        routes[("GET", "/api/derived-layers/bus_stops")] = (
            200,
            {
                "derivedLayer": {
                    "name": "bus_stops",
                    "semanticProfile": semantic_profile,
                }
            },
        )
        with tempfile.TemporaryDirectory() as directory, JsonServer(routes) as server:
            store = self.configured_store(directory, server.endpoint)
            code, stdout, stderr = self.invoke(
                ["derived-layers", "show", "bus_stops"],
                store,
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            json.loads(stdout)["derivedLayer"]["semanticProfile"],
            semantic_profile,
        )


if __name__ == "__main__":
    unittest.main()
