import json

import httpx

from company_brain.integrations.odoo.client import OdooMCPClient


def main() -> None:
    with httpx.Client(timeout=5) as http:
        client = OdooMCPClient(
            "http://127.0.0.1:8021/mcp",
            "runtime-mcp-key",
            http_client=http,
            requests_per_second=20,
        )
        tools = client.discover_tools()
        result = client.call_tool(
            "search_records",
            {
                "model": "res.partner",
                "domain": [["customer_rank", ">", 0]],
                "fields": ["id", "name"],
                "limit": 2,
                "offset": 0,
            },
        )
    names = [tool["name"] for tool in tools]
    assert names == ["search_records", "get_record", "aggregate_records"]
    assert "create_record" not in names
    print(
        json.dumps(
            {
                "session_initialized": True,
                "discovered_tools": names,
                "write_tool_filtered": True,
                "search_result": result["content"][0]["text"],
            }
        )
    )


if __name__ == "__main__":
    main()
