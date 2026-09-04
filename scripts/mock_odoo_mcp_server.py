import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION_ID = "phase5-runtime-session"
READ_TOOLS = [
    {"name": "search_records", "description": "Search", "inputSchema": {"type": "object"}},
    {"name": "get_record", "description": "Get", "inputSchema": {"type": "object"}},
    {"name": "aggregate_records", "description": "Aggregate", "inputSchema": {"type": "object"}},
]
WRITE_TOOL = {
    "name": "create_record",
    "description": "Must be filtered",
    "inputSchema": {"type": "object"},
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        assert self.path == "/mcp"
        assert self.headers["Authorization"] == "Bearer runtime-mcp-key"
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
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-odoo", "version": "19"},
            }
        elif method == "tools/list":
            result = {"tools": [*READ_TOOLS, WRITE_TOOL]}
        elif method == "tools/call":
            tool_name = body["params"]["name"]
            arguments = body["params"]["arguments"]
            if tool_name == "search_records":
                assert arguments["limit"] == 2
                value: object = [{"id": 7, "name": "Runtime Partner"}]
            elif tool_name == "get_record":
                assert arguments["model"] == "res.partner"
                assert arguments["id"] == 7
                value = {
                    "id": 7,
                    "name": "Runtime Partner",
                    "is_company": True,
                    "customer_rank": 1,
                    "supplier_rank": 0,
                    "active": True,
                }
            else:
                raise AssertionError(tool_name)
            result = {
                "content": [{"type": "text", "text": json.dumps(value)}]
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
    ThreadingHTTPServer(("127.0.0.1", 8021), Handler).serve_forever()
