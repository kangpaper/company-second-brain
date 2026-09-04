from collections.abc import AsyncIterator
from hashlib import sha256
from uuid import UUID

import httpx
import pytest
from sqlalchemy.orm import Session

from company_brain.db.session import get_session
from company_brain.domain.models import (
    Entity,
    EntityMerge,
    EntityResolutionAudit,
    EntityType,
    Event,
    Evidence,
    EvidenceLink,
    ExternalReference,
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


def scope(
    session: Session, suffix: str, role: str = "owner"
) -> tuple[Organization, Workspace, dict[str, str]]:
    organization = Organization(name=f"Org {suffix}", slug=f"org-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name=f"Workspace {suffix}",
        slug=f"workspace-{suffix}",
    )
    user = User(
        organization_id=organization.id,
        email=f"{suffix}@example.com",
        display_name=suffix,
        api_token_hash=sha256(f"token-{suffix}".encode()).hexdigest(),
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
    return (
        organization,
        workspace,
        {
            "Authorization": f"Bearer token-{suffix}",
            "X-Organization-ID": str(organization.id),
            "X-Workspace-ID": str(workspace.id),
        },
    )


def add_entity(
    session: Session, organization: Organization, workspace: Workspace, name: str, **metadata: str
) -> Entity:
    entity = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.CUSTOMER,
        name=name,
        normalized_name=name.casefold(),
        metadata_=metadata,
    )
    session.add(entity)
    session.commit()
    return entity


async def test_external_reference_resolution_returns_existing_entity_without_case(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = scope(session, "external")
    entity = add_entity(session, organization, workspace, "Acme")
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="odoo_instance",
        uri="https://odoo.example.com/mcp",
    )
    session.add(source)
    session.flush()
    session.add(
        ExternalReference(
            organization_id=organization.id,
            workspace_id=workspace.id,
            entity_id=entity.id,
            source_id=source.id,
            source_system="odoo",
            source_model="res.partner",
            external_id="42",
        )
    )
    session.commit()

    response = await client.post(
        "/api/v1/entity-resolution/resolve",
        headers=headers,
        json={
            "entity_type": "customer",
            "name": "Other spelling",
            "external_reference": {
                "source_id": str(source.id),
                "source_model": "res.partner",
                "external_id": "42",
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "outcome": "matched",
        "match_method": "external_reference",
        "entity_id": str(entity.id),
        "case_id": None,
        "candidates": [],
    }


async def test_external_reference_does_not_bypass_requested_entity_type(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = scope(session, "external-type")
    entity = add_entity(session, organization, workspace, "Invoice 42")
    entity.entity_type = EntityType.INVOICE
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="odoo_instance",
        uri="https://odoo.example.com/mcp",
    )
    session.add(source)
    session.flush()
    session.add(
        ExternalReference(
            organization_id=organization.id,
            workspace_id=workspace.id,
            entity_id=entity.id,
            source_id=source.id,
            source_system="odoo",
            source_model="account.move",
            external_id="42",
        )
    )
    session.commit()

    response = await client.post(
        "/api/v1/entity-resolution/resolve",
        headers=headers,
        json={
            "entity_type": "customer",
            "name": "Invoice 42",
            "external_reference": {
                "source_id": str(source.id),
                "source_model": "account.move",
                "external_id": "42",
            },
        },
    )

    assert response.status_code == 202
    assert response.json()["outcome"] == "review_required"


async def test_exact_name_match_is_not_reported_as_identifier_match(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = scope(session, "exact-name")
    entity = add_entity(session, organization, workspace, "Àcme Corporation")

    response = await client.post(
        "/api/v1/entity-resolution/resolve",
        headers=headers,
        json={"entity_type": "customer", "name": "acme corporation"},
    )

    assert response.status_code == 200
    assert response.json()["entity_id"] == str(entity.id)
    assert response.json()["match_method"] == "exact_name"


async def test_ambiguous_fuzzy_resolution_creates_tenant_scoped_review_case(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = scope(session, "ambiguous")
    first = add_entity(session, organization, workspace, "Acme Corporation")
    second = add_entity(session, organization, workspace, "Acme Corp Europe")
    _, _, other_headers = scope(session, "other")

    response = await client.post(
        "/api/v1/entity-resolution/resolve",
        headers=headers,
        json={"entity_type": "customer", "name": "Acme Corp"},
    )
    own_queue = await client.get("/api/v1/entity-resolution/cases", headers=headers)
    other_queue = await client.get("/api/v1/entity-resolution/cases", headers=other_headers)

    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "review_required"
    assert body["entity_id"] is None
    assert {item["entity_id"] for item in body["candidates"]} == {str(first.id), str(second.id)}
    assert len(own_queue.json()) == 1
    assert other_queue.json() == []


async def test_member_cannot_create_resolution_case(
    client: httpx.AsyncClient, session: Session
) -> None:
    _, _, headers = scope(session, "member", role="member")
    response = await client.post(
        "/api/v1/entity-resolution/resolve",
        headers=headers,
        json={"entity_type": "customer", "name": "Acme"},
    )
    assert response.status_code == 403


async def test_review_case_can_only_match_a_snapshotted_candidate_once(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = scope(session, "decision")
    candidate = add_entity(session, organization, workspace, "Acme Corporation")
    outsider = add_entity(session, organization, workspace, "Unrelated")
    created = await client.post(
        "/api/v1/entity-resolution/resolve",
        headers=headers,
        json={"entity_type": "customer", "name": "Acme Corp"},
    )
    case_id = created.json()["case_id"]

    rejected = await client.post(
        f"/api/v1/entity-resolution/cases/{case_id}/decision",
        headers=headers,
        json={"action": "match", "entity_id": str(outsider.id)},
    )
    accepted = await client.post(
        f"/api/v1/entity-resolution/cases/{case_id}/decision",
        headers=headers,
        json={"action": "match", "entity_id": str(candidate.id)},
    )
    repeated = await client.post(
        f"/api/v1/entity-resolution/cases/{case_id}/decision",
        headers=headers,
        json={"action": "dismiss"},
    )

    assert rejected.status_code == 409
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "resolved"
    assert accepted.json()["selected_entity_id"] == str(candidate.id)
    assert repeated.status_code == 409
    audits = session.query(EntityResolutionAudit).all()
    assert [(audit.action, audit.details["case_id"]) for audit in audits] == [("match", case_id)]


async def test_merge_and_split_are_relationship_safe_and_audited(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = scope(session, "merge")
    target = add_entity(session, organization, workspace, "Acme Corporation")
    source = add_entity(session, organization, workspace, "Acme Corp")
    other = add_entity(session, organization, workspace, "Invoice", kind="other")
    other.entity_type = EntityType.INVOICE
    origin = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="manual",
        uri="manual://merge",
    )
    session.add(origin)
    session.flush()
    external = ExternalReference(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_id=source.id,
        source_id=origin.id,
        source_system="manual",
        source_model="customer",
        external_id="source-1",
    )
    outbound = Relationship(
        organization_id=organization.id,
        workspace_id=workspace.id,
        from_entity_id=source.id,
        to_entity_id=other.id,
        relationship_type="CUSTOMER_HAS_INVOICE",
    )
    between = Relationship(
        organization_id=organization.id,
        workspace_id=workspace.id,
        from_entity_id=source.id,
        to_entity_id=target.id,
        relationship_type="POSSIBLE_DUPLICATE",
    )
    event = Event(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=source.id,
        event_type="CREATED",
    )
    memory = Memory(
        organization_id=organization.id,
        workspace_id=workspace.id,
        subject_entity_id=source.id,
        memory_type=MemoryType.BUSINESS,
        text="Source memory",
    )
    evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=origin.id,
        evidence_type="manual",
    )
    session.add_all([external, outbound, between, event, memory, evidence])
    session.flush()
    evidence_link = EvidenceLink(
        organization_id=organization.id,
        workspace_id=workspace.id,
        evidence_id=evidence.id,
        entity_id=source.id,
    )
    relationship_evidence_link = EvidenceLink(
        organization_id=organization.id,
        workspace_id=workspace.id,
        evidence_id=evidence.id,
        relationship_id=between.id,
    )
    session.add_all([evidence_link, relationship_evidence_link])
    session.commit()

    merged = await client.post(
        "/api/v1/entity-resolution/merge",
        headers=headers,
        json={"source_entity_id": str(source.id), "target_entity_id": str(target.id)},
    )
    assert merged.status_code == 200
    merge_id = merged.json()["merge_id"]
    session.expire_all()
    assert session.get(Entity, source.id).lifecycle_status == "merged"
    assert session.get(ExternalReference, external.id).entity_id == target.id
    assert session.get(Relationship, outbound.id).from_entity_id == target.id
    assert session.get(Relationship, between.id) is None
    assert session.get(EvidenceLink, relationship_evidence_link.id) is None
    assert session.get(Event, event.id).subject_entity_id == target.id
    assert session.get(Memory, memory.id).subject_entity_id == target.id
    assert session.get(EvidenceLink, evidence_link.id).entity_id == target.id
    target.aliases = [*target.aliases, "Added after merge"]
    session.commit()

    split = await client.post(
        f"/api/v1/entity-resolution/merges/{merge_id}/split",
        headers=headers,
    )
    assert split.status_code == 200
    session.expire_all()
    assert session.get(Entity, source.id).lifecycle_status == "active"
    assert session.get(ExternalReference, external.id).entity_id == source.id
    assert session.get(Relationship, outbound.id).from_entity_id == source.id
    restored_between = session.get(Relationship, between.id)
    assert restored_between is not None
    assert restored_between.from_entity_id == source.id
    assert restored_between.to_entity_id == target.id
    restored_relationship_link = session.get(EvidenceLink, relationship_evidence_link.id)
    assert restored_relationship_link is not None
    assert restored_relationship_link.relationship_id == between.id
    assert session.get(Event, event.id).subject_entity_id == source.id
    assert session.get(Memory, memory.id).subject_entity_id == source.id
    assert session.get(EvidenceLink, evidence_link.id).entity_id == source.id
    assert "Added after merge" in session.get(Entity, target.id).aliases
    actions = [audit.action for audit in session.query(EntityResolutionAudit).all()]
    assert actions == ["merge", "split"]


async def test_merge_rejects_cross_tenant_entity(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = scope(session, "merge-local")
    source = add_entity(session, organization, workspace, "Local")
    other_organization, other_workspace, _ = scope(session, "merge-other")
    target = add_entity(session, other_organization, other_workspace, "Other")

    response = await client.post(
        "/api/v1/entity-resolution/merge",
        headers=headers,
        json={"source_entity_id": str(source.id), "target_entity_id": str(target.id)},
    )

    assert response.status_code == 404


async def test_merge_rejects_duplicate_typed_relationship(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = scope(session, "merge-duplicate-edge")
    source = add_entity(session, organization, workspace, "Acme duplicate")
    target = add_entity(session, organization, workspace, "Acme canonical")
    other = add_entity(session, organization, workspace, "Partner")
    session.add_all(
        [
            Relationship(
                organization_id=organization.id,
                workspace_id=workspace.id,
                from_entity_id=source.id,
                to_entity_id=other.id,
                relationship_type="WORKS_WITH",
            ),
            Relationship(
                organization_id=organization.id,
                workspace_id=workspace.id,
                from_entity_id=target.id,
                to_entity_id=other.id,
                relationship_type="WORKS_WITH",
            ),
        ]
    )
    session.commit()

    response = await client.post(
        "/api/v1/entity-resolution/merge",
        headers=headers,
        json={"source_entity_id": str(source.id), "target_entity_id": str(target.id)},
    )

    assert response.status_code == 409
    session.expire_all()
    assert session.get(Entity, source.id).lifecycle_status == "active"


async def test_split_integrity_collision_returns_409_and_rolls_back(
    client: httpx.AsyncClient, session: Session
) -> None:
    organization, workspace, headers = scope(session, "split-collision")
    source = add_entity(session, organization, workspace, "Duplicate")
    target = add_entity(session, organization, workspace, "Canonical")
    relationship = Relationship(
        organization_id=organization.id,
        workspace_id=workspace.id,
        from_entity_id=source.id,
        to_entity_id=target.id,
        relationship_type="POSSIBLE_DUPLICATE",
    )
    session.add(relationship)
    session.commit()
    merged = await client.post(
        "/api/v1/entity-resolution/merge",
        headers=headers,
        json={"source_entity_id": str(source.id), "target_entity_id": str(target.id)},
    )
    merge_id = merged.json()["merge_id"]

    session.add(
        Relationship(
            organization_id=organization.id,
            workspace_id=workspace.id,
            from_entity_id=source.id,
            to_entity_id=target.id,
            relationship_type="POSSIBLE_DUPLICATE",
        )
    )
    session.commit()

    response = await client.post(
        f"/api/v1/entity-resolution/merges/{merge_id}/split",
        headers=headers,
    )

    assert response.status_code == 409
    session.expire_all()
    assert session.get(Entity, source.id).lifecycle_status == "merged"
    assert session.get(EntityMerge, UUID(merge_id)).status == "active"
