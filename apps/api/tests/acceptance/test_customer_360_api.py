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
    Event,
    Evidence,
    EvidenceLink,
    Membership,
    Memory,
    MemoryType,
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


def customer_scope(
    session: Session, suffix: str, role: str = "member"
) -> tuple[Organization, Workspace, dict[str, str]]:
    organization = Organization(name=f"Org {suffix}", slug=f"org-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id, name="Main", slug=f"main-{suffix}"
    )
    token = f"customer-360-{suffix}"
    user = User(
        organization_id=organization.id,
        email=f"{suffix}@example.com",
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
            role=role,
        )
    )
    session.commit()
    return organization, workspace, {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": str(organization.id),
        "X-Workspace-ID": str(workspace.id),
    }


def add_entity(
    session: Session,
    organization: Organization,
    workspace: Workspace,
    entity_type: EntityType,
    name: str,
    metadata: object | None = None,
    *,
    aliases: list[object] | None = None,
    lifecycle_status: str = "active",
    created_at: datetime = datetime(2026, 8, 1, tzinfo=UTC),
) -> Entity:
    entity = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=entity_type,
        name=name,
        normalized_name=name.casefold(),
        aliases=aliases or [],
        metadata_={} if metadata is None else metadata,
        lifecycle_status=lifecycle_status,
        created_at=created_at,
    )
    session.add(entity)
    session.flush()
    return entity


