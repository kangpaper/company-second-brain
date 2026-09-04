import os

import httpx

from company_brain.api.generic_mcp_integrations import (
    get_allowed_mcp_hosts,
    get_mcp_connector_factory,
)
from company_brain.integrations.mcp.adapter import ReadOnlyMCPAdapter
from company_brain.integrations.mcp.client import MCPClient
from company_brain.main import app


def runtime_factory(endpoint: str, access_token: str) -> ReadOnlyMCPAdapter:
    if endpoint != "https://runtime.mcp.example/mcp":
        raise RuntimeError("unexpected runtime MCP endpoint")
    port = int(os.environ["PHASE13_MCP_PORT"])
    http = httpx.Client(
        timeout=httpx.Timeout(5, connect=2),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )
    return MCPClient(
        f"http://127.0.0.1:{port}/mcp",
        access_token,
        http_client=http,
        owns_http_client=True,
        requests_per_second=20,
    )


app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"runtime.mcp.example"}
app.dependency_overrides[get_mcp_connector_factory] = lambda: runtime_factory
