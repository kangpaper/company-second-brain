import json

import httpx
import pytest

from company_brain.integrations.odoo.client import (
    OdooMCPClient,
    OdooMCPError,
    OdooMCPPolicyError,
)


class MCPServer:
    def __init__(self, tools: list[dict[str, object]]) -> None:
        self.tools = tools
        self.requests: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        assert request.headers["accept"] == "application/json, text/event-stream"
        assert request.headers["authorization"] == "Bearer test-key"
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-123"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "odoo", "version": "19"},
                    },
                },
            )
        if body["method"] == "notifications/initialized":
            assert request.headers["mcp-session-id"] == "session-123"
            return httpx.Response(202)
        if body["method"] == "tools/list":
            assert request.headers["mcp-session-id"] == "session-123"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"tools": self.tools},
                },
            )
        if body["method"] == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": "ok"}]},
                },
            )
        raise AssertionError(body)


def tool(name: str) -> dict[str, object]:
    return {"name": name, "description": name, "inputSchema": {"type": "object"}}


def test_discovery_returns_only_hard_allowlisted_read_tools() -> None:
    server = MCPServer(
        [
            tool("search_records"),
            tool("get_record"),
            tool("aggregate_records"),
            tool("create_record"),
            tool("call_model_method"),
            tool("custom_read_looking_tool"),
        ]
    )
    transport = httpx.MockTransport(server)

    with httpx.Client(transport=transport) as http:
        client = OdooMCPClient("https://odoo.example.com/mcp", "test-key", http_client=http)
        discovered = client.discover_tools()

    assert [item["name"] for item in discovered] == [
        "search_records",
        "get_record",
        "aggregate_records",
    ]
    assert [request["method"] for request in server.requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


def test_write_and_unknown_tools_are_denied_before_network_call() -> None:
    server = MCPServer([])
    transport = httpx.MockTransport(server)

    with httpx.Client(transport=transport) as http:
        client = OdooMCPClient("https://odoo.example.com/mcp", "test-key", http_client=http)
        for name in (
            "create_record",
            "update_record",
            "delete_record",
            "post_message",
            "call_model_method",
            "custom_read_looking_tool",
        ):
            with pytest.raises(OdooMCPPolicyError, match="not allowed"):
                client.call_tool(name, {})

    assert server.requests == []


def test_transient_failures_retry_with_bounded_retry_after() -> None:
    attempts = 0
    now = 0.0
    delays: list[float] = []

    def clock() -> float:
        return now

    def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay

    def server(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        body = json.loads(request.content)
        if attempts < 3:
            return httpx.Response(503, headers={"Retry-After": "999"})
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"ok": True}},
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        client = OdooMCPClient(
            "https://odoo.example.com/mcp",
            "test-key",
            http_client=http,
            clock=clock,
            sleep=sleep,
        )
        result = client.request("tools/list")

    assert result == {"ok": True}
    assert attempts == 3
    assert delays == [5.0, 5.0]


def test_remote_errors_are_sanitized_and_not_retried() -> None:
    attempts = 0

    def server(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32000, "message": "secret database traceback"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        client = OdooMCPClient(
            "https://odoo.example.com/mcp", "test-key", http_client=http
        )
        with pytest.raises(OdooMCPError) as captured:
            client.request("tools/list")

    assert "secret" not in str(captured.value)
    assert attempts == 1


def test_local_rate_limit_delays_calls_before_network() -> None:
    now = 0.0
    delays: list[float] = []

    def clock() -> float:
        return now

    def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay

    def server(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        client = OdooMCPClient(
            "https://odoo.example.com/mcp",
            "test-key",
            http_client=http,
            requests_per_second=2,
            clock=clock,
            sleep=sleep,
        )
        client.request("tools/list")
        client.request("tools/list")
        client.request("tools/list")

    assert delays == [0.5, 0.5]


def test_owned_http_client_is_closed_but_injected_client_is_not() -> None:
    owned = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    injected = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))

    OdooMCPClient(
        "https://odoo.example.com/mcp", "test-key", http_client=owned, owns_http_client=True
    ).close()
    OdooMCPClient(
        "https://odoo.example.com/mcp", "test-key", http_client=injected
    ).close()

    assert owned.is_closed is True
    assert injected.is_closed is False
    injected.close()


def test_json_rpc_result_can_arrive_over_sse() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=(
                "event: message\n"
                f"data: {{\"jsonrpc\":\"2.0\",\"id\":{body['id']},"
                "\"result\":{\"tools\":[]}}\n\n"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        client = OdooMCPClient(
            "https://odoo.example.com/mcp", "test-key", http_client=http
        )
        assert client.request("tools/list") == {"tools": []}


def test_sse_keepalive_event_before_json_rpc_is_ignored() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=(
                "event: ping\ndata: keepalive\n\n"
                "event: message\n"
                f"data: {{\"jsonrpc\":\"2.0\",\"id\":{body['id']},"
                "\"result\":{\"tools\":[]}}\n\n"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        client = OdooMCPClient(
            "https://odoo.example.com/mcp", "test-key", http_client=http
        )
        assert client.request("tools/list") == {"tools": []}


def test_sse_json_rpc_notification_before_response_is_ignored() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=(
                "data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\n"
                f"data: {{\"jsonrpc\":\"2.0\",\"id\":{body['id']},"
                "\"result\":{\"tools\":[]}}\n\n"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        client = OdooMCPClient(
            "https://odoo.example.com/mcp", "test-key", http_client=http
        )
        assert client.request("tools/list") == {"tools": []}


def test_sse_stale_response_before_matching_response_is_ignored() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=(
                "data: {\"jsonrpc\":\"2.0\",\"id\":999,\"result\":{}}\n\n"
                f"data: {{\"jsonrpc\":\"2.0\",\"id\":{body['id']},"
                "\"result\":{\"tools\":[]}}\n\n"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        client = OdooMCPClient(
            "https://odoo.example.com/mcp", "test-key", http_client=http
        )
        assert client.request("tools/list") == {"tools": []}


def test_pinned_endpoint_uses_original_host_and_tls_sni() -> None:
    observed: dict[str, object] = {}

    def server(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["host"] = request.headers["host"]
        observed["sni"] = request.extensions.get("sni_hostname")
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        client = OdooMCPClient(
            "https://203.0.113.10/mcp",
            "test-key",
            http_client=http,
            host_header="odoo.example.com",
            server_hostname="odoo.example.com",
        )
        client.request("tools/list")

    assert observed == {
        "url": "https://203.0.113.10/mcp",
        "host": "odoo.example.com",
        "sni": b"odoo.example.com",
    }


def test_mismatched_json_rpc_id_is_rejected() -> None:
    def server(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 999, "result": {"tools": []}},
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        client = OdooMCPClient(
            "https://odoo.example.com/mcp", "test-key", http_client=http
        )
        with pytest.raises(OdooMCPError, match="mismatched response"):
            client.request("tools/list")


def test_oversized_remote_response_is_rejected() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"data": "x" * (2 * 1024 * 1024)},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        client = OdooMCPClient(
            "https://odoo.example.com/mcp", "test-key", http_client=http
        )
        with pytest.raises(OdooMCPError, match="too large"):
            client.request("tools/list")
