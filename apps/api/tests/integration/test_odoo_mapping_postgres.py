import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    Entity,
    EntityType,
    ExternalReference,
    Organization,
    Source,
    Workspace,
)
from company_brain.domain.repositories import TenantScope
from company_brain.integrations.odoo.mapping import map_odoo_record
from company_brain.integrations.odoo.persistence import persist_odoo_mapping

pytestmark = pytest.mark.postgres


def make_tenant(session: Session, suffix: str) -> tuple[Organization, Workspace]:
    organization = Organization(name=f"Mapping {suffix}", slug=f"mapping-{suffix}-{uuid4().hex}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name=f"Mapping {suffix}",
        slug=f"mapping-{suffix}-{uuid4().hex}",
    )
    session.add(workspace)
    session.flush()
    return organization, workspace


def test_external_reference_rejects_cross_tenant_source() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    engine = create_engine(database_url)
    with Session(engine) as session:
        org_a, workspace_a = make_tenant(session, "a")
        org_b, workspace_b = make_tenant(session, "b")
        entity = Entity(
            organization_id=org_a.id,
            workspace_id=workspace_a.id,
            entity_type=EntityType.CUSTOMER,
            name="Acme",
            normalized_name="acme",
        )
        source_b = Source(
            organization_id=org_b.id,
            workspace_id=workspace_b.id,
            source_type="odoo_record",
            uri="odoo://res.partner/42",
        )
        session.add_all([entity, source_b])
        session.flush()
        session.add(
            ExternalReference(
                organization_id=org_a.id,
                workspace_id=workspace_a.id,
                entity_id=entity.id,
                source_id=source_b.id,
                source_system="odoo",
                source_model="res.partner",
                external_id="42",
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()
    engine.dispose()


def test_same_odoo_record_can_be_mapped_in_two_workspaces() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    engine = create_engine(database_url)
    with Session(engine) as session:
        organization = Organization(
            name="Shared Organization", slug=f"shared-{uuid4().hex}"
        )
        session.add(organization)
        session.flush()
        workspace_a = Workspace(
            organization_id=organization.id, name="A", slug=f"a-{uuid4().hex}"
        )
        workspace_b = Workspace(
            organization_id=organization.id, name="B", slug=f"b-{uuid4().hex}"
        )
        session.add_all([workspace_a, workspace_b])
        session.flush()
        for workspace in (workspace_a, workspace_b):
            entity = Entity(
                organization_id=organization.id,
                workspace_id=workspace.id,
                entity_type=EntityType.CUSTOMER,
                name="Acme",
                normalized_name="acme",
            )
            source = Source(
                organization_id=organization.id,
                workspace_id=workspace.id,
                source_type="odoo_record",
                uri="odoo://res.partner/42",
            )
            session.add_all([entity, source])
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
        session.flush()
        session.rollback()
    engine.dispose()


def test_concurrent_mapping_reconciles_to_one_source_entity_and_reference() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    engine = create_engine(database_url)
    with Session(engine) as setup:
        organization, workspace = make_tenant(setup, "concurrent")
        setup.commit()
        scope = TenantScope(organization.id, workspace.id)
    dto = map_odoo_record(
        "res.partner",
        {"id": 42, "name": "Concurrent Acme", "is_company": True},
    )

    def persist() -> str:
        with Session(engine) as session:
            result = persist_odoo_mapping(
                session, scope, dto, "https://odoo.example.com/mcp"
            )
            session.commit()
            return str(result.entity.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        entity_ids = list(executor.map(lambda _: persist(), range(2)))

    with Session(engine) as verify:
        source_count = verify.query(Source).filter_by(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            source_type="odoo_instance",
        ).count()
        entity_count = verify.query(Entity).filter_by(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
        ).count()
        reference_count = verify.query(ExternalReference).filter_by(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
        ).count()
        assert len(set(entity_ids)) == 1
        assert source_count == 1
        assert entity_count == 1
        assert reference_count == 1
    engine.dispose()


def test_duplicate_odoo_instance_source_is_rejected_by_database() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    engine = create_engine(database_url)
    with Session(engine) as session:
        organization, workspace = make_tenant(session, "source-unique")
        source_values = {
            "organization_id": organization.id,
            "workspace_id": workspace.id,
            "source_type": "odoo_instance",
            "uri": "https://odoo.example.com/mcp",
        }
        session.add(Source(**source_values))
        session.flush()
        session.add(Source(**source_values))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    engine.dispose()
