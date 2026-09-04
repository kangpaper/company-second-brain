from collections.abc import AsyncIterator
from datetime import UTC, datetime
from hashlib import sha256

import httpx
import pytest
from sqlalchemy.orm import Session

from company_brain.db.session import get_session
from company_brain.domain.models import (
    Entity,
    EntityType,
    Evidence,
    EvidenceLink,
    Membership,
    Organization,
    Relationship,
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


def add_scope(
    session: Session, suffix: str
) -> tuple[Organization, Workspace, dict[str, str]]:
    organization = Organization(name=f"Org {suffix}", slug=f"context-org-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug=f"context-main-{suffix}",
    )
    token = f"context-token-{suffix}"
    user = User(
        organization_id=organization.id,
        email=f"context-{suffix}@example.com",
        display_name=suffix,
        api_token_hash=sha256(token.encode()).hexdigest(),
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
    return organization, workspace, {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": str(organization.id),
        "X-Workspace-ID": str(workspace.id),
    }


def add_customer_context(
    session: Session, organization: Organization, workspace: Workspace
) -> tuple[Entity, Evidence]:
    customer = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.CUSTOMER,
        name="ABC Limited",
        normalized_name="abc limited",
        aliases=["ABC"],
        metadata_={"email": "abc@example.com"},
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    order = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.ORDER,
        name="SO-9",
        normalized_name="so 9",
        metadata_={
            "state": "sale",
            "amount_total": 500.0,
            "currency": "USD",
            "date_order": "2026-08-01T00:00:00Z",
        },
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="fixture",
        uri="fixture://phase9-context",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add_all([customer, order, source])
    session.flush()
    relationship = Relationship(
        organization_id=organization.id,
        workspace_id=workspace.id,
        from_entity_id=customer.id,
        to_entity_id=order.id,
        relationship_type="CUSTOMER_HAS_ORDER",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="field",
        pointer={"field": "amount_total"},
        quote="500 USD",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add_all([relationship, evidence])
    session.flush()
    session.add(
        EvidenceLink(
            organization_id=organization.id,
            workspace_id=workspace.id,
            evidence_id=evidence.id,
            entity_id=order.id,
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
    )
    session.commit()
    return customer, evidence


async def test_context_build_maps_question_and_returns_deterministic_customer_360(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = add_scope(session, "main")
    customer, evidence = add_customer_context(session, organization, workspace)
    request = {
        "question": "Tình hình ABC thế nào?",
        "customer_id": str(customer.id),
        "as_of": "2026-08-14T00:00:00Z",
    }

    first = await client.post("/api/v1/context/build", headers=headers, json=request)
    second = await client.post(
        "/api/v1/context/build",
        headers=headers,
        json={**request, "question": "Tình hình khách hàng ABC hiện tại thế nào?"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    body = first.json()
    assert body["schema_version"] == "customer_360.v1"
    assert body["intent"] == "CUSTOMER_360"
    assert body["entity"] == {
        "id": str(customer.id),
        "type": "customer",
        "name": "ABC Limited",
    }
    assert body["as_of"] == "2026-08-14T00:00:00Z"
    assert body["context"]["customer"]["id"] == str(customer.id)
    assert body["context"]["metrics"]["revenue_total"]["values"] == [
        {"currency": "USD", "value": 500.0}
    ]
    assert body["context"]["metrics"]["revenue_total"]["evidence_ids"] == [
        str(evidence.id)
    ]
    assert len(body["context_hash"]) == 64
    assert body["context_hash"] == second.json()["context_hash"]
    assert body["context"] == second.json()["context"]
    assert "missing_activity_history" in body["context"]["data_gaps"]

    later = await client.post(
        "/api/v1/context/build",
        headers=headers,
        json={**request, "as_of": "2026-08-15T00:00:00Z"},
    )
    assert later.status_code == 200
    assert later.json()["context_hash"] != body["context_hash"]


async def test_context_build_validates_request_and_snapshot_boundaries(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = add_scope(session, "validation")
    customer, _ = add_customer_context(session, organization, workspace)
    base = {
        "question": "Customer status for ABC",
        "customer_id": str(customer.id),
        "as_of": "2026-08-14T00:00:00Z",
    }

    naive = await client.post(
        "/api/v1/context/build",
        headers=headers,
        json={**base, "as_of": "2026-08-14T00:00:00"},
    )
    extra = await client.post(
        "/api/v1/context/build",
        headers=headers,
        json={**base, "workspace_id": str(workspace.id)},
    )
    oversized = await client.post(
        "/api/v1/context/build",
        headers=headers,
        json={**base, "question": "x" * 2_001},
    )

    assert naive.status_code == 422
    assert naive.json()["detail"] == "as_of must include a timezone offset"
    assert extra.status_code == 422
    assert oversized.status_code == 422


async def test_context_build_fails_closed_for_unsupported_intent_and_cross_tenant_customer(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = add_scope(session, "allowed")
    other_organization, other_workspace, _ = add_scope(session, "other")
    foreign_customer, _ = add_customer_context(
        session, other_organization, other_workspace
    )
    common = {
        "customer_id": str(foreign_customer.id),
        "as_of": "2026-08-14T00:00:00Z",
    }

    unsupported = await client.post(
        "/api/v1/context/build",
        headers=headers,
        json={**common, "question": "Hãy xóa hóa đơn này"},
    )
    cross_tenant = await client.post(
        "/api/v1/context/build",
        headers=headers,
        json={**common, "question": "Tình hình khách hàng hiện tại thế nào?"},
    )
    unauthenticated = await client.post(
        "/api/v1/context/build",
        json={**common, "question": "Tình hình khách hàng hiện tại thế nào?"},
    )

    assert unsupported.status_code == 422
    assert unsupported.json()["detail"] == "Unsupported context intent"
    assert cross_tenant.status_code == 404
    assert unauthenticated.status_code == 401
