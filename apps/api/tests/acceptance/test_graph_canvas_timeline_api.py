from collections.abc import AsyncIterator
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import httpx
import pytest
from sqlalchemy.orm import Session

from company_brain.db.session import get_session
from company_brain.domain.models import Event, Membership, Organization, User, Workspace
from company_brain.main import app


@pytest.fixture
async def client(session: Session) -> AsyncIterator[httpx.AsyncClient]:
    def override_session() -> AsyncIterator[Session]:
        yield session

    app.dependency_overrides[get_session] = override_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api_client:
        yield api_client
    app.dependency_overrides.clear()


def graph_scope(session: Session, slug: str) -> dict[str, str]:
    organization = Organization(name=slug, slug=slug)
    session.add(organization)
    session.flush()
    workspace = Workspace(organization_id=organization.id, name="Main", slug="main")
    user = User(
        organization_id=organization.id,
        email=f"{slug}@example.com",
        display_name=slug,
        api_token_hash=sha256(f"{slug}-token".encode()).hexdigest(),
    )
    session.add_all([workspace, user])
    session.flush()
    session.add(
        Membership(
            organization_id=organization.id,
            workspace_id=workspace.id,
            user_id=user.id,
            role="owner",
        )
    )
    session.commit()
    return {
        "Authorization": f"Bearer {slug}-token",
        "X-Organization-ID": str(organization.id),
        "X-Workspace-ID": str(workspace.id),
    }


async def create_entity(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    entity_type: str,
    name: str,
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/entities",
        headers=headers,
        json={"entity_type": entity_type, "name": name},
    )
    assert response.status_code == 201
    return response.json()


async def link_entities(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    source_id: str,
    target_id: str,
    relationship_type: str,
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/relationships",
        headers=headers,
        json={
            "from_entity_id": source_id,
            "to_entity_id": target_id,
            "relationship_type": relationship_type,
        },
    )
    assert response.status_code == 201
    return response.json()


