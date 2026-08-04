from __future__ import annotations

import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from mapp_config_cli.client import (
    ApiClient,
    MAX_REQUEST_BYTES,
    contract_major,
    normalize_endpoint,
    verify_target,
)
from mapp_config_cli.config import Profile
from mapp_config_cli.errors import (
    CliError,
    EXIT_AUTHENTICATION,
    EXIT_CONFLICT,
    EXIT_CONNECTIVITY,
    EXIT_VALIDATION,
    EXIT_VISUAL,
)

from tests.support import JsonServer


class EndpointTests(unittest.TestCase):
    def test_normalizes_root_endpoint(self):
        self.assertEqual(
            normalize_endpoint("https://CONFIG.Example.COM:443/"),
            "https://config.example.com",
        )
        self.assertEqual(
            normalize_endpoint("http://localhost:8080/"),
            "http://localhost:8080",
        )

    def test_rejects_userinfo_query_fragment_and_path(self):
        for endpoint in (
            "https://user:password@example.com",
            "https://example.com/?token=x",
            "https://example.com/#fragment",
            "https://example.com/config",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(CliError):
                normalize_endpoint(endpoint)

    def test_remote_http_requires_explicit_permission(self):
        with self.assertRaises(CliError):
            normalize_endpoint("http://example.com")
        self.assertEqual(
            normalize_endpoint("http://example.com", allow_http=True),
            "http://example.com",
        )

    def test_timeout_must_be_finite_and_positive(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(CliError):
                ApiClient("http://localhost", timeout=timeout)


class TransportTests(unittest.TestCase):
    def test_request_serializes_non_ascii_json_as_compact_utf8(self):
        client = ApiClient("http://localhost", "token")
        with patch.object(
            client.opener,
            "open",
            side_effect=urllib.error.URLError("synthetic stop"),
        ) as mocked_open:
            with self.assertRaises(CliError):
                client.request(
                    "/api/test",
                    method="POST",
                    payload={"label": "café"},
                )

        request = mocked_open.call_args.args[0]
        self.assertEqual(request.data, b'{"label":"caf\xc3\xa9"}')

    def test_request_allows_exactly_five_mib_serialized_body(self):
        empty_payload = b'{"value":""}'
        payload = {"value": "x" * (MAX_REQUEST_BYTES - len(empty_payload))}
        client = ApiClient("http://localhost", "token")
        with patch.object(
            client.opener,
            "open",
            side_effect=urllib.error.URLError("synthetic stop"),
        ) as mocked_open:
            with self.assertRaises(CliError):
                client.request("/api/test", method="POST", payload=payload)

        request = mocked_open.call_args.args[0]
        self.assertEqual(len(request.data), MAX_REQUEST_BYTES)

    def test_request_rejects_serialized_body_over_five_mib_without_network(self):
        empty_payload = b'{"value":""}'
        payload = {
            "value": "x" * (MAX_REQUEST_BYTES - len(empty_payload) + 1),
        }
        client = ApiClient("http://localhost", "token")
        with patch.object(client.opener, "open") as mocked_open:
            with self.assertRaises(CliError) as raised:
                client.request("/api/test", method="POST", payload=payload)

        self.assertEqual(raised.exception.exit_code, EXIT_VALIDATION)
        self.assertEqual(raised.exception.error_code, "client.payload_too_large")
        self.assertEqual(
            raised.exception.safe_details,
            {
                "requestBytes": MAX_REQUEST_BYTES + 1,
                "maxRequestBytes": MAX_REQUEST_BYTES,
            },
        )
        mocked_open.assert_not_called()

    def test_localhost_subdomain_uses_configured_transport_hostname(self):
        client = ApiClient("http://config.localhost:3000")
        with patch.object(
            client.opener,
            "open",
            side_effect=urllib.error.URLError("synthetic stop"),
        ) as mocked_open:
            with self.assertRaises(CliError):
                client.request("/api/public/identity", authenticated=False)

        request = mocked_open.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://config.localhost:3000/api/public/identity",
        )
        self.assertIsNone(request.get_header("Host"))

    def test_request_id_header_is_preserved_as_response_metadata(self):
        routes = {
            ("GET", "/ok"): (
                200,
                {"value": True},
                {"X-Request-ID": "request-123"},
            ),
        }
        with JsonServer(routes) as server:
            result = ApiClient(server.endpoint, "token").request("/ok")
        self.assertEqual("request-123", result["meta"]["requestId"])

    def test_rejects_redirect_without_forwarding_authorization(self):
        received: list[str | None] = []

        class Target(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{target.server_port}/capture",
                )
                self.end_headers()

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            client = ApiClient(
                f"http://127.0.0.1:{redirect.server_port}",
                "secret-token",
            )
            with self.assertRaises(CliError) as raised:
                client.request("/api/test")
            self.assertEqual(raised.exception.exit_code, EXIT_CONNECTIVITY)
            self.assertEqual(received, [])
        finally:
            redirect.shutdown()
            target.shutdown()
            redirect.server_close()
            target.server_close()
            redirect_thread.join(timeout=2)
            target_thread.join(timeout=2)

    def test_preserves_structured_http_error_details(self):
        routes = {
            ("GET", "/api/test"): (
                422,
                {
                    "error": "Validation failed.",
                    "errors": [
                        {
                            "path": "locale.layers.Example",
                            "ruleId": "workspace.structure",
                        }
                    ],
                },
            )
        }
        with JsonServer(routes) as server:
            client = ApiClient(server.endpoint, "token")
            with self.assertRaises(CliError) as raised:
                client.request("/api/test")
        payload = raised.exception.payload()
        self.assertEqual(payload["details"]["errors"][0]["ruleId"], "workspace.structure")

    def test_preserves_semantic_server_error_code(self):
        routes = {
            ("GET", "/api/semantic/catalog/objects/missing"): (
                404,
                {
                    "error": "Semantic asset does not exist.",
                    "code": "semantic.asset_missing",
                },
            )
        }
        with JsonServer(routes) as server:
            client = ApiClient(server.endpoint, "token")
            with self.assertRaises(CliError) as raised:
                client.request("/api/semantic/catalog/objects/missing")
        self.assertEqual(
            raised.exception.error_code,
            "semantic.asset_missing",
        )

    def test_preserves_valid_namespaced_server_error_codes(self):
        codes = (
            "operation.not_found",
            "workspace.fingerprint_conflict",
            "proposal.approval_required",
            "xyz.confirmation_required",
            "locale.not_found",
        )
        routes = {
            ("GET", f"/api/errors/{index}"): (
                404,
                {"error": "Expected failure.", "code": code},
            )
            for index, code in enumerate(codes)
        }
        with JsonServer(routes) as server:
            client = ApiClient(server.endpoint, "token")
            for index, code in enumerate(codes):
                with self.subTest(code=code), self.assertRaises(CliError) as raised:
                    client.request(f"/api/errors/{index}")
                self.assertEqual(raised.exception.error_code, code)

    def test_rejects_malformed_server_error_codes(self):
        codes = (
            "operation",
            ".operation.not_found",
            "operation.",
            "operation..not_found",
            "Operation.not_found",
            "operation.not-found",
            "operation.not_found\nspoofed.code",
            42,
        )
        routes = {
            ("GET", f"/api/errors/{index}"): (
                404,
                {"error": "Expected failure.", "code": code},
            )
            for index, code in enumerate(codes)
        }
        with JsonServer(routes) as server:
            client = ApiClient(server.endpoint, "token")
            for index, code in enumerate(codes):
                with self.subTest(code=code), self.assertRaises(CliError) as raised:
                    client.request(f"/api/errors/{index}")
                self.assertEqual(raised.exception.error_code, "api.http_error")

    def test_download_preserves_valid_namespaced_server_error_code(self):
        with JsonServer(
            {
                ("GET", "/api/evidence/missing"): (
                    404,
                    {
                        "error": "Operation does not exist.",
                        "code": "operation.not_found",
                    },
                )
            }
        ) as server:
            client = ApiClient(server.endpoint, "token")
            with self.assertRaises(CliError) as raised:
                client.request_bytes("/api/evidence/missing")

        self.assertEqual(raised.exception.error_code, "operation.not_found")

    def test_download_rejects_malformed_server_error_code(self):
        with JsonServer(
            {
                ("GET", "/api/evidence/missing"): (
                    404,
                    {
                        "error": "Operation does not exist.",
                        "code": "operation.not_found\nspoofed.code",
                    },
                )
            }
        ) as server:
            client = ApiClient(server.endpoint, "token")
            with self.assertRaises(CliError) as raised:
                client.request_bytes("/api/evidence/missing")

        self.assertEqual(raised.exception.error_code, "api.http_error")

    def test_preserves_authorization_scope_error_code(self):
        routes = {
            ("POST", "/api/semantic/generate"): (
                403,
                {
                    "error": "The credential does not grant the required scope.",
                    "code": "auth.scope_required",
                    "requiredScope": "semantic:generate",
                    "grantedScopes": ["semantic:inspect"],
                },
            )
        }
        with JsonServer(routes) as server:
            client = ApiClient(server.endpoint, "token")
            with self.assertRaises(CliError) as raised:
                client.request(
                    "/api/semantic/generate",
                    method="POST",
                    payload={
                        "assetId": "asset:roads",
                        "target": {"kind": "table"},
                    },
                )
        self.assertEqual(raised.exception.exit_code, EXIT_AUTHENTICATION)
        self.assertEqual(
            raised.exception.error_code,
            "auth.scope_required",
        )
        self.assertEqual(
            raised.exception.safe_details["requiredScope"],
            "semantic:generate",
        )

    def test_visual_gateway_timeout_uses_visual_exit_code(self):
        with JsonServer(
            {
                ("POST", "/api/visual-test"): (
                    504,
                    {
                        "error": "Browser verification timed out.",
                        "run": {"status": "timed-out"},
                    },
                )
            }
        ) as server:
            client = ApiClient(server.endpoint, "token")
            with self.assertRaises(CliError) as raised:
                client.request(
                    "/api/visual-test",
                    method="POST",
                    payload={"layer": "Bus Stops"},
                    failure_code=EXIT_VISUAL,
                )
        self.assertEqual(raised.exception.exit_code, EXIT_VISUAL)
        self.assertEqual(raised.exception.http_status, 504)
        self.assertEqual(
            raised.exception.payload()["details"]["run"]["status"],
            "timed-out",
        )

    def test_visual_planning_code_and_safe_stage_are_preserved(self):
        response = {
            "error": "Visual planning timed out before browser validation began.",
            "code": "visual.planning_timeout",
            "planningStage": "layer-summary",
            "queryPurpose": "feature-count-and-extent",
            "timeoutMilliseconds": 5000,
        }
        with JsonServer(
            {
                ("POST", "/api/visual-test"): (
                    422,
                    response,
                )
            }
        ) as server:
            client = ApiClient(server.endpoint, "token")
            with self.assertRaises(CliError) as raised:
                client.request(
                    "/api/visual-test",
                    method="POST",
                    payload={"layer": "Bus Stops"},
                    failure_code=EXIT_VISUAL,
                )

        error = raised.exception
        self.assertEqual(EXIT_VISUAL, error.exit_code)
        self.assertEqual("visual.planning_timeout", error.error_code)
        self.assertEqual("layer-summary", error.safe_details["planningStage"])
        self.assertEqual(
            "feature-count-and-extent",
            error.safe_details["queryPurpose"],
        )

    def test_non_json_response_is_structured(self):
        with JsonServer(
            {
                ("GET", "/api/test"): (
                    200,
                    b"<html>not json</html>",
                    {"Content-Type": "text/html"},
                )
            }
        ) as server:
            client = ApiClient(server.endpoint, "token")
            with self.assertRaises(CliError) as raised:
                client.request("/api/test")
        self.assertEqual(raised.exception.error_code, "api.non_json_response")

    def test_server_error_cannot_echo_the_bearer_token(self):
        token = "synthetic-secret-token"
        with JsonServer(
            {
                ("GET", "/api/test"): (
                    500,
                    {
                        "error": f"Upstream rejected {token}",
                        "accessToken": token,
                        "nested": {"message": f"value={token}"},
                    },
                )
            }
        ) as server:
            client = ApiClient(server.endpoint, token)
            with self.assertRaises(CliError) as raised:
                client.request("/api/test")
        rendered = str(raised.exception.payload())
        self.assertNotIn(token, rendered)
        self.assertIn("[redacted]", rendered)

    def test_success_response_cannot_echo_the_bearer_token(self):
        token = "synthetic-secret-token"
        with JsonServer(
            {("GET", "/api/test"): (200, {"message": f"value={token}"})}
        ) as server:
            result = ApiClient(server.endpoint, token).request("/api/test")
        self.assertEqual(result["message"], "value=[redacted]")


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, path, **kwargs):
        self.requests.append((path, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, CliError):
            raise response
        return response


class VerifyTargetTests(unittest.TestCase):
    def profile(self, *, instance_id="instance-1", contract_version="1.0"):
        return Profile(
            "test",
            "https://config.example.com",
            instance_id,
            contract_version,
        )

    def contract(
        self,
        *,
        instance_id="instance-1",
        version="1.0",
        api_version="1.0",
    ):
        return {
            "instanceId": instance_id,
            "contractVersion": version,
            "apiVersion": api_version,
        }

    def test_verifies_identity_before_authenticated_contract(self):
        client = StubClient(
            [
                {"instanceId": "instance-1", "contractVersion": "1.0"},
                self.contract(),
                {
                    "authenticated": True,
                    "actor": "token:test",
                    "scopes": ["inspect"],
                    "expires": None,
                },
            ]
        )
        target = verify_target(client, self.profile())
        self.assertEqual(target.live_instance_id, "instance-1")
        self.assertEqual(
            client.requests,
            [
                ("/api/public/identity", {"authenticated": False}),
                ("/api/contract", {}),
                ("/api/connect", {}),
            ],
        )

    def test_identity_contract_version_is_optional(self):
        client = StubClient(
            [
                {"instanceId": "instance-1"},
                self.contract(),
                {
                    "authenticated": True,
                    "actor": "token:test",
                    "scopes": ["inspect"],
                    "expires": None,
                },
            ]
        )
        self.assertEqual(
            verify_target(client, self.profile()).contract_version,
            "1.0",
        )

    def test_contract_1_through_1_2_falls_back_to_auth_me_on_connect_404(self):
        for version in ("1", "1.0", "1.1", "1.2"):
            with self.subTest(version=version):
                client = StubClient(
                    [
                        {
                            "instanceId": "instance-1",
                            "contractVersion": version,
                        },
                        self.contract(version=version),
                        CliError(
                            "No route for GET /api/connect.",
                            EXIT_VALIDATION,
                            http_status=404,
                            error_code="api.http_error",
                        ),
                        {
                            "actor": "token:legacy",
                            "scopes": ["inspect", "propose"],
                            "expires": "2030-01-01T00:00:00Z",
                        },
                    ]
                )

                target = verify_target(
                    client,
                    self.profile(contract_version=version),
                )

                self.assertEqual(
                    target.connection,
                    {
                        "authenticated": True,
                        "actor": "token:legacy",
                        "scopes": ["inspect", "propose"],
                        "expires": "2030-01-01T00:00:00Z",
                    },
                )
                self.assertEqual(
                    client.requests,
                    [
                        ("/api/public/identity", {"authenticated": False}),
                        ("/api/contract", {}),
                        ("/api/connect", {}),
                        ("/api/auth/me", {}),
                    ],
                )

    def test_connect_auth_failure_does_not_fall_back(self):
        failure = CliError(
            "Authentication failed.",
            EXIT_AUTHENTICATION,
            http_status=401,
            error_code="auth.invalid_credential",
        )
        client = StubClient(
            [
                {"instanceId": "instance-1", "contractVersion": "1.2"},
                self.contract(version="1.2"),
                failure,
            ]
        )

        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile(contract_version="1.2"))

        self.assertIs(raised.exception, failure)
        self.assertEqual(len(client.requests), 3)

    def test_non_404_connect_failure_does_not_fall_back(self):
        failure = CliError(
            "Configuration service is unavailable.",
            EXIT_CONNECTIVITY,
            http_status=503,
            error_code="api.http_error",
        )
        client = StubClient(
            [
                {"instanceId": "instance-1", "contractVersion": "1.2"},
                self.contract(version="1.2"),
                failure,
            ]
        )

        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile(contract_version="1.2"))

        self.assertIs(raised.exception, failure)
        self.assertEqual(len(client.requests), 3)

    def test_contract_1_3_connect_404_does_not_fall_back(self):
        failure = CliError(
            "No route for GET /api/connect.",
            EXIT_VALIDATION,
            http_status=404,
            error_code="api.http_error",
        )
        client = StubClient(
            [
                {"instanceId": "instance-1", "contractVersion": "1.3"},
                self.contract(version="1.3"),
                failure,
            ]
        )

        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile(contract_version="1.3"))

        self.assertIs(raised.exception, failure)
        self.assertEqual(len(client.requests), 3)

    def test_malformed_contract_version_stops_before_connection_endpoints(self):
        client = StubClient(
            [
                {"instanceId": "instance-1", "contractVersion": "1.0"},
                self.contract(version="1.bad"),
            ]
        )

        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile())

        self.assertEqual(raised.exception.error_code, "contract.invalid_version")
        self.assertEqual(
            client.requests,
            [
                ("/api/public/identity", {"authenticated": False}),
                ("/api/contract", {}),
            ],
        )

    def test_malformed_connect_response_does_not_fall_back(self):
        client = StubClient(
            [
                {"instanceId": "instance-1", "contractVersion": "1.2"},
                self.contract(version="1.2"),
                {"actor": "token:test", "scopes": ["inspect"]},
                {
                    "actor": "token:legacy",
                    "scopes": ["inspect"],
                    "expires": None,
                },
            ]
        )

        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile(contract_version="1.2"))

        self.assertEqual(raised.exception.error_code, "auth.invalid_response")
        self.assertEqual(len(client.requests), 3)

    def test_malformed_auth_me_fallback_response_is_rejected(self):
        client = StubClient(
            [
                {"instanceId": "instance-1", "contractVersion": "1.2"},
                self.contract(version="1.2"),
                CliError(
                    "No route for GET /api/connect.",
                    EXIT_VALIDATION,
                    http_status=404,
                    error_code="api.http_error",
                ),
                {"actor": "token:legacy", "scopes": "inspect"},
            ]
        )

        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile(contract_version="1.2"))

        self.assertEqual(raised.exception.error_code, "auth.invalid_response")
        self.assertEqual(len(client.requests), 4)

    def test_rejects_stored_contract_before_network(self):
        client = StubClient([])
        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile(contract_version="2.0"))
        self.assertEqual(raised.exception.exit_code, EXIT_CONFLICT)
        self.assertEqual(client.requests, [])

    def test_rejects_incomplete_public_identity(self):
        client = StubClient([{}])
        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile())
        self.assertEqual(raised.exception.error_code, "instance.invalid_identity")

    def test_rejects_live_instance_mismatch_before_authentication(self):
        client = StubClient(
            [{"instanceId": "other-instance", "contractVersion": "1.0"}]
        )
        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile())
        self.assertEqual(raised.exception.error_code, "instance.mismatch")
        self.assertEqual(len(client.requests), 1)

    def test_rejects_public_contract_mismatch_before_authentication(self):
        client = StubClient(
            [{"instanceId": "instance-1", "contractVersion": "2.0"}]
        )
        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile())
        self.assertEqual(raised.exception.error_code, "contract.incompatible")
        self.assertEqual(len(client.requests), 1)

    def test_rejects_authenticated_contract_instance_mismatch(self):
        client = StubClient(
            [
                {"instanceId": "instance-1", "contractVersion": "1.0"},
                self.contract(instance_id="other-instance"),
            ]
        )
        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile())
        self.assertEqual(
            raised.exception.error_code,
            "instance.contract_mismatch",
        )

    def test_rejects_authenticated_contract_version_mismatch(self):
        client = StubClient(
            [
                {"instanceId": "instance-1", "contractVersion": "1.0"},
                self.contract(version="2.0"),
            ]
        )
        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile())
        self.assertEqual(raised.exception.error_code, "contract.incompatible")

    def test_rejects_authenticated_api_version_mismatch(self):
        client = StubClient(
            [
                {"instanceId": "instance-1", "contractVersion": "1.0"},
                self.contract(api_version="2.0"),
            ]
        )
        with self.assertRaises(CliError) as raised:
            verify_target(client, self.profile())
        self.assertEqual(raised.exception.error_code, "api.incompatible")

    def test_contract_version_parser_rejects_invalid_values(self):
        for value in (
            None,
            "",
            "not-a-version",
            "1.",
            "1.garbage",
            "1.0.0.0",
            "01.0",
        ):
            with self.subTest(value=value), self.assertRaises(CliError):
                contract_major(value)

    def test_contract_version_parser_accepts_numeric_semver_like_values(self):
        for value in ("1", "1.0", "1.0.0", "1.2.3-rc.1", "1.2+build.4"):
            with self.subTest(value=value):
                self.assertEqual(contract_major(value), 1)


if __name__ == "__main__":
    unittest.main()
