import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    IntegrationAudit,
    Organization,
    User,
    Workspace,
)

pytestmark = pytest.mark.postgres


def postgres_engine():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return create_engine(database_url)


def audit_fixture(session: Session) -> IntegrationAudit:
    organization = Organization(name="Audit", slug=f"audit-{uuid4()}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id, name="Main", slug="main", settings={}
    )
    user = User(
        organization_id=organization.id,
        email=f"audit-{uuid4()}@example.com",
        display_name="Auditor",
    )
    session.add_all([workspace, user])
    session.flush()
    audit = IntegrationAudit(
        organization_id=organization.id,
        workspace_id=workspace.id,
        actor_user_id=user.id,
        provider="odoo",
        endpoint="https://odoo.example.com/mcp",
        operation="search",
        tool_name="search_records",
        outcome="succeeded",
        request_metadata={"model": "res.partner"},
    )
    session.add(audit)
    session.flush()
    return audit


def test_integration_audit_is_database_append_only() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        audit = audit_fixture(session)
        audit_id = audit.id
        session.commit()

        persisted = session.get(IntegrationAudit, audit_id)
        assert persisted is not None
        persisted.outcome = "failed"
        persisted.error_code = "tampered"
        persisted.error_message = "tampered"
        with pytest.raises(DBAPIError, match="append-only"):
            session.flush()
        session.rollback()

        persisted = session.get(IntegrationAudit, audit_id)
        assert persisted is not None
        session.delete(persisted)
        with pytest.raises(DBAPIError, match="append-only"):
            session.flush()
        session.rollback()


def test_integration_audit_rejects_cross_organization_actor() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        audit = audit_fixture(session)
        other = Organization(name="Other", slug=f"other-audit-{uuid4()}")
        session.add(other)
        session.flush()
        other_user = User(
            organization_id=other.id,
            email=f"other-{uuid4()}@example.com",
            display_name="Other",
        )
        session.add(other_user)
        session.flush()
        session.add(
            IntegrationAudit(
                organization_id=audit.organization_id,
                workspace_id=audit.workspace_id,
                actor_user_id=other_user.id,
                provider="odoo",
                endpoint="https://odoo.example.com/mcp",
                operation="search",
                outcome="succeeded",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


@pytest.mark.parametrize(
    ("outcome", "error_code", "error_message"),
    [
        ("unknown", None, None),
        ("succeeded", "error", "failed"),
        ("failed", None, None),
        ("denied", "policy", None),
    ],
)
def test_integration_audit_enforces_outcome_state(
    outcome: str, error_code: str | None, error_message: str | None
) -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        valid = audit_fixture(session)
        session.add(
            IntegrationAudit(
                organization_id=valid.organization_id,
                workspace_id=valid.workspace_id,
                actor_user_id=valid.actor_user_id,
                provider="odoo",
                endpoint="https://odoo.example.com/mcp",
                operation="search",
                outcome=outcome,
                error_code=error_code,
                error_message=error_message,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_integration_audit_schema_has_no_credential_columns() -> None:
    inspector = inspect(postgres_engine())
    columns = {column["name"] for column in inspector.get_columns("integration_audits")}
    forbidden_fragments = {"credential", "secret", "password", "token", "api_key"}
    assert not any(fragment in column for fragment in forbidden_fragments for column in columns)


def test_integration_audit_can_be_tenant_filtered() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        first = audit_fixture(session)
        second = audit_fixture(session)
        session.commit()
        found = list(
            session.scalars(
                select(IntegrationAudit).where(
                    IntegrationAudit.organization_id == first.organization_id,
                    IntegrationAudit.workspace_id == first.workspace_id,
                )
            )
        )
        assert [audit.id for audit in found] == [first.id]
        assert second.id not in {audit.id for audit in found}