def link(
    session: Session,
    organization: Organization,
    workspace: Workspace,
    customer: Entity,
    target: Entity,
    relationship_type: str,
) -> Relationship:
    relationship = Relationship(
        organization_id=organization.id,
        workspace_id=workspace.id,
        from_entity_id=customer.id,
        to_entity_id=target.id,
        relationship_type=relationship_type,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(relationship)
    session.flush()
    return relationship


async def test_customer_360_returns_deterministic_evidenced_business_context(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "main")
    customer = add_entity(
        session,
        organization,
        workspace,
        EntityType.CUSTOMER,
        "Acme",
        {"email": "hello@acme.test", "phone": "+84 123"},
    )
    order = add_entity(
        session,
        organization,
        workspace,
        EntityType.ORDER,
        "SO-1",
        {
            "amount_total": 1250.5,
            "currency": "USD",
            "state": "sale",
            "date_order": "2026-08-01T10:00:00+00:00",
        },
    )
    invoice = add_entity(
        session,
        organization,
        workspace,
        EntityType.INVOICE,
        "INV-1",
        {
            "amount_total": 300.0,
            "currency": "USD",
            "payment_state": "not_paid",
            "invoice_date": "2026-07-01T00:00:00+00:00",
            "due_date": "2026-07-31T00:00:00+00:00",
        },
    )
    ticket = add_entity(
        session,
        organization,
        workspace,
        EntityType.TICKET,
        "Delivery issue",
        {"priority": "3", "stage": "open"},
    )
    order_relationship = link(
        session, organization, workspace, customer, order, "CUSTOMER_HAS_ORDER"
    )
    link(session, organization, workspace, customer, invoice, "CUSTOMER_HAS_INVOICE")
    link(session, organization, workspace, customer, ticket, "CUSTOMER_HAS_TICKET")
    event = Event(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=ticket.id,
        event_type="ticket_updated",
        occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
        payload={"status": "open"},

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    future_event = Event(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=ticket.id,
        event_type="future_ticket_update",
        occurred_at=datetime(2026, 9, 2, tzinfo=UTC),
        payload={"status": "future"},

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="fixture",
        uri="fixture://customer-360",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add_all([event, future_event, source])
    session.flush()
    memory = Memory(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=customer.id,
        memory_type=MemoryType.BUSINESS,
        text="Prefers quarterly reviews",
        review_status="approved",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(memory)
    evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="field",
        pointer={"field": "amount_total"},
        quote="1250.5 USD",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(evidence)
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
    session.add(
        EvidenceLink(
            organization_id=organization.id,
            workspace_id=workspace.id,
            evidence_id=evidence.id,
            relationship_id=order_relationship.id,

            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
    )
    session.commit()

    response = await client.get(
        f"/api/v1/customers/{customer.id}/360",
        headers=headers,
        params={"as_of": "2026-08-14T00:00:00Z"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["customer"] == {
        "id": str(customer.id),
        "name": "Acme",
        "aliases": [],
        "email": "hello@acme.test",
        "phone": "+84 123",
    }
    assert [item["name"] for item in body["orders"]] == ["SO-1"]
    assert [item["name"] for item in body["invoices"]] == ["INV-1"]
    assert [item["name"] for item in body["tickets"]] == ["Delivery issue"]
    assert body["metrics"]["revenue_total"] == {
        "values": [{"currency": "USD", "value": 1250.5}],
        "evidence_ids": [str(evidence.id)],
        "calculation": "sum completed order amount_total grouped by currency",
    }
    assert body["metrics"]["overdue_invoice_count"] == {
        "value": 1,
        "as_of": "2026-08-14T00:00:00Z",
        "evidence_ids": [],
        "calculation": "count unpaid invoices with due_date before as_of",
    }
    assert [item["event_type"] for item in body["timeline"]] == ["ticket_updated"]
    assert [item["text"] for item in body["memories"]] == ["Prefers quarterly reviews"]
    assert body["relationships"][0]["relationship_type"] == "CUSTOMER_HAS_INVOICE"
    assert body["evidence"] == [
        {
            "id": str(evidence.id),
            "source_id": str(source.id),
            "evidence_type": "field",
            "pointer": {"field": "amount_total"},
            "quote": "1250.5 USD",
        }
    ]
    assert body["data_gaps"] == [
        "missing_revenue_growth_baseline:USD",
        "missing_risk_evidence:OVERDUE_PAYMENT",
    ]


async def test_customer_360_is_tenant_and_customer_type_safe(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization_a, workspace_a, headers_a = customer_scope(session, "scope-a")
    organization_b, workspace_b, _ = customer_scope(session, "scope-b")
    hidden = add_entity(
        session, organization_b, workspace_b, EntityType.CUSTOMER, "Hidden"
    )
    wrong_type = add_entity(
        session, organization_a, workspace_a, EntityType.SUPPLIER, "Supplier"
    )
    session.commit()

    params = {"as_of": "2026-08-14T00:00:00Z"}
    hidden_response = await client.get(
        f"/api/v1/customers/{hidden.id}/360", headers=headers_a, params=params
    )
    wrong_type_response = await client.get(
        f"/api/v1/customers/{wrong_type.id}/360", headers=headers_a, params=params
    )

    assert hidden_response.status_code == 404
    assert wrong_type_response.status_code == 404


async def test_customer_metrics_and_risk_views_share_deterministic_read_model(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "views")
    customer = add_entity(
        session, organization, workspace, EntityType.CUSTOMER, "Views Customer"
    )
    session.commit()
    params = {"as_of": "2026-08-14T00:00:00Z"}

    metrics = await client.get(
        f"/api/v1/customers/{customer.id}/metrics",
        headers=headers,
        params={**params, "window": "6m"},
    )
    risk = await client.get(
        f"/api/v1/customers/{customer.id}/risk", headers=headers, params=params
    )
    unsupported = await client.get(
        f"/api/v1/customers/{customer.id}/metrics",
        headers=headers,
        params={**params, "window": "12m"},
    )

    assert metrics.status_code == 200
    assert metrics.json()["window"] == "6m"
    assert metrics.json()["metrics"]["revenue_total"]["values"] == []
    assert risk.status_code == 200
    assert risk.json()["signals"] == []
    assert "missing_activity_history" in risk.json()["data_gaps"]
    assert unsupported.status_code == 422


async def test_customer_views_reject_as_of_too_early_for_six_month_window(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "minimum-as-of")
    customer = add_entity(session, organization, workspace, EntityType.CUSTOMER, "Old")
    session.commit()

    response = await client.get(
        f"/api/v1/customers/{customer.id}/360",
        headers=headers,
        params={"as_of": "0001-01-01T00:00:00Z"},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "as_of",
    ["9999-12-31T23:59:59-14:00", "0001-01-01T00:00:00+14:00"],
)
async def test_customer_views_reject_utc_normalization_overflow(
    client: httpx.AsyncClient,
    session: Session,
    as_of: str,
) -> None:
    organization, workspace, headers = customer_scope(session, f"utc-overflow-{as_of[0]}")
    customer = add_entity(session, organization, workspace, EntityType.CUSTOMER, "Boundary")
    session.commit()

    response = await client.get(
        f"/api/v1/customers/{customer.id}/360",
        headers=headers,
        params={"as_of": as_of},
    )

    assert response.status_code == 422


async def test_historical_view_filters_future_and_invalid_context_and_reports_memory_truncation(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "historical")
    customer = add_entity(session, organization, workspace, EntityType.CUSTOMER, "History")
    future_order = add_entity(
        session,
        organization,
        workspace,
        EntityType.ORDER,
        "Future",
        created_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    future_relationship = link(
        session, organization, workspace, customer, future_order, "CUSTOMER_HAS_ORDER"
    )
    future_relationship.created_at = datetime(2027, 1, 1, tzinfo=UTC)
    inactive = add_entity(
        session,
        organization,
        workspace,
        EntityType.ORDER,
        "Inactive",
        lifecycle_status="merged",
    )
    invalid_relationship = link(
        session, organization, workspace, customer, inactive, "CUSTOMER_HAS_ORDER"
    )
    for index in range(101):
        session.add(
            Memory(
                organization_id=organization.id,
                workspace_id=workspace.id,
                subject_entity_id=customer.id,
                memory_type=MemoryType.BUSINESS,
                text=f"Memory {index:03d}",
                review_status="approved",

                created_at=datetime(2026, 8, 1, tzinfo=UTC),
            )
        )
    future_memory = Memory(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=customer.id,
        memory_type=MemoryType.BUSINESS,
        text="Future memory",
        review_status="approved",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    future_memory.created_at = datetime(2027, 1, 1, tzinfo=UTC)
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="fixture",
        uri="fixture://historical-future",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add_all([future_memory, source])
    session.flush()
    future_evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="field",
        pointer={"future": True},

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    future_evidence.created_at = datetime(2027, 1, 1, tzinfo=UTC)
    session.add(future_evidence)
    session.flush()
    session.add(
        EvidenceLink(
            organization_id=organization.id,
            workspace_id=workspace.id,
            evidence_id=future_evidence.id,
            entity_id=customer.id,

            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
    )
    session.commit()

    response = await client.get(
        f"/api/v1/customers/{customer.id}/360",
        headers=headers,
        params={"as_of": "2026-08-14T00:00:00Z"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["orders"] == []
    assert all(
        item["id"] not in {str(future_relationship.id), str(invalid_relationship.id)}
        for item in body["relationships"]
    )
    assert len(body["memories"]) == 100
    assert all(item["text"] != "Future memory" for item in body["memories"])
    assert body["evidence"] == []
    assert "memory_truncated" in body["data_gaps"]


async def test_customer_evidence_is_bounded_and_reports_truncation(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "evidence-cap")
    customer = add_entity(session, organization, workspace, EntityType.CUSTOMER, "Bounded")
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="fixture",
        uri="fixture://evidence-cap",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(source)
    session.flush()
    for index in range(501):
        evidence = Evidence(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_id=source.id,
            evidence_type="field",
            pointer={"index": index},

            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        session.add(evidence)
        session.flush()
        session.add(
            EvidenceLink(
                organization_id=organization.id,
                workspace_id=workspace.id,
                evidence_id=evidence.id,
                entity_id=customer.id,

                created_at=datetime(2026, 8, 1, tzinfo=UTC),
            )
        )
    session.commit()

    response = await client.get(
        f"/api/v1/customers/{customer.id}/360",
        headers=headers,
        params={"as_of": "2026-08-14T00:00:00Z"},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["evidence"]) == 500
    assert "evidence_truncated" in body["data_gaps"]


async def test_customer_360_sanitizes_non_finite_confidence_values(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "confidence")
    customer = add_entity(session, organization, workspace, EntityType.CUSTOMER, "Confidence")
    order = add_entity(session, organization, workspace, EntityType.ORDER, "SO-INF")
    relationship = link(
        session, organization, workspace, customer, order, "CUSTOMER_HAS_ORDER"
    )
    relationship.confidence = float("inf")
    memory = Memory(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=customer.id,
        memory_type=MemoryType.BUSINESS,
        text="Unbounded confidence",
        confidence=float("-inf"),
        review_status="approved",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(memory)
    session.commit()

    response = await client.get(
        f"/api/v1/customers/{customer.id}/360",
        headers=headers,
        params={"as_of": "2026-08-14T00:00:00Z"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["relationships"][0]["confidence"] is None
    assert body["memories"][0]["confidence"] is None
    assert f"invalid_relationship_confidence:{relationship.id}" in body["data_gaps"]
    assert f"invalid_memory_confidence:{memory.id}" in body["data_gaps"]


async def test_customer_360_sanitizes_nested_non_finite_json_values(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "nested-json")
    customer = add_entity(session, organization, workspace, EntityType.CUSTOMER, "Nested")
    order = add_entity(
        session,
        organization,
        workspace,
        EntityType.ORDER,
        "SO-NESTED",
        {"nested": {"values": [1, float("nan"), float("inf")]}} ,
    )
    link(session, organization, workspace, customer, order, "CUSTOMER_HAS_ORDER")
    event = Event(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=order.id,
        event_type="nested_payload",
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        payload={"nested": [float("-inf")]},

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    memory = Memory(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=customer.id,
        memory_type=MemoryType.BUSINESS,
        text="Nested facts",
        structured_facts={"score": float("nan")},
        review_status="approved",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="fixture",
        uri="fixture://nested-json",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add_all([event, memory, source])
    session.flush()
    evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="field",
        pointer={"coordinates": [float("inf")]},

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(evidence)
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

    response = await client.get(
        f"/api/v1/customers/{customer.id}/360",
        headers=headers,
        params={"as_of": "2026-08-14T00:00:00Z"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["orders"][0]["attributes"]["nested"]["values"] == [1, None, None]
    assert body["timeline"][0]["payload"]["nested"] == [None]
    assert body["memories"][0]["structured_facts"]["score"] is None
    assert body["evidence"][0]["pointer"]["coordinates"] == [None]
    assert f"invalid_business_attributes:{order.id}" in body["data_gaps"]
    assert f"invalid_timeline_payload:{event.id}" in body["data_gaps"]
    assert f"invalid_memory_structured_facts:{memory.id}" in body["data_gaps"]
    assert f"invalid_evidence_pointer:{evidence.id}" in body["data_gaps"]


async def test_customer_360_sanitizes_malformed_customer_aliases(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "aliases")
    customer = add_entity(
        session,
        organization,
        workspace,
        EntityType.CUSTOMER,
        "Aliases",
        aliases=["Valid alias", 7, None, "Other alias"],
    )
    session.commit()

    response = await client.get(
        f"/api/v1/customers/{customer.id}/360",
        headers=headers,
        params={"as_of": "2026-08-14T00:00:00Z"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["customer"]["aliases"] == ["Valid alias", "Other alias"]
    assert f"invalid_customer_aliases:{customer.id}" in body["data_gaps"]


async def test_customer_views_fail_closed_for_malformed_json_roots(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "json-roots")
    customer = add_entity(
        session, organization, workspace, EntityType.CUSTOMER, "Roots", []
    )
    order = add_entity(
        session, organization, workspace, EntityType.ORDER, "SO-ROOT", ["not", "an", "object"]
    )
    link(session, organization, workspace, customer, order, "CUSTOMER_HAS_ORDER")
    event = Event(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=order.id,
        event_type="bad_root",
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        payload="not-an-object",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    memory = Memory(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=customer.id,
        memory_type=MemoryType.BUSINESS,
        text="Bad root",
        structured_facts=None,
        review_status="approved",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="fixture",
        uri="fixture://json-roots",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add_all([event, memory, source])
    session.flush()
    evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="field",
        pointer=[],

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(evidence)
    session.flush()
    session.add(EvidenceLink(
        organization_id=organization.id,
        workspace_id=workspace.id,
        evidence_id=evidence.id,
        entity_id=order.id,

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    ))
    session.commit()

    params = {"as_of": "2026-08-14T00:00:00Z"}
    response = await client.get(
        f"/api/v1/customers/{customer.id}/360", headers=headers, params=params
    )
    metrics = await client.get(
        f"/api/v1/customers/{customer.id}/metrics",
        headers=headers,
        params={**params, "window": "6m"},
    )
    risk = await client.get(
        f"/api/v1/customers/{customer.id}/risk", headers=headers, params=params
    )

    assert response.status_code == metrics.status_code == risk.status_code == 200
    body = response.json()
    assert body["customer"]["email"] is None
    assert body["orders"][0]["attributes"] == {}
    assert body["timeline"][0]["payload"] == {}
    assert body["memories"][0]["structured_facts"] == {}
    assert body["evidence"][0]["pointer"] == {}
    expected = {
        f"invalid_customer_metadata:{customer.id}",
        f"invalid_business_attributes:{order.id}",
        f"invalid_timeline_payload:{event.id}",
        f"invalid_memory_structured_facts:{memory.id}",
        f"invalid_evidence_pointer:{evidence.id}",
    }
    assert expected <= set(body["data_gaps"])
    assert f"invalid_entity_metadata:{order.id}" in metrics.json()["data_gaps"]
    assert f"invalid_entity_metadata:{order.id}" in risk.json()["data_gaps"]


async def test_customer_views_apply_relationship_validity_intervals(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "validity")
    customer = add_entity(session, organization, workspace, EntityType.CUSTOMER, "Validity")
    as_of = datetime(2026, 8, 14, tzinfo=UTC)

    def related_order(
        name: str, amount: int, valid_from: datetime | None, valid_to: datetime | None
    ) -> tuple[Entity, Relationship]:
        order = add_entity(
            session, organization, workspace, EntityType.ORDER, name,
            {
                "state": "sale", "amount_total": amount, "currency": "USD",
                "date_order": "2026-08-01T00:00:00Z",
            },
        )
        relationship = link(
            session, organization, workspace, customer, order, "CUSTOMER_HAS_ORDER"
        )
        relationship.valid_from = valid_from
        relationship.valid_to = valid_to
        return order, relationship

    open_order, _ = related_order("Open", 10, None, None)
    starts_exact, _ = related_order("Starts exact", 20, as_of, None)
    future_order, _ = related_order(
        "Future", 40, datetime(2026, 8, 15, tzinfo=UTC), None
    )
    expired_order, _ = related_order(
        "Expired", 80, None, datetime(2026, 8, 13, tzinfo=UTC)
    )
    ends_exact, _ = related_order("Ends exact", 160, None, as_of)
    session.commit()

    params = {"as_of": "2026-08-14T00:00:00Z"}
    response = await client.get(
        f"/api/v1/customers/{customer.id}/360", headers=headers, params=params
    )
    metrics = await client.get(
        f"/api/v1/customers/{customer.id}/metrics",
        headers=headers,
        params={**params, "window": "6m"},
    )
    risk = await client.get(
        f"/api/v1/customers/{customer.id}/risk", headers=headers, params=params
    )

    assert response.status_code == metrics.status_code == risk.status_code == 200
    included_ids = {item["id"] for item in response.json()["orders"]}
    assert included_ids == {str(open_order.id), str(starts_exact.id)}
    assert str(future_order.id) not in included_ids
    assert str(expired_order.id) not in included_ids
    assert str(ends_exact.id) not in included_ids
    revenue = metrics.json()["metrics"]["revenue_total"]["values"]
    assert revenue == [{"currency": "USD", "value": 30.0}]
    assert risk.json()["signals"] == []


async def test_customer_views_exclude_future_created_and_invalid_target_context(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "context-isolation")
    customer = add_entity(session, organization, workspace, EntityType.CUSTOMER, "Isolation")
    wrong_target = add_entity(
        session, organization, workspace, EntityType.TICKET, "Wrong target"
    )
    malformed = link(
        session,
        organization,
        workspace,
        customer,
        wrong_target,
        "CUSTOMER_HAS_ORDER",
    )
    future_created_event = Event(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=customer.id,
        event_type="backfilled_future",
        occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
        created_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    wrong_target_event = Event(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=wrong_target.id,
        event_type="unrelated_target",
        occurred_at=datetime(2026, 7, 2, tzinfo=UTC),

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="fixture",
        uri="fixture://context-isolation",

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add_all([future_created_event, wrong_target_event, source])
    session.flush()
    future_evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="event",
        pointer={"event": "future"},

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    wrong_target_evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="entity",
        pointer={"entity": "wrong-target"},

        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add_all([future_evidence, wrong_target_evidence])
    session.flush()
    session.add_all([
        EvidenceLink(
            organization_id=organization.id,
            workspace_id=workspace.id,
            evidence_id=future_evidence.id,
            event_id=future_created_event.id,

            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        EvidenceLink(
            organization_id=organization.id,
            workspace_id=workspace.id,
            evidence_id=wrong_target_evidence.id,
            entity_id=wrong_target.id,

            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
    ])
    session.commit()

    params = {"as_of": "2026-08-14T00:00:00Z"}
    response = await client.get(
        f"/api/v1/customers/{customer.id}/360", headers=headers, params=params
    )
    metrics = await client.get(
        f"/api/v1/customers/{customer.id}/metrics",
        headers=headers,
        params={**params, "window": "6m"},
    )
    risk = await client.get(
        f"/api/v1/customers/{customer.id}/risk", headers=headers, params=params
    )

    assert response.status_code == metrics.status_code == risk.status_code == 200
    body = response.json()
    assert body["timeline"] == []
    assert body["evidence"] == []
    assert body["relationships"] == []
    assert f"invalid_related_entity:{malformed.id}" in body["data_gaps"]
    assert "missing_activity_history" in metrics.json()["data_gaps"]
    assert "missing_activity_history" in risk.json()["data_gaps"]


async def test_customer_views_exclude_future_created_evidence_sources(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "future-source")
    customer = add_entity(session, organization, workspace, EntityType.CUSTOMER, "Source")
    order = add_entity(
        session,
        organization,
        workspace,
        EntityType.ORDER,
        "SO-SOURCE",
        {
            "state": "sale", "amount_total": 50, "currency": "USD",
            "date_order": "2026-08-01T00:00:00Z",
        },
    )
    link(session, organization, workspace, customer, order, "CUSTOMER_HAS_ORDER")
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="fixture",
        uri="fixture://future-source",
        created_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    session.add(source)
    session.flush()
    evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="field",
        pointer={"field": "amount_total"},
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(evidence)
    session.flush()
    session.add(EvidenceLink(
        organization_id=organization.id,
        workspace_id=workspace.id,
        evidence_id=evidence.id,
        entity_id=order.id,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    ))
    session.commit()

    params = {"as_of": "2026-08-14T00:00:00Z"}
    response = await client.get(
        f"/api/v1/customers/{customer.id}/360", headers=headers, params=params
    )
    metrics = await client.get(
        f"/api/v1/customers/{customer.id}/metrics",
        headers=headers,
        params={**params, "window": "6m"},
    )
    risk = await client.get(
        f"/api/v1/customers/{customer.id}/risk", headers=headers, params=params
    )

    assert response.status_code == metrics.status_code == risk.status_code == 200
    assert response.json()["evidence"] == []
    assert metrics.json()["metrics"]["revenue_total"]["evidence_ids"] == []
    assert risk.json()["signals"] == []


@pytest.mark.anyio
async def test_phase11_risk_assessment_is_deterministic_and_evidence_backed(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "phase11")
    customer = add_entity(
        session, organization, workspace, EntityType.CUSTOMER, "Phase 11 Customer"
    )
    ticket_specs = [
        ("2026-06-20T00:00:00Z", None),
        ("2026-07-20T00:00:00Z", "delivery"),
        ("2026-07-25T00:00:00Z", "delivery_complaint"),
        ("2026-08-01T00:00:00Z", None),
        ("2026-08-10T00:00:00Z", None),
    ]
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="fixture",
        uri="fixture://phase11-risk",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(source)
    session.flush()
    evidence_ids: list[str] = []
    tickets: list[Entity] = []
    for index, (opened_at, complaint_type) in enumerate(ticket_specs):
        metadata: dict[str, object] = {"opened_at": opened_at}
        if complaint_type is not None:
            metadata["complaint_type"] = complaint_type
        ticket = add_entity(
            session,
            organization,
            workspace,
            EntityType.TICKET,
            f"Ticket {index}",
            metadata,
        )
        tickets.append(ticket)
        link(
            session,
            organization,
            workspace,
            customer,
            ticket,
            "CUSTOMER_HAS_TICKET",
        )
        evidence = Evidence(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_id=source.id,
            evidence_type="record",
            pointer={"ticket": index},
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        session.add(evidence)
        session.flush()
        evidence_ids.append(str(evidence.id))
        session.add(
            EvidenceLink(
                organization_id=organization.id,
                workspace_id=workspace.id,
                evidence_id=evidence.id,
                entity_id=ticket.id,
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
            )
        )
    session.commit()

    response = await client.get(
        f"/api/v1/customers/{customer.id}/risk-assessment",
        headers=headers,
        params={"as_of": "2026-08-14T00:00:00Z"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["customer_id"] == str(customer.id)
    assert body["as_of"] == "2026-08-14T00:00:00Z"
    assert body["calculation_version"] == "customer-risk.v1"
    assert body["score"] == 45
    assert body["severity"] == "moderate"
    assert [signal["type"] for signal in body["signals"]] == [
        "DELIVERY_COMPLAINTS",
        "TICKET_INCREASE",
    ]
    assert sorted(
        {
            evidence_id
            for signal in body["signals"]
            for evidence_id in signal["evidence_ids"]
        }
    ) == sorted(evidence_ids)

    tickets[0].metadata_ = {
        **tickets[0].metadata_,
        "complaint_type": "delivery",
    }
    session.commit()
    repeated = await client.get(
        f"/api/v1/customers/{customer.id}/risk-assessment",
        headers=headers,
        params={"as_of": "2026-08-14T00:00:00Z"},
    )
    assert repeated.status_code == 200
    assert repeated.json() == body


@pytest.mark.anyio
async def test_phase11_historical_assessment_survives_later_customer_deletion(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "risk-delete-history")
    customer = add_entity(
        session,
        organization,
        workspace,
        EntityType.CUSTOMER,
        "Deleted Later",
    )
    session.commit()
    path = f"/api/v1/customers/{customer.id}/risk-assessment"
    params = {"as_of": "2026-08-14T00:00:00Z"}

    before_delete = await client.get(path, headers=headers, params=params)
    assert before_delete.status_code == 200

    session.delete(customer)
    session.commit()

    historical = await client.get(path, headers=headers, params=params)
    assert historical.status_code == 200
    assert historical.json() == before_delete.json()
    assert (
        await client.get(
            path,
            headers=headers,
            params={"as_of": "2999-01-01T00:00:00Z"},
        )
    ).status_code == 404


@pytest.mark.anyio
async def test_phase11_historical_assessment_does_not_leak_across_later_scope_transfer(
    client: httpx.AsyncClient, session: Session
) -> None:
    old_organization, old_workspace, old_headers = customer_scope(
        session, "risk-transfer-old"
    )
    new_organization, new_workspace, new_headers = customer_scope(
        session, "risk-transfer-new"
    )
    customer = add_entity(
        session,
        old_organization,
        old_workspace,
        EntityType.CUSTOMER,
        "Transferred Later",
    )
    session.commit()
    path = f"/api/v1/customers/{customer.id}/risk-assessment"
    historical_params = {"as_of": "2026-08-14T00:00:00Z"}

    old_before_transfer = await client.get(
        path, headers=old_headers, params=historical_params
    )
    assert old_before_transfer.status_code == 200

    new_organization_id = new_organization.id
    new_workspace_id = new_workspace.id
    customer.organization_id = new_organization_id
    customer.workspace_id = new_workspace_id
    session.commit()

    assert (
        await client.get(path, headers=new_headers, params=historical_params)
    ).status_code == 404
    old_historical = await client.get(
        path, headers=old_headers, params=historical_params
    )
    assert old_historical.status_code == 200
    assert old_historical.json() == old_before_transfer.json()
    assert (
        await client.get(
            path,
            headers=old_headers,
            params={"as_of": "2999-01-01T00:00:00Z"},
        )
    ).status_code == 404
    assert (
        await client.get(
            path,
            headers=new_headers,
            params={"as_of": "2999-01-01T00:00:00Z"},
        )
    ).status_code == 200


@pytest.mark.anyio
async def test_phase11_risk_assessment_enforces_auth_scope_and_timezone(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "phase11-secure")
    customer = add_entity(
        session, organization, workspace, EntityType.CUSTOMER, "Scoped Customer"
    )
    _, _, other_headers = customer_scope(session, "phase11-other")
    session.commit()
    path = f"/api/v1/customers/{customer.id}/risk-assessment"

    unauthenticated = await client.get(
        path, params={"as_of": "2026-08-14T00:00:00Z"}
    )
    naive_time = await client.get(
        path, headers=headers, params={"as_of": "2026-08-14T00:00:00"}
    )
    cross_tenant = await client.get(
        path,
        headers=other_headers,
        params={"as_of": "2026-08-14T00:00:00Z"},
    )

    assert unauthenticated.status_code == 401
    assert naive_time.status_code == 422
    assert cross_tenant.status_code == 404


@pytest.mark.anyio
async def test_phase11_historical_risk_excludes_future_created_ticket(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = customer_scope(session, "phase11-future")
    customer = add_entity(
        session, organization, workspace, EntityType.CUSTOMER, "Historical Customer"
    )
    future_ticket = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.TICKET,
        name="Future-created complaint",
        normalized_name="future-created complaint",
        metadata_={
            "opened_at": "2026-08-01T00:00:00Z",
            "complaint_type": "delivery",
        },
        created_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    session.add(future_ticket)
    session.flush()
    session.add(
        Relationship(
            organization_id=organization.id,
            workspace_id=workspace.id,
            from_entity_id=customer.id,
            to_entity_id=future_ticket.id,
            relationship_type="CUSTOMER_HAS_TICKET",
            created_at=datetime(2026, 8, 15, tzinfo=UTC),
        )
    )
    session.commit()

    response = await client.get(
        f"/api/v1/customers/{customer.id}/risk-assessment",
        headers=headers,
        params={"as_of": "2026-08-14T00:00:00Z"},
    )

    assert response.status_code == 200
    assert response.json()["score"] == 0
    assert response.json()["signals"] == []
