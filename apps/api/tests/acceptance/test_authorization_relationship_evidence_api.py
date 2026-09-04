from collections.abc import AsyncIterator
from hashlib import sha256
from uuid import UUID

import httpx
import pytest
from sqlalchemy.orm import Session

from company_brain.db.session import get_session
from company_brain.domain.models import (
    Entity,
    Evidence,
    EvidenceLink,
    Membership,
    Organization,
    Source,
    User,
    Workspace,
)
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


def authorized_scope(session: Session) -> tuple[dict[str, str], Organization, Workspace]:
    organization = Organization(name="Authorized", slug="authorized")
    session.add(organization)
    session.flush()
    workspace = Workspace(organization_id=organization.id, name="Main", slug="main")
    user = User(
        organization_id=organization.id,
        email="owner@example.com",
        display_name="Owner",
        api_token_hash=sha256(b"test-token").hexdigest(),
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
    return (
        {
            "Authorization": "Bearer test-token",
            "X-Organization-ID": str(organization.id),
            "X-Workspace-ID": str(workspace.id),
        },
        organization,
        workspace,
    )


def member_scope(session: Session) -> dict[str, str]:
    organization = Organization(name="Readers", slug="readers")
    session.add(organization)
    session.flush()
    workspace = Workspace(organization_id=organization.id, name="Read", slug="read")
    user = User(
        organization_id=organization.id,
        email="reader@example.com",
        display_name="Reader",
        api_token_hash=sha256(b"reader-token").hexdigest(),
    )
    session.add_all([workspace, user])
    session.flush()
    session.add(
        Membership(
            organization_id=organization.id,
            workspace_id=workspace.id,
            user_id=user.id,
            role="member",
        )
    )
    session.commit()
    return {
        "Authorization": "Bearer reader-token",
        "X-Organization-ID": str(organization.id),
        "X-Workspace-ID": str(workspace.id),
    }


async def test_entity_api_rejects_missing_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/entities")
    assert response.status_code == 401


async def test_authenticated_user_cannot_spoof_another_tenant_scope(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers_a, _, _ = authorized_scope(session)
    organization_b = Organization(name="Other", slug="other")
    session.add(organization_b)
    session.flush()
    workspace_b = Workspace(organization_id=organization_b.id, name="Other", slug="other")
    session.add(workspace_b)
    session.commit()
    spoofed_headers = {
        **headers_a,
        "X-Organization-ID": str(organization_b.id),
        "X-Workspace-ID": str(workspace_b.id),
    }

    response = await client.get("/api/v1/entities", headers=spoofed_headers)

    assert response.status_code == 403


async def test_read_only_member_cannot_create_entity(
    client: httpx.AsyncClient, session: Session
) -> None:
    response = await client.post(
        "/api/v1/entities",
        headers=member_scope(session),
        json={"entity_type": "customer", "name": "Forbidden"},
    )
    assert response.status_code == 403


async def test_relationship_and_evidence_are_attached_to_entities(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, _, _ = authorized_scope(session)
    customer = await client.post(
        "/api/v1/entities", headers=headers, json={"entity_type": "customer", "name": "ABC"}
    )
    invoice = await client.post(
        "/api/v1/entities", headers=headers, json={"entity_type": "invoice", "name": "INV-1"}
    )
    relationship = await client.post(
        "/api/v1/relationships",
        headers=headers,
        json={
            "from_entity_id": customer.json()["id"],
            "to_entity_id": invoice.json()["id"],
            "relationship_type": "CUSTOMER_HAS_INVOICE",
        },
    )
    evidence = await client.post(
        f"/api/v1/entities/{customer.json()['id']}/evidence",
        headers=headers,
        json={
            "source_type": "manual",
            "uri": "manual://test",
            "evidence_type": "business_fact",
            "pointer": {"field": "name"},
            "quote": "ABC",
        },
    )

    assert relationship.status_code == 201
    assert evidence.status_code == 201
    assert (
        await client.get(f"/api/v1/entities/{customer.json()['id']}/relationships", headers=headers)
    ).json()[0]["id"] == relationship.json()["id"]
    assert (
        await client.get(f"/api/v1/entities/{customer.json()['id']}/evidence", headers=headers)
    ).json()[0]["id"] == evidence.json()["id"]

    patched = await client.patch(
        f"/api/v1/relationships/{relationship.json()['id']}",
        headers=headers,
        json={"confidence": 0.8},
    )
    assert patched.status_code == 200
    assert patched.json()["confidence"] == 0.8
    assert (
        await client.delete(f"/api/v1/relationships/{relationship.json()['id']}", headers=headers)
    ).status_code == 204


async def test_relationship_integrity_conflicts_return_409(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, _, _ = authorized_scope(session)
    first = await client.post(
        "/api/v1/entities", headers=headers, json={"entity_type": "customer", "name": "A"}
    )
    second = await client.post(
        "/api/v1/entities", headers=headers, json={"entity_type": "customer", "name": "B"}
    )
    payload = {
        "from_entity_id": first.json()["id"],
        "to_entity_id": second.json()["id"],
        "relationship_type": "RELATED_TO",
    }
    assert (
        await client.post("/api/v1/relationships", headers=headers, json=payload)
    ).status_code == 201

    duplicate = await client.post("/api/v1/relationships", headers=headers, json=payload)
    self_loop = await client.post(
        "/api/v1/relationships",
        headers=headers,
        json={**payload, "to_entity_id": first.json()["id"]},
    )

    assert duplicate.status_code == 409
    assert self_loop.status_code == 409


async def test_relationship_patch_rejects_explicit_null(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, _, _ = authorized_scope(session)
    first = await client.post(
        "/api/v1/entities", headers=headers, json={"entity_type": "customer", "name": "A"}
    )
    second = await client.post(
        "/api/v1/entities", headers=headers, json={"entity_type": "invoice", "name": "B"}
    )
    relationship = await client.post(
        "/api/v1/relationships",
        headers=headers,
        json={
            "from_entity_id": first.json()["id"],
            "to_entity_id": second.json()["id"],
            "relationship_type": "LINKED_TO",
        },
    )

    for field in ("relationship_type", "confidence"):
        response = await client.patch(
            f"/api/v1/relationships/{relationship.json()['id']}",
            headers=headers,
            json={field: None},
        )
        assert response.status_code == 422, field


async def test_deleting_referenced_entity_returns_conflict(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, _, _ = authorized_scope(session)
    customer = await client.post(
        "/api/v1/entities", headers=headers, json={"entity_type": "customer", "name": "A"}
    )
    invoice = await client.post(
        "/api/v1/entities", headers=headers, json={"entity_type": "invoice", "name": "B"}
    )
    await client.post(
        "/api/v1/relationships",
        headers=headers,
        json={
            "from_entity_id": customer.json()["id"],
            "to_entity_id": invoice.json()["id"],
            "relationship_type": "CUSTOMER_HAS_INVOICE",
        },
    )

    response = await client.delete(f"/api/v1/entities/{customer.json()['id']}", headers=headers)

    assert response.status_code == 409


async def test_deleting_evidenced_relationship_returns_conflict_and_rolls_back(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, organization, workspace = authorized_scope(session)
    first = await client.post(
        "/api/v1/entities", headers=headers, json={"entity_type": "customer", "name": "A"}
    )
    second = await client.post(
        "/api/v1/entities", headers=headers, json={"entity_type": "invoice", "name": "B"}
    )
    relationship = await client.post(
        "/api/v1/relationships",
        headers=headers,
        json={
            "from_entity_id": first.json()["id"],
            "to_entity_id": second.json()["id"],
            "relationship_type": "SUPPORTED_BY_EVIDENCE",
        },
    )
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="manual",
        uri="manual://relationship-evidence",
    )
    session.add(source)
    session.flush()
    evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="business_fact",
    )
    session.add(evidence)
    session.flush()
    session.add(
        EvidenceLink(
            organization_id=organization.id,
            workspace_id=workspace.id,
            evidence_id=evidence.id,
            relationship_id=UUID(relationship.json()["id"]),
        )
    )
    session.commit()

    response = await client.delete(
        f"/api/v1/relationships/{relationship.json()['id']}", headers=headers
    )

    assert response.status_code == 409
    assert session.query(Entity).count() == 2
