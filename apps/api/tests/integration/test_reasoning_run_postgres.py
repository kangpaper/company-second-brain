import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    Entity,
    EntityType,
    Evidence,
    Membership,
    Organization,
    ReasoningRun,
    Source,
    User,
    Workspace,
)

pytestmark = pytest.mark.postgres


def postgres_engine():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return create_engine(database_url)


def reasoning_scope(session: Session) -> tuple[Organization, Workspace, User, Entity, Evidence]:
    organization = Organization(name="Reasoning", slug=f"reasoning-{uuid4().hex}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug=f"main-{uuid4().hex}",
        settings={},
    )
    user = User(
        organization_id=organization.id,
        email=f"reasoning-{uuid4().hex}@example.com",
        display_name="Reasoner",
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
    customer = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.CUSTOMER,
        name="ABC",
        normalized_name="abc",
    )
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="fixture",
        uri=f"fixture://{uuid4()}",
    )
    session.add_all([customer, source])
    session.flush()
    evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="field",
        pointer={"field": "amount_total"},
    )
    session.add(evidence)
    session.flush()
    return organization, workspace, user, customer, evidence


def successful_run(
    organization: Organization,
    workspace: Workspace,
    user: User,
    customer: Entity,
    citation_ids: object,
    **overrides: object,
) -> ReasoningRun:
    values: dict[str, object] = {
        "organization_id": organization.id,
        "workspace_id": workspace.id,
        "actor_user_id": user.id,
        "customer_id": customer.id,
        "context_hash": "a" * 64,
        "provider": "stub",
        "model": "stub-v1",
        "prompt_version": "grounded-v1",
        "status": "succeeded",
        "answer": "Grounded answer.",
        "citation_ids": citation_ids,
        "uncertainty": "Some records may be incomplete.",
        "error_code": None,
        "error_message": None,
    }
    values.update(overrides)
    return ReasoningRun(**values)


def test_reasoning_run_accepts_tenant_scoped_evidence_and_is_append_only() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, user, customer, evidence = reasoning_scope(session)
        run = successful_run(
            organization, workspace, user, customer, [str(evidence.id)]
        )
        session.add(run)
        session.commit()

        persisted = session.get(ReasoningRun, run.id)
        assert persisted is not None
        persisted.answer = "tampered"
        with pytest.raises(DBAPIError, match="append-only"):
            session.flush()
        session.rollback()

        persisted = session.get(ReasoningRun, run.id)
        assert persisted is not None
        session.delete(persisted)
        with pytest.raises(DBAPIError, match="append-only"):
            session.flush()
        session.rollback()
    engine.dispose()


