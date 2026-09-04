import hashlib
import json
import os
import sys
import uuid

import httpx
import psycopg

DATABASE_URL = os.getenv(
    "PHASE3_DATABASE_URL",
    "postgresql://brain:brain@localhost:5432/company_brain",
)
API_URL = os.getenv("PHASE3_API_URL", "http://127.0.0.1:8010")


def main() -> None:
    connection = psycopg.connect(DATABASE_URL)
    organization_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    token = f"phase3-{uuid.uuid4()}"
    slug = f"phase3-runtime-{organization_id.hex[:8]}"
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO organizations(id,name,slug,created_at,updated_at) "
                "VALUES(%s,'Phase3 Runtime',%s,now(),now())",
                (organization_id, slug),
            )
            cursor.execute(
                "INSERT INTO workspaces("
                "id,organization_id,name,slug,settings,created_at,updated_at) "
                "VALUES(%s,%s,'Graph','graph','{}',now(),now())",
                (workspace_id, organization_id),
            )
            cursor.execute(
                "INSERT INTO users("
                "id,organization_id,email,display_name,api_token_hash,created_at,updated_at) "
                "VALUES(%s,%s,%s,'Runtime',%s,now(),now())",
                (
                    user_id,
                    organization_id,
                    f"{slug}@example.com",
                    hashlib.sha256(token.encode()).hexdigest(),
                ),
            )
            cursor.execute(
                "INSERT INTO memberships("
                "id,organization_id,workspace_id,user_id,role,created_at,updated_at) "
                "VALUES(%s,%s,%s,%s,'editor',now(),now())",
                (uuid.uuid4(), organization_id, workspace_id, user_id),
            )
        connection.commit()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Organization-ID": str(organization_id),
            "X-Workspace-ID": str(workspace_id),
        }
        with httpx.Client(base_url=API_URL, headers=headers, timeout=10) as client:
            client.get("/health").raise_for_status()
            customer = client.post(
                "/api/v1/entities",
                json={"entity_type": "customer", "name": "Runtime Customer"},
            )
            customer.raise_for_status()
            invoice = client.post(
                "/api/v1/entities",
                json={"entity_type": "invoice", "name": "Runtime Invoice"},
            )
            invoice.raise_for_status()
            relationship = client.post(
                "/api/v1/relationships",
                json={
                    "from_entity_id": customer.json()["id"],
                    "to_entity_id": invoice.json()["id"],
                    "relationship_type": "CUSTOMER_HAS_INVOICE",
                },
            )
            relationship.raise_for_status()
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO events("
                    "id,organization_id,workspace_id,subject_entity_id,event_type,"
                    "occurred_at,payload,created_at,updated_at) "
                    "VALUES(%s,%s,%s,%s,'invoice_created',now(),%s,now(),now())",
                    (
                        uuid.uuid4(),
                        organization_id,
                        workspace_id,
                        uuid.UUID(invoice.json()["id"]),
                        json.dumps({"amount": 100}),
                    ),
                )
            connection.commit()
            graph = client.get(
                "/api/v1/graph",
                params={"root_entity_id": customer.json()["id"], "depth": 1},
            )
            graph.raise_for_status()
            timeline = client.get(
                "/api/v1/timeline",
                params={"root_entity_id": customer.json()["id"], "depth": 1},
            )
            timeline.raise_for_status()
            canvas_content = {
                "nodes": [
                    {
                        "id": "customer",
                        "type": "text",
                        "x": 0,
                        "y": 0,
                        "width": 300,
                        "height": 180,
                        "text": "# Runtime Customer",
                    }
                ],
                "edges": [],
            }
            imported = client.post(
                "/api/v1/canvases/import",
                json={
                    "path": "runtime.canvas",
                    "title": "Runtime",
                    "canvas": canvas_content,
                },
            )
            imported.raise_for_status()
            exported = client.get(
                f"/api/v1/canvases/{imported.json()['id']}/export"
            )
            exported.raise_for_status()
            assert exported.json() == canvas_content
            print(
                json.dumps(
                    {
                        "graph_node_names": [node["name"] for node in graph.json()["nodes"]],
                        "timeline_event_types": [
                            item["event_type"] for item in timeline.json()
                        ],
                        "canvas_node_ids": [
                            node["id"] for node in exported.json()["nodes"]
                        ],
                    }
                )
            )
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            for table in (
                "canvases",
                "events",
                "relationships",
                "entities",
                "memberships",
            ):
                cursor.execute(
                    f"DELETE FROM {table} WHERE organization_id=%s",  # noqa: S608
                    (organization_id,),
                )
            cursor.execute("DELETE FROM users WHERE organization_id=%s", (organization_id,))
            cursor.execute(
                "DELETE FROM workspaces WHERE organization_id=%s", (organization_id,)
            )
            cursor.execute("DELETE FROM organizations WHERE id=%s", (organization_id,))
        connection.commit()
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"phase3 runtime smoke failed: {error}", file=sys.stderr)
        raise
