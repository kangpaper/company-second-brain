from typing import Any

import httpx
import pytest

from company_brain.integrations.mcp.adapter import ReadOnlyMCPAdapter
from company_brain.integrations.mcp.client import MCPClient
from company_brain.integrations.odoo.client import OdooMCPError, OdooMCPPolicyError


def client_with_results(results: dict[str, dict[str, Any]]) -> tuple[MCPClient, list[str]]:
    client = MCPClient(
        "https://mcp.example.com/mcp",
        "request-scoped-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )
    methods: list[str] = []
    client.initialize = lambda: {}  # type: ignore[method-assign]

    def request(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        del params
        methods.append(method)
        return results[method]

    client.request = request  # type: ignore[method-assign]
    return client, methods


def test_common_adapter_lists_and_reads_standard_mcp_resources() -> None:
    client, methods = client_with_results(
        {
            "resources/list": {
                "resources": [
                    {
                        "uri": "kb://policy/payment",
                        "name": "Payment Policy",
                        "mimeType": "text/markdown",
                    }
                ],
                "nextCursor": "next-page",
            },
            "resources/read": {
                "contents": [
                    {
                        "uri": "kb://policy/payment",
                        "name": "Payment Policy",
                        "mimeType": "text/markdown",
                        "text": "# Payment Policy",
                    }
                ]
            },
        }
    )

    assert isinstance(client, ReadOnlyMCPAdapter)
    resources, cursor = client.list_resources()
    content = client.read_resource("kb://policy/payment")

    assert resources[0]["name"] == "Payment Policy"
    assert cursor == "next-page"
    assert content.uri == "kb://policy/payment"
    assert content.text == "# Payment Policy"
    assert methods == ["resources/list", "resources/read"]


@pytest.mark.parametrize(
    "resource_uri",
    [
        "relative/resource",
        "https://user:password@resource.example/item",
        "kb://policy/item?access_token=leak",
        "kb://policy/item#",
        "kb://policy/item#access_token=leak",
        "kb://policy/item#API.KEY=leak",
        "kb://policy/item#client-secret=leak",
        "kb://policy/item with spaces",
        "kb://policy/item\x7f",
        "kb://policy/item\u0080",
        "kb://policy/item\u009f",
        "kb://policy/item\u200b",
    ],
)
def test_resource_list_rejects_sensitive_or_malformed_uris(resource_uri: str) -> None:
    client, _ = client_with_results(
        {"resources/list": {"resources": [{"uri": resource_uri, "name": "Policy"}]}}
    )

    with pytest.raises(OdooMCPError, match="invalid resource list"):
        client.list_resources()


def test_resource_list_projects_only_bounded_public_descriptor_fields() -> None:
    client, _ = client_with_results(
        {
            "resources/list": {
                "resources": [
                    {
                        "uri": "kb://policy/payment",
                        "name": "Payment Policy",
                        "description": "Public policy",
                        "mimeType": "text/markdown",
                        "size": 42,
                        "access_token": "must-not-leak",
                        "annotations": {"secret": "must-not-leak"},
                    }
                ]
            }
        }
    )

    resources, _ = client.list_resources()

    assert resources == [
        {
            "uri": "kb://policy/payment",
            "name": "Payment Policy",
            "description": "Public policy",
            "mimeType": "text/markdown",
            "size": 42,
        }
    ]


@pytest.mark.parametrize(
    "result",
    [
        {"contents": [{"uri": "kb://other", "text": "wrong identity"}]},
        {"contents": [{"uri": "kb://policy/payment", "blob": "AA=="}]},
        {
            "contents": [
                {
                    "uri": "kb://policy/payment",
                    "mimeType": "application/octet-stream",
                    "text": "not allowed",
                }
            ]
        },
    ],
)
def test_resource_read_fails_closed_on_unmappable_payload(result: dict[str, Any]) -> None:
    client, _ = client_with_results({"resources/read": result})

    with pytest.raises(OdooMCPError, match="invalid resource content"):
        client.read_resource("kb://policy/payment")


def test_write_tool_is_denied_before_json_rpc_request() -> None:
    client, methods = client_with_results({})

    with pytest.raises(OdooMCPPolicyError, match="not allowed"):
        client.call_tool("delete_everything", {})

    assert methods == []
