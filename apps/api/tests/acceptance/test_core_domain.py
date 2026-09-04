import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    Document,
    Entity,
    EntityType,
    Event,
    Evidence,
    Memory,
    MemoryType,
    Organization,
    Relationship,
    Source,
    User,
    Workspace,
)
from company_brain.domain.repositories import EntityRepository, TenantScope


def make_tenant(session: Session, suffix: str) -> tuple[Organization, Workspace]:
    organization = Organization(name=f"Org {suffix}", slug=f"org-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name=f"Workspace {suffix}",
        slug=f"workspace-{suffix}",
    )
    session.add(workspace)
    session.commit()
    return organization, workspace


def test_core_aggregates_can_be_persisted_with_evidence_ready_links(session: Session) -> None:
    organization, workspace = make_tenant(session, "alpha")
    user = User(organization_id=organization.id, email="owner@example.com", display_name="Owner")
    customer = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.CUSTOMER,
        name="ABC Ltd.",
        normalized_name="abc",
    )
    invoice = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.INVOICE,
        name="INV/2026/001",
        normalized_name="inv 2026 001",
    )
    session.add_all([user, customer, invoice])
    session.flush()
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="odoo_record",
        uri="odoo://account.move/456",
    )
    relationship = Relationship(
        organization_id=organization.id,
        workspace_id=workspace.id,
        from_entity_id=customer.id,
        to_entity_id=invoice.id,
        relationship_type="CUSTOMER_HAS_INVOICE",
        confidence=1.0,
    )
    event = Event(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=customer.id,
        event_type="INVOICE_OVERDUE",
        payload={"invoice_id": str(invoice.id)},
    )
    document = Document(
        organization_id=organization.id,
        workspace_id=workspace.id,
        title="ABC account note",
        path="customers/abc.md",
        content="# ABC",
    )
    memory = Memory(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=customer.id,
        memory_type=MemoryType.BUSINESS,
        text="ABC raised a delivery concern.",
        confidence=0.9,
    )
    session.add_all([source, relationship, event, document, memory])
    session.flush()
    evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="odoo_field",
        pointer={"model": "account.move", "id": "456", "field": "amount_residual"},
        quote="Amount residual from Odoo record 456",
    )
    session.add(evidence)
    session.commit()

    assert customer.id is not None
    assert relationship.from_entity_id == customer.id
    assert event.payload["invoice_id"] == str(invoice.id)
    assert memory.subject_entity_id == customer.id
    assert evidence.source_id == source.id


def test_entity_repository_never_returns_another_tenants_entity(session: Session) -> None:
    org_a, workspace_a = make_tenant(session, "a")
    org_b, workspace_b = make_tenant(session, "b")
    entity_b = Entity(
        organization_id=org_b.id,
        workspace_id=workspace_b.id,
        entity_type=EntityType.CUSTOMER,
        name="Secret Customer",
        normalized_name="secret customer",
    )
    session.add(entity_b)
    session.commit()

    repository = EntityRepository(session, TenantScope(org_a.id, workspace_a.id))

    assert repository.get(entity_b.id) is None
    assert repository.list() == []


def test_membership_cannot_reference_user_from_another_organization(session: Session) -> None:
    from company_brain.domain.models import Membership

    org_a, workspace_a = make_tenant(session, "membership-a")
    org_b, _ = make_tenant(session, "membership-b")
    user_b = User(
        organization_id=org_b.id,
        email="cross-tenant@example.com",
        display_name="Cross Tenant",
    )
    session.add(user_b)
    session.flush()
    session.add(
        Membership(
            organization_id=org_a.id,
            workspace_id=workspace_a.id,
            user_id=user_b.id,
            role="owner",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_entity_cannot_reference_workspace_from_another_organization(session: Session) -> None:
    org_a, _ = make_tenant(session, "constraint-a")
    _, workspace_b = make_tenant(session, "constraint-b")
    session.add(
        Entity(
            organization_id=org_a.id,
            workspace_id=workspace_b.id,
            entity_type=EntityType.CUSTOMER,
            name="Invalid cross-tenant customer",
            normalized_name="invalid cross tenant customer",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_relationship_cannot_link_entity_from_another_workspace(session: Session) -> None:
    organization, workspace_a = make_tenant(session, "relation-a")
    workspace_b = Workspace(
        organization_id=organization.id,
        name="Workspace relation-b",
        slug="workspace-relation-b",
    )
    session.add(workspace_b)
    session.flush()
    entity_a = Entity(
        organization_id=organization.id,
        workspace_id=workspace_a.id,
        entity_type=EntityType.CUSTOMER,
        name="Customer A",
        normalized_name="customer a",
    )
    entity_b = Entity(
        organization_id=organization.id,
        workspace_id=workspace_b.id,
        entity_type=EntityType.INVOICE,
        name="Invoice B",
        normalized_name="invoice b",
    )
    session.add_all([entity_a, entity_b])
    session.flush()
    session.add(
        Relationship(
            organization_id=organization.id,
            workspace_id=workspace_a.id,
            from_entity_id=entity_a.id,
            to_entity_id=entity_b.id,
            relationship_type="CUSTOMER_HAS_INVOICE",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_external_reference_is_unique_inside_an_organization(session: Session) -> None:
    from company_brain.domain.models import ExternalReference

    organization, workspace = make_tenant(session, "unique")
    first = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.CUSTOMER,
        name="ABC",
        normalized_name="abc",
    )
    second = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.CUSTOMER,
        name="ABC duplicate",
        normalized_name="abc duplicate",
    )
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="odoo_record",
        uri="odoo://res.partner/123",
    )
    session.add_all([first, second, source])
    session.flush()
    session.add(
        ExternalReference(
            organization_id=organization.id,
            workspace_id=workspace.id,
            entity_id=first.id,
            source_id=source.id,
            source_system="odoo",
            source_model="res.partner",
            external_id="123",
        )
    )
    session.commit()
    session.add(
        ExternalReference(
            organization_id=organization.id,
            workspace_id=workspace.id,
            entity_id=second.id,
            source_id=source.id,
            source_system="odoo",
            source_model="res.partner",
            external_id="123",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()
