from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mapp_config_cli.client import (
    ApiClient,
    contract_major,
    normalize_endpoint,
    verify_target,
)
from mapp_config_cli.config import Profile
from mapp_config_cli.errors import (
    CliError,
    EXIT_CONFLICT,
    EXIT_CONNECTIVITY,
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
        return self.responses.pop(0)


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
            ]
        )
        target = verify_target(client, self.profile())
        self.assertEqual(target.live_instance_id, "instance-1")
        self.assertEqual(
            client.requests,
            [
                ("/api/public/identity", {"authenticated": False}),
                ("/api/contract", {}),
            ],
        )

    def test_identity_contract_version_is_optional(self):
        client = StubClient(
            [
                {"instanceId": "instance-1"},
                self.contract(),
            ]
        )
        self.assertEqual(
            verify_target(client, self.profile()).contract_version,
            "1.0",
        )

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
