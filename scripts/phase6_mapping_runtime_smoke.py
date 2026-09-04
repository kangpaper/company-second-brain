import json

import httpx

from company_brain.domain.models import EntityType
from company_brain.integrations.odoo.client import OdooMCPClient
from company_brain.integrations.odoo.mapping import (
    MAPPING_FIELDS,
    extract_mcp_record,
    map_odoo_record,
)


def main() -> None:
    with httpx.Client(timeout=5) as http:
        client = OdooMCPClient(
            "http://127.0.0.1:8021/mcp",
            "runtime-mcp-key",
            http_client=http,
            requests_per_second=20,
        )
        result = client.call_tool(
            "get_record",
            {
                "model": "res.partner",
                "id": 7,
                "fields": MAPPING_FIELDS["res.partner"],
            },
        )
    record = extract_mcp_record(result, expected_id=7)
    mapped = map_odoo_record("res.partner", record)
    assert mapped.external_id == "7"
    assert mapped.entity_type is EntityType.CUSTOMER
    assert mapped.name == "Runtime Partner"
    assert "remote_secret" not in mapped.attributes
    print(
        json.dumps(
            {
                "session_initialized": True,
                "tool": "get_record",
                "record_id_verified": True,
                "entity_type": mapped.entity_type.value,
                "name": mapped.name,
            }
        )
    )


if __name__ == "__main__":
    main()