import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from company_brain.integrations.mcp.adapter import (
    MCPResourceContent,
    project_mcp_resource_descriptor,
    validate_mcp_resource_uri,
)

READ_ONLY_TOOLS = frozenset(
    {
        "list_models",
        "get_fields",
        "search_records",
        "get_record",
        "aggregate_records",
        "list_resource_templates",
        "get_current_context",
    }
)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class OdooMCPError(RuntimeError):
    """A sanitized connector or protocol failure."""


class OdooMCPPolicyError(OdooMCPError):
    """A locally denied operation that must never reach Odoo."""


class OdooMCPClient:
    def __init__(
        self,
        endpoint_url: str,
        api_key: str,
        *,
        http_client: httpx.Client,
        owns_http_client: bool = False,
        host_header: str | None = None,
        server_hostname: str | None = None,
        requests_per_second: float = 5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.http_client = http_client
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if host_header is not None:
            self.headers["Host"] = host_header
        self.server_hostname = server_hostname
        self.session_id: str | None = None
        self.next_id = 1
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.owns_http_client = owns_http_client
        self.minimum_interval = 1 / requests_per_second
        self.clock = clock
        self.sleep = sleep
        self.last_request_at: float | None = None

    def close(self) -> None:
        if self.owns_http_client and not self.http_client.is_closed:
            self.http_client.close()

    def post(self, payload: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        last_error: httpx.HTTPError | None = None
        for attempt in range(3):
            now = self.clock()
            if self.last_request_at is not None:
                remaining = self.minimum_interval - (now - self.last_request_at)
                if remaining > 0:
                    self.sleep(remaining)
            self.last_request_at = self.clock()
            try:
                request = self.http_client.build_request(
                    "POST", self.endpoint_url, headers=headers, json=payload
                )
                if self.server_hostname is not None:
                    request.extensions["sni_hostname"] = self.server_hostname.encode("ascii")
                streamed = self.http_client.send(request, stream=True)
                content = bytearray()
                try:
                    for chunk in streamed.iter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_RESPONSE_BYTES:
                            raise OdooMCPError("Odoo MCP response is too large")
                    response = httpx.Response(
                        streamed.status_code,
                        headers=streamed.headers,
                        content=bytes(content),
                        request=request,
                    )
                finally:
                    streamed.close()
            except httpx.HTTPError as error:
                last_error = error
                if attempt == 2:
                    raise OdooMCPError("Odoo MCP request failed") from error
                self.sleep(min(2**attempt, 5.0))
                continue
            if response.status_code not in {429, 502, 503, 504}:
                return response
            if attempt == 2:
                return response
            try:
                delay = float(response.headers.get("Retry-After", 2**attempt))
            except ValueError:
                delay = float(2**attempt)
            self.sleep(max(0.0, min(delay, 5.0)))
        raise OdooMCPError("Odoo MCP request failed") from last_error

    @staticmethod
    def decode_response(
        response: httpx.Response, expected_id: int | None = None
    ) -> dict[str, Any]:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        try:
            if content_type == "text/event-stream":
                body = None
                normalized = response.text.replace("\r\n", "\n").replace("\r", "\n")
                for event in normalized.split("\n\n"):
                    data_lines = [
                        line[5:].lstrip()
                        for line in event.splitlines()
                        if line.startswith("data:")
                    ]
                    if not data_lines:
                        continue
                    try:
                        candidate = json.loads("\n".join(data_lines))
                    except ValueError:
                        continue
                    if (
                        isinstance(candidate, dict)
                        and candidate.get("jsonrpc") == "2.0"
                        and candidate.get("id") == expected_id
                    ):
                        body = candidate
                        break
                if body is None:
                    raise ValueError("missing JSON-RPC SSE event")
            else:
                body = response.json()
        except ValueError as error:
            raise OdooMCPError("Odoo MCP returned an invalid response") from error
        if not isinstance(body, dict):
            raise OdooMCPError("Odoo MCP returned an invalid response")
        return body

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            response = self.post(payload, headers)
            response.raise_for_status()
            body = self.decode_response(response, request_id)
        except httpx.HTTPError as error:
            raise OdooMCPError("Odoo MCP request failed") from error
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            raise OdooMCPError("Odoo MCP returned an invalid response")
        if body.get("id") != request_id:
            raise OdooMCPError("Odoo MCP returned a mismatched response")
        if "error" in body:
            raise OdooMCPError("Odoo MCP tool returned an error")
        result = body.get("result")
        if not isinstance(result, dict):
            raise OdooMCPError("Odoo MCP returned an invalid result")
        return result

    def initialize(self) -> dict[str, Any]:
        if self.session_id is not None:
            return {}
        request_id = self.next_id
        self.next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "company-second-brain", "version": "0.1.0"},
            },
        }
        try:
            response = self.post(payload, self.headers)
            response.raise_for_status()
            body = self.decode_response(response, request_id)
        except httpx.HTTPError as error:
            raise OdooMCPError("Odoo MCP initialization failed") from error
        if (
            not isinstance(body, dict)
            or body.get("jsonrpc") != "2.0"
            or body.get("id") != request_id
            or not isinstance(body.get("result"), dict)
        ):
            raise OdooMCPError("Odoo MCP initialization returned an invalid response")
        self.session_id = response.headers.get("Mcp-Session-Id")
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        try:
            notified = self.post(notification, headers)
            notified.raise_for_status()
        except httpx.HTTPError as error:
            raise OdooMCPError("Odoo MCP initialization failed") from error
        result = body.get("result")
        if not isinstance(result, dict):
            raise OdooMCPError("Odoo MCP initialization returned an invalid response")
        return result

    def discover_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        tools = self.request("tools/list").get("tools")
        if not isinstance(tools, list):
            raise OdooMCPError("Odoo MCP returned an invalid tool list")
        return [
            item
            for item in tools
            if isinstance(item, dict) and item.get("name") in READ_ONLY_TOOLS
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in READ_ONLY_TOOLS:
            raise OdooMCPPolicyError(f"Odoo MCP tool {name!r} is not allowed")
        self.initialize()
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def list_resources(
        self, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        self.initialize()
        params = {"cursor": cursor} if cursor is not None else None
        result = self.request("resources/list", params)
        resources = result.get("resources")
        next_cursor = result.get("nextCursor")
        if (
            not isinstance(resources, list)
            or len(resources) > 200
            or (next_cursor is not None and not isinstance(next_cursor, str))
        ):
            raise OdooMCPError("MCP returned an invalid resource list")
        bounded: list[dict[str, Any]] = []
        for item in resources:
            try:
                descriptor = project_mcp_resource_descriptor(item)
            except ValueError as error:
                raise OdooMCPError("MCP returned an invalid resource list") from error
            bounded.append(descriptor)
        return bounded, next_cursor

    def read_resource(self, uri: str) -> MCPResourceContent:
        try:
            validate_mcp_resource_uri(uri)
        except ValueError as error:
            raise OdooMCPPolicyError("MCP resource URI is not allowed") from error
        self.initialize()
        result = self.request("resources/read", {"uri": uri})
        contents = result.get("contents")
        if not isinstance(contents, list) or len(contents) != 1:
            raise OdooMCPError("MCP returned invalid resource content")
        content = contents[0]
        if not isinstance(content, dict) or content.get("uri") != uri:
            raise OdooMCPError("MCP returned invalid resource content")
        text = content.get("text")
        mime_type = content.get("mimeType", "text/plain")
        name = content.get("name") or uri.rsplit("/", 1)[-1]
        if (
            not isinstance(text, str)
            or len(text.encode()) > MAX_RESPONSE_BYTES
            or not isinstance(mime_type, str)
            or mime_type not in {"text/plain", "text/markdown"}
            or not isinstance(name, str)
            or not name
            or len(name) > 500
        ):
            raise OdooMCPError("MCP returned invalid resource content")
        return MCPResourceContent(uri=uri, name=name, mime_type=mime_type, text=text)
