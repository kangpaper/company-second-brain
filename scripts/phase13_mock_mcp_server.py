import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION_ID = "phase13-runtime-session"
RESOURCE_URI = "kb://policies/runtime-payment"
READ_LOCK = threading.Lock()
READ_COUNT = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        assert self.path == "/mcp"
        assert self.headers["Authorization"] in {
            "Bearer runtime-mcp-token",
            "Bearer runtime-server-owned-token",
        }
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size))
        method = body["method"]
        if method == "notifications/initialized":
            assert self.headers["Mcp-Session-Id"] == SESSION_ID
            self.send_response(202)
            self.end_headers()
            return
        if method != "initialize":
            assert self.headers["Mcp-Session-Id"] == SESSION_ID
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"resources": {}},
                "serverInfo": {"name": "phase13-mock-mcp", "version": "1.0"},
            }
        elif method == "resources/list":
            result = {
                "resources": [
                    {
                        "uri": RESOURCE_URI,
                        "name": "Runtime Payment Policy",
                        "mimeType": "text/markdown",
                    }
                ]
            }
        elif method == "resources/read":
            global READ_COUNT
            assert body["params"] == {"uri": RESOURCE_URI}
            with READ_LOCK:
                READ_COUNT += 1
                payment_days = 30 if READ_COUNT <= 3 else 45
            result = {
                "contents": [
                    {
                        "uri": RESOURCE_URI,
                        "name": "Runtime Payment Policy",
                        "mimeType": "text/markdown",
                        "text": (
                            "# Runtime Payment Policy\n\n"
                            f"Invoices are due in {payment_days} days."
                        ),
                    }
                ]
            }
        else:
            raise AssertionError(method)
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": body["id"], "result": result}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if method == "initialize":
            self.send_header("Mcp-Session-Id", SESSION_ID)
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    port = int(os.environ["PHASE13_MCP_PORT"])
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
