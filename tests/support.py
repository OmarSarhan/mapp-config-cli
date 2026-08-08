from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit


Response = tuple[int, Any] | tuple[int, Any, dict[str, str]]
Responder = Callable[[dict[str, Any]], Response]


class JsonServer:
    def __init__(self, routes: dict[tuple[str, str], Response | Responder]):
        self.routes = routes
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _handle(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    body = raw.decode("utf-8", "replace")
                path = urlsplit(self.path).path
                request = {
                    "method": self.command,
                    "path": path,
                    "query": urlsplit(self.path).query,
                    "headers": dict(self.headers),
                    "body": body,
                }
                owner.requests.append(request)
                response = owner.routes.get((self.command, path))
                if response is None:
                    response = (404, {"error": f"No route for {self.command} {path}"})
                if callable(response):
                    response = response(request)
                status, payload, *extra = response
                headers = extra[0] if extra else {}
                if isinstance(payload, bytes):
                    encoded = payload
                else:
                    encoded = json.dumps(payload).encode("utf-8")
                    headers = {"Content-Type": "application/json", **headers}
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = _handle
            do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def standard_routes(
    *,
    instance_id: str = "instance-1",
    revision: str = "rev-1",
    workspace: dict[str, Any] | None = None,
) -> dict[tuple[str, str], Response | Responder]:
    workspace = workspace or {
        "key": "demo",
        "locale": {"layers": {"Bus Stops": {"format": "mvt"}}},
    }
    return {
        ("GET", "/api/public/identity"): (
            200,
            {
                "instanceId": instance_id,
                "contractVersion": "1.0",
                "xyzVersion": "v4.23.4",
            },
        ),
        ("GET", "/api/contract"): (
            200,
            {
                "apiVersion": "1.0",
                "contractVersion": "1.0",
                "rulesVersion": "1.0",
                "xyzVersion": "v4.23.4",
                "instanceId": instance_id,
                "authentication": {"scopes": ["full"]},
                "commands": [
                    "describe",
                    "schema",
                    "rules",
                    "examples",
                    "capabilities list",
                    "capabilities show",
                    "dependencies list",
                    "dependencies check",
                    "plugins list",
                    "plugins show",
                    "plugins validate",
                    "plugins usage",
                    "workspace get",
                    "layers list",
                    "layers get",
                    "layers statistics",
                    "layers effective",
                    "catalog list",
                    "icons list",
                    "derived-layers capabilities",
                    "derived-layers list",
                    "derived-layers show",
                    "derived-layers map-extent",
                    "derived-layers plan-area-weighted-h3",
                    "derived-layers create",
                    "derived-layers refresh",
                    "derived-layers replace",
                    "derived-layers drop",
                    "validate",
                    "set",
                    "unset",
                    "amend",
                    "sql capabilities",
                    "sql test",
                    "visual-plan",
                    "visual-test",
                    "screenshot",
                    "proposals create",
                    "proposals check",
                    "proposals show",
                    "proposals list",
                    "proposals apply",
                    "proposals decline",
                    "proposals preview-plan",
                    "proposals preview-test",
                    "proposals preview-screenshot",
                    "xyz status",
                    "xyz reload",
                    "auth status",
                    "auth device",
                    "operations show",
                    "operations wait",
                    "operations cancel",
                ],
            },
        ),
        ("GET", "/api/workspace"): (
            200,
            {"workspace": workspace, "revision": revision},
        ),
        ("GET", "/api/layers"): (
            200,
            {
                "revision": revision,
                "locale": "locale",
                "layers": (
                    workspace.get("locale", {}).get("layers", {})
                    if isinstance(workspace.get("locale"), dict)
                    else {}
                ),
            },
        ),
        ("GET", "/api/auth/me"): (
            200,
            {"actor": "token:abc", "scopes": ["full"]},
        ),
        ("GET", "/api/connect"): (
            200,
            {
                "authenticated": True,
                "actor": "token:abc",
                "tokenId": "abc",
                "scopes": ["full"],
                "expires": None,
            },
        ),
    }