@pytest.mark.parametrize(
    ("citations", "overrides"),
    [
        ({"not": "an array"}, {}),
        ([], {}),
        (["not-a-uuid"], {}),
        ([str(uuid4())], {}),
        (None, {}),
        ([], {"context_hash": "short"}),
        ([], {"provider": "   "}),
        ([], {"model": ""}),
        ([], {"prompt_version": " "}),
        ([], {"answer": " "}),
        ([], {"uncertainty": "\t"}),
    ],
)
def test_reasoning_run_rejects_invalid_success_audit_shape(
    citations: object, overrides: dict[str, object]
) -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, user, customer, _ = reasoning_scope(session)
        session.commit()
        session.add(
            successful_run(
                organization,
                workspace,
                user,
                customer,
                citations,
                **overrides,
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            session.flush()
        session.rollback()
    engine.dispose()


def test_reasoning_run_rejects_duplicate_and_cross_tenant_citations() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        first = reasoning_scope(session)
        second = reasoning_scope(session)
        session.commit()
        first_org, first_workspace, first_user, first_customer, first_evidence = first
        second_evidence = second[4]

        duplicate = successful_run(
            first_org,
            first_workspace,
            first_user,
            first_customer,
            [str(first_evidence.id), str(first_evidence.id)],
        )
        session.add(duplicate)
        with pytest.raises((IntegrityError, DBAPIError)):
            session.flush()
        session.rollback()

        cross_tenant = successful_run(
            first_org,
            first_workspace,
            first_user,
            first_customer,
            [str(second_evidence.id)],
        )
        session.add(cross_tenant)
        with pytest.raises((IntegrityError, DBAPIError)):
            session.flush()
        session.rollback()
    engine.dispose()


def test_failed_reasoning_run_requires_empty_citations_and_bounded_error() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, user, customer, evidence = reasoning_scope(session)
        session.commit()
        run = successful_run(
            organization,
            workspace,
            user,
            customer,
            [str(evidence.id)],
            status="failed",
            answer=None,
            uncertainty=None,
            error_code="provider_failure",
            error_message="AI provider failed",
        )
        session.add(run)
        with pytest.raises((IntegrityError, DBAPIError)):
            session.flush()
        session.rollback()
    engine.dispose()


def test_reasoning_run_blocks_truncate() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        with pytest.raises(DBAPIError):
            session.execute(text("TRUNCATE TABLE reasoning_runs"))
        session.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(
                text("TRUNCATE TABLE reasoning_run_citations, reasoning_runs")
            )
        session.rollback()
    engine.dispose()


def test_reasoning_run_rejects_actor_without_workspace_membership() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, _, customer, evidence = reasoning_scope(session)
        outsider = User(
            organization_id=organization.id,
            email=f"outsider-{uuid4().hex}@example.com",
            display_name="Outsider",
        )
        session.add(outsider)
        session.commit()
        session.add(
            successful_run(
                organization,
                workspace,
                outsider,
                customer,
                [str(evidence.id)],
            )
        )
        with pytest.raises((IntegrityError, DBAPIError), match="workspace membership"):
            session.flush()
        session.rollback()
    engine.dispose()


def test_reasoning_run_rejects_non_customer_entity() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, user, _, evidence = reasoning_scope(session)
        order = Entity(
            organization_id=organization.id,
            workspace_id=workspace.id,
            entity_type=EntityType.ORDER,
            name="Not a customer",
            normalized_name="not a customer",
        )
        session.add(order)
        session.commit()
        session.add(
            successful_run(
                organization,
                workspace,
                user,
                order,
                [str(evidence.id)],
            )
        )
        with pytest.raises((IntegrityError, DBAPIError), match="active customer"):
            session.flush()
        session.rollback()
    engine.dispose()


def test_reasoning_run_citations_keep_evidence_referentially_intact() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, user, customer, evidence = reasoning_scope(session)
        run = successful_run(
            organization, workspace, user, customer, [str(evidence.id)]
        )
        session.add(run)
        session.commit()

        persisted_evidence = session.get(Evidence, evidence.id)
        assert persisted_evidence is not None
        session.delete(persisted_evidence)
        with pytest.raises((IntegrityError, DBAPIError)):
            session.flush()
        session.rollback()

        assert session.get(ReasoningRun, run.id) is not None
        association_count = session.scalar(
            text(
                "SELECT count(*) FROM reasoning_run_citations "
                "WHERE reasoning_run_id = :run_id AND evidence_id = :evidence_id"
            ),
            {"run_id": run.id, "evidence_id": evidence.id},
        )
        assert association_count == 1

        with pytest.raises(DBAPIError, match="does not match audit JSON"):
            session.execute(
                text(
                    "INSERT INTO reasoning_run_citations "
                    "(organization_id, workspace_id, reasoning_run_id, evidence_id, ordinal) "
                    "VALUES (:organization_id, :workspace_id, :run_id, :evidence_id, 2)"
                ),
                {
                    "organization_id": organization.id,
                    "workspace_id": workspace.id,
                    "run_id": run.id,
                    "evidence_id": evidence.id,
                },
            )
        session.rollback()

        for statement in (
            "UPDATE reasoning_run_citations SET ordinal = ordinal "
            "WHERE reasoning_run_id = :run_id",
            "DELETE FROM reasoning_run_citations WHERE reasoning_run_id = :run_id",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                session.execute(text(statement), {"run_id": run.id})
            session.rollback()
    engine.dispose()
