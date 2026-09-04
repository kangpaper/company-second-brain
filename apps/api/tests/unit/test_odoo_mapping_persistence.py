import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    Entity,
    ExternalReference,
    Organization,
    Source,
    Workspace,
)
from company_brain.domain.repositories import TenantScope
from company_brain.integrations.odoo.mapping import map_odoo_record
from company_brain.integrations.odoo.persistence import persist_odoo_mapping


@pytest.fixture
def seeded_scope(session: Session) -> TenantScope:
    organization = Organization(name="Mapping Org", slug="mapping-org")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id, name="Mapping", slug="mapping"
    )
    session.add(workspace)
    session.commit()
    return TenantScope(organization.id, workspace.id)


def test_mapping_persistence_creates_entity_source_and_external_reference(
    session: Session, seeded_scope: TenantScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    dto = map_odoo_record(
        "res.partner",
        {
            "id": 42,
            "name": "Acme",
            "is_company": True,
            "customer_rank": 2,
            "email": "hello@acme.example",
        },
    )

    def reject_commit() -> None:
        raise AssertionError("persistence helper must not own the transaction")

    monkeypatch.setattr(session, "commit", reject_commit)
    result = persist_odoo_mapping(
        session, seeded_scope, dto, "https://odoo-a.example.com/mcp"
    )

    assert result.created is True
    assert result.entity.name == "Acme"
    assert result.entity.metadata_ == dto.attributes
    source = session.scalar(select(Source))
    reference = session.scalar(select(ExternalReference))
    assert source is not None
    assert source.source_type == "odoo_instance"
    assert source.uri == "https://odoo-a.example.com/mcp"
    assert source.metadata_ == {"source_system": "odoo"}
    assert reference is not None
    assert reference.entity_id == result.entity.id
    assert reference.source_id == source.id
    assert reference.source_system == "odoo"
    assert reference.source_model == "res.partner"
    assert reference.external_id == "42"
    assert reference.raw_ref == {}



def test_mapping_persistence_is_idempotent_and_updates_allowlisted_fields(
    session: Session, seeded_scope: TenantScope
) -> None:
    first = map_odoo_record(
        "res.partner",
        {"id": 42, "name": "Acme", "is_company": True, "customer_rank": 1},
    )
    second = map_odoo_record(
        "res.partner",
        {
            "id": 42,
            "name": "Acme Renamed",
            "is_company": True,
            "customer_rank": 3,
        },
    )

    initial = persist_odoo_mapping(
        session, seeded_scope, first, "https://odoo-a.example.com/mcp"
    )
    updated = persist_odoo_mapping(
        session, seeded_scope, second, "https://odoo-a.example.com/mcp"
    )

    assert updated.created is False
    assert updated.entity.id == initial.entity.id
    assert updated.entity.name == "Acme Renamed"
    assert updated.entity.metadata_["customer_rank"] == 3
    assert session.scalar(select(func.count()).select_from(Entity)) == 1
    assert session.scalar(select(func.count()).select_from(Source)) == 1
    assert session.scalar(select(func.count()).select_from(ExternalReference)) == 1


def test_same_external_id_from_two_odoo_instances_creates_distinct_mappings(
    session: Session, seeded_scope: TenantScope
) -> None:
    dto = map_odoo_record(
        "res.partner", {"id": 42, "name": "Acme", "is_company": True}
    )

    first = persist_odoo_mapping(
        session, seeded_scope, dto, "https://odoo-a.example.com/mcp"
    )
    second = persist_odoo_mapping(
        session, seeded_scope, dto, "https://odoo-b.example.com/mcp"
    )

    assert first.entity.id != second.entity.id
    assert first.source.id != second.source.id
    assert session.scalar(select(func.count()).select_from(Entity)) == 2
    assert session.scalar(select(func.count()).select_from(Source)) == 2
    assert session.scalar(select(func.count()).select_from(ExternalReference)) == 2