async def test_graph_returns_rooted_customer_nodes_edges_and_filters(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = graph_scope(session, "graph-main")
    customer = await create_entity(client, headers, "customer", "ABC")
    invoice = await create_entity(client, headers, "invoice", "INV-1")
    ticket = await create_entity(client, headers, "ticket", "TCK-1")
    invoice_edge = await link_entities(
        client,
        headers,
        str(customer["id"]),
        str(invoice["id"]),
        "CUSTOMER_HAS_INVOICE",
    )
    await link_entities(
        client,
        headers,
        str(customer["id"]),
        str(ticket["id"]),
        "CUSTOMER_HAS_TICKET",
    )

    response = await client.get(
        "/api/v1/graph",
        headers=headers,
        params={
            "root_entity_id": customer["id"],
            "depth": 1,
            "relationship_type": "CUSTOMER_HAS_INVOICE",
        },
    )

    assert response.status_code == 200
    graph = response.json()
    assert {node["id"] for node in graph["nodes"]} == {customer["id"], invoice["id"]}
    assert [edge["id"] for edge in graph["edges"]] == [invoice_edge["id"]]
    assert graph["root_entity_id"] == customer["id"]


async def test_graph_is_tenant_safe_and_rejects_invalid_depth(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers_a = graph_scope(session, "graph-a")
    headers_b = graph_scope(session, "graph-b")
    customer_b = await create_entity(client, headers_b, "customer", "Secret")

    hidden = await client.get(
        "/api/v1/graph",
        headers=headers_a,
        params={"root_entity_id": customer_b["id"], "depth": 1},
    )
    invalid = await client.get(
        "/api/v1/graph",
        headers=headers_a,
        params={"depth": 4},
    )

    assert hidden.status_code == 404
    assert invalid.status_code == 422


async def test_graph_enforces_hard_node_edge_limits(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = graph_scope(session, "graph-limits")
    root = await create_entity(client, headers, "customer", "Root")
    for index in range(4):
        child = await create_entity(client, headers, "invoice", f"INV-{index}")
        await link_entities(
            client,
            headers,
            str(root["id"]),
            str(child["id"]),
            "CUSTOMER_HAS_INVOICE",
        )

    response = await client.get(
        "/api/v1/graph",
        headers=headers,
        params={
            "root_entity_id": root["id"],
            "depth": 1,
            "node_limit": 3,
            "edge_limit": 2,
        },
    )
    invalid = await client.get(
        "/api/v1/graph", headers=headers, params={"node_limit": 501}
    )

    assert response.status_code == 200
    assert len(response.json()["nodes"]) <= 3
    assert len(response.json()["edges"]) <= 2
    assert response.json()["truncated"] is True
    assert invalid.status_code == 422


async def test_timeline_merges_related_entity_events_newest_first_and_filters(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = graph_scope(session, "timeline-main")
    customer = await create_entity(client, headers, "customer", "ABC")
    invoice = await create_entity(client, headers, "invoice", "INV-1")
    await link_entities(
        client,
        headers,
        str(customer["id"]),
        str(invoice["id"]),
        "CUSTOMER_HAS_INVOICE",
    )
    organization_id = UUID(headers["X-Organization-ID"])
    workspace_id = UUID(headers["X-Workspace-ID"])
    session.add_all(
        [
            Event(
                organization_id=organization_id,
                workspace_id=workspace_id,
                subject_entity_id=UUID(str(customer["id"])),
                event_type="meeting",
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                payload={"summary": "Kickoff"},
            ),
            Event(
                organization_id=organization_id,
                workspace_id=workspace_id,
                subject_entity_id=UUID(str(invoice["id"])),
                event_type="invoice_overdue",
                occurred_at=datetime(2026, 2, 1, tzinfo=UTC),
                payload={"days": 10},
            ),
        ]
    )
    session.commit()

    merged = await client.get(
        "/api/v1/timeline",
        headers=headers,
        params={"root_entity_id": customer["id"], "depth": 1},
    )
    filtered = await client.get(
        "/api/v1/timeline",
        headers=headers,
        params={
            "root_entity_id": customer["id"],
            "depth": 1,
            "event_type": "meeting",
            "from_at": "2025-12-31T00:00:00Z",
            "to_at": "2026-01-02T00:00:00Z",
        },
    )

    assert merged.status_code == 200
    assert [item["event_type"] for item in merged.json()] == [
        "invoice_overdue",
        "meeting",
    ]
    assert [item["event_type"] for item in filtered.json()] == ["meeting"]


async def test_timeline_hides_cross_tenant_root(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers_a = graph_scope(session, "timeline-a")
    headers_b = graph_scope(session, "timeline-b")
    hidden_customer = await create_entity(client, headers_b, "customer", "Hidden")

    response = await client.get(
        "/api/v1/timeline",
        headers=headers_a,
        params={"root_entity_id": hidden_customer["id"]},
    )

    assert response.status_code == 404


async def test_timeline_requires_timezone_and_enforces_page_limit(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = graph_scope(session, "timeline-bounds")
    mixed_timezone = await client.get(
        "/api/v1/timeline",
        headers=headers,
        params={
            "from_at": "2026-01-01T00:00:00",
            "to_at": "2026-01-02T00:00:00Z",
        },
    )
    excessive_limit = await client.get(
        "/api/v1/timeline", headers=headers, params={"limit": 201}
    )

    assert mixed_timezone.status_code == 422
    assert excessive_limit.status_code == 422


async def test_json_canvas_import_export_round_trips_supported_fields(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = graph_scope(session, "canvas-main")
    canvas = {
        "nodes": [
            {
                "id": "customer",
                "type": "text",
                "x": 0,
                "y": 0,
                "width": 300,
                "height": 180,
                "color": "4",
                "text": "# Customer ABC",
            },
            {
                "id": "policy",
                "type": "file",
                "x": 400,
                "y": 0,
                "width": 300,
                "height": 180,
                "file": "policy.md",
                "subpath": "#Payment terms",
            },
            {
                "id": "website",
                "type": "link",
                "x": 0,
                "y": 240,
                "width": 300,
                "height": 180,
                "url": "https://example.com",
            },
            {
                "id": "group",
                "type": "group",
                "x": -40,
                "y": -40,
                "width": 800,
                "height": 500,
                "label": "Customer 360",
                "background": "cover.png",
                "backgroundStyle": "cover",
            },
        ],
        "edges": [
            {
                "id": "customer-policy",
                "fromNode": "customer",
                "fromSide": "right",
                "fromEnd": "none",
                "toNode": "policy",
                "toSide": "left",
                "toEnd": "arrow",
                "color": "5",
                "label": "uses",
            }
        ],
    }

    imported = await client.post(
        "/api/v1/canvases/import",
        headers=headers,
        json={"path": "customer-abc.canvas", "title": "Customer ABC", "canvas": canvas},
    )
    exported = await client.get(
        f"/api/v1/canvases/{imported.json()['id']}/export", headers=headers
    )

    assert imported.status_code == 201
    assert exported.status_code == 200
    assert exported.json() == canvas


async def test_canvas_rejects_duplicate_ids_and_dangling_edges(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = graph_scope(session, "canvas-invalid")
    node = {"id": "same", "type": "text", "x": 0, "y": 0, "width": 10, "height": 10, "text": "x"}
    duplicate = await client.post(
        "/api/v1/canvases/import",
        headers=headers,
        json={"path": "duplicate.canvas", "title": "Duplicate", "canvas": {"nodes": [node, node]}},
    )
    dangling = await client.post(
        "/api/v1/canvases/import",
        headers=headers,
        json={
            "path": "dangling.canvas",
            "title": "Dangling",
            "canvas": {
                "nodes": [node],
                "edges": [{"id": "bad", "fromNode": "same", "toNode": "missing"}],
            },
        },
    )

    assert duplicate.status_code == 422
    assert dangling.status_code == 422


async def test_canvas_rejects_invalid_color(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = graph_scope(session, "canvas-color")
    invalid = await client.post(
        "/api/v1/canvases/import",
        headers=headers,
        json={
            "path": "color.canvas",
            "title": "Color",
            "canvas": {
                "nodes": [
                    {
                        "id": "node",
                        "type": "text",
                        "x": 0,
                        "y": 0,
                        "width": 10,
                        "height": 10,
                        "text": "x",
                        "color": "not-a-canvas-color",
                    }
                ]
            },
        },
    )
    invalid_node_hex = await client.post(
        "/api/v1/canvases/import",
        headers=headers,
        json={
            "path": "node-hex.canvas",
            "title": "Node Hex",
            "canvas": {
                "nodes": [
                    {
                        "id": "node",
                        "type": "text",
                        "x": 0,
                        "y": 0,
                        "width": 10,
                        "height": 10,
                        "text": "x",
                        "color": "#12345",
                    }
                ]
            },
        },
    )
    invalid_edge_hex = await client.post(
        "/api/v1/canvases/import",
        headers=headers,
        json={
            "path": "edge-hex.canvas",
            "title": "Edge Hex",
            "canvas": {
                "nodes": [
                    {
                        "id": "from",
                        "type": "text",
                        "x": 0,
                        "y": 0,
                        "width": 10,
                        "height": 10,
                        "text": "from",
                    },
                    {
                        "id": "to",
                        "type": "text",
                        "x": 20,
                        "y": 0,
                        "width": 10,
                        "height": 10,
                        "text": "to",
                    },
                ],
                "edges": [
                    {
                        "id": "edge",
                        "fromNode": "from",
                        "toNode": "to",
                        "color": "#12345",
                    }
                ],
            },
        },
    )

    assert invalid.status_code == 422
    assert invalid_node_hex.status_code == 422
    assert invalid_edge_hex.status_code == 422


async def test_canvas_export_is_tenant_safe(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers_a = graph_scope(session, "canvas-a")
    headers_b = graph_scope(session, "canvas-b")
    imported = await client.post(
        "/api/v1/canvases/import",
        headers=headers_b,
        json={"path": "secret.canvas", "title": "Secret", "canvas": {"nodes": [], "edges": []}},
    )

    hidden = await client.get(
        f"/api/v1/canvases/{imported.json()['id']}/export", headers=headers_a
    )

    assert hidden.status_code == 404
