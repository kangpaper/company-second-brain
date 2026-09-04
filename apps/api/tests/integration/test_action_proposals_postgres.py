import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event, Lock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from company_brain.api.action_proposals import execute_action_proposal
from company_brain.api.dependencies import Principal
from company_brain.domain.models import (
    ActionAudit,
    ActionProposal,
    Membership,
    Organization,
    User,
    Workspace,
)
from company_brain.domain.repositories import TenantScope

pytestmark = pytest.mark.postgres


def postgres_engine():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return create_engine(database_url)


class BlockingConnector:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self._lock = Lock()
        self.calls: list[str] = []

    def execute(
        self,
        *,
        operation: str,
        target: dict[str, object],
        parameters: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        del operation, target, parameters
        with self._lock:
            self.calls.append(idempotency_key)
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test connector timed out")
        return {"remote_reference": "concurrent-safe"}


def seed_team(session: Session) -> tuple[Organization, Workspace, dict[str, User]]:
    suffix = uuid4().hex
    organization = Organization(name="Action DB", slug=f"action-db-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug=f"main-{suffix}",
    )
    session.add(workspace)
    session.flush()
    users: dict[str, User] = {}
    for role in ("editor", "admin", "owner", "member"):
        user = User(
            organization_id=organization.id,
            email=f"{suffix}-{role}@example.com",
            display_name=role,
        )
        session.add(user)
        session.flush()
        session.add(
            Membership(
                organization_id=organization.id,
                workspace_id=workspace.id,
                user_id=user.id,
                role=role,
            )
        )
        users[role] = user
    session.commit()
    return organization, workspace, users


def pending_proposal(
    organization: Organization,
    workspace: Workspace,
    requester: User,
    *,
    operation: str = "update_record",
) -> ActionProposal:
    return ActionProposal(
        organization_id=organization.id,
        workspace_id=workspace.id,
        requested_by_user_id=requester.id,
        connector="odoo",
        operation=operation,
        target={"model": "res.partner", "record_id": "42"},
        parameters=(
            {"values": {"email": "new@example.com"}}
            if operation == "update_record"
            else {"values": {}}
        ),
        reason="Verified correction",
        risk_level="elevated" if operation == "delete_record" else "standard",
        status="pending",
    )


def proposed_audit(proposal: ActionProposal, actor: User) -> ActionAudit:
    return ActionAudit(
        organization_id=proposal.organization_id,
        workspace_id=proposal.workspace_id,
        proposal_id=proposal.id,
        actor_user_id=actor.id,
        event_type="proposed",
        outcome="succeeded",
        metadata_={"connector": "odoo"},
    )


def test_action_state_machine_and_audits_survive_service_bypass() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, users = seed_team(session)
        admin_id = users["admin"].id
        owner_id = users["owner"].id
        proposal = pending_proposal(organization, workspace, users["editor"])
        session.add(proposal)
        session.flush()
        session.add(proposed_audit(proposal, users["editor"]))
        session.commit()

        proposal.status = "approved"
        proposal.approved_by_user_id = admin_id
        proposal.approved_at = datetime.now(UTC)
        session.flush()
        session.add(
            ActionAudit(
                organization_id=organization.id,
                workspace_id=workspace.id,
                proposal_id=proposal.id,
                actor_user_id=admin_id,
                event_type="approved",
                outcome="succeeded",
                metadata_={},
            )
        )
        session.commit()

        proposal.status = "executed"
        proposal.executed_by_user_id = owner_id
        proposal.executed_at = datetime.now(UTC)
        session.flush()
        audit = ActionAudit(
            organization_id=organization.id,
            workspace_id=workspace.id,
            proposal_id=proposal.id,
            actor_user_id=owner_id,
            event_type="execution_succeeded",
            outcome="succeeded",
            metadata_={},
        )
        session.add(audit)
        session.commit()

        proposal.reason = "tampered"
        with pytest.raises(DBAPIError, match="immutable"):
            session.flush()
        session.rollback()

        persisted_audit = session.get(ActionAudit, audit.id)
        assert persisted_audit is not None
        persisted_audit.metadata_ = {"tampered": True}
        with pytest.raises(DBAPIError, match="append-only"):
            session.flush()
        session.rollback()

        persisted_audit = session.get(ActionAudit, audit.id)
        assert persisted_audit is not None
        session.delete(persisted_audit)
        with pytest.raises(DBAPIError, match="append-only"):
            session.flush()
        session.rollback()
    engine.dispose()


def test_action_permissions_and_transition_shape_are_database_enforced() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, users = seed_team(session)
        editor_id = users["editor"].id
        admin_id = users["admin"].id
        owner_id = users["owner"].id

        member_proposal = pending_proposal(organization, workspace, users["member"])
        session.add(member_proposal)
        with pytest.raises(DBAPIError, match="lacks write access"):
            session.flush()
        session.rollback()

        proposal = pending_proposal(organization, workspace, users["editor"])
        session.add(proposal)
        session.flush()
        session.add(proposed_audit(proposal, users["editor"]))
        session.commit()
        proposal.status = "approved"
        proposal.approved_by_user_id = editor_id
        proposal.approved_at = datetime.now(UTC)
        with pytest.raises(DBAPIError, match="invalid action proposal approval"):
            session.flush()
        session.rollback()

        delete_proposal = pending_proposal(
            organization, workspace, users["editor"], operation="delete_record"
        )
        session.add(delete_proposal)
        session.flush()
        session.add(proposed_audit(delete_proposal, users["editor"]))
        session.commit()
        delete_proposal.status = "approved"
        delete_proposal.approved_by_user_id = admin_id
        delete_proposal.approved_at = datetime.now(UTC)
        with pytest.raises(DBAPIError, match="approver lacks permission"):
            session.flush()
        session.rollback()

        illegal = pending_proposal(organization, workspace, users["editor"])
        session.add(illegal)
        session.flush()
        session.add(proposed_audit(illegal, users["editor"]))
        session.commit()
        illegal.status = "executed"
        illegal.executed_by_user_id = owner_id
        illegal.executed_at = datetime.now(UTC)
        with pytest.raises(DBAPIError, match="invalid action proposal state transition"):
            session.flush()
        session.rollback()
    engine.dispose()


def test_concurrent_execution_calls_connector_once_with_proposal_idempotency_key() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, users = seed_team(session)
        organization_id = organization.id
        workspace_id = workspace.id
        admin_id = users["admin"].id
        owner_id = users["owner"].id
        proposal = pending_proposal(organization, workspace, users["editor"])
        session.add(proposal)
        session.flush()
        session.add(proposed_audit(proposal, users["editor"]))
        session.commit()

        proposal.status = "approved"
        proposal.approved_by_user_id = admin_id
        proposal.approved_at = datetime.now(UTC)
        session.flush()
        session.add(
            ActionAudit(
                organization_id=organization_id,
                workspace_id=workspace_id,
                proposal_id=proposal.id,
                actor_user_id=admin_id,
                event_type="approved",
                outcome="succeeded",
                metadata_={},
            )
        )
        session.commit()
        proposal_id = proposal.id

    connector = BlockingConnector()
    principal = Principal(
        user_id=owner_id,
        role="owner",
        scope=TenantScope(
            organization_id=organization_id,
            workspace_id=workspace_id,
        ),
    )
    second_started = Event()

    def execute(started: Event | None = None) -> str | int:
        if started is not None:
            started.set()
        with Session(engine) as session:
            try:
                result = execute_action_proposal(
                    proposal_id,
                    session,
                    principal,
                    connector,
                )
            except HTTPException as error:
                return error.status_code
            return result.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute)
        assert connector.entered.wait(timeout=5)
        second = pool.submit(execute, second_started)
        assert second_started.wait(timeout=5)
        connector.release.set()
        outcomes = {first.result(timeout=5), second.result(timeout=5)}

    assert outcomes == {"executed", 409}
    assert connector.calls == [str(proposal_id)]
    engine.dispose()


def test_action_transition_requires_matching_audit_before_commit() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, users = seed_team(session)
        admin_id = users["admin"].id
        proposal = pending_proposal(organization, workspace, users["editor"])
        session.add(proposal)
        session.flush()
        session.add(proposed_audit(proposal, users["editor"]))
        session.commit()

        proposal.status = "approved"
        proposal.approved_by_user_id = admin_id
        proposal.approved_at = datetime.now(UTC)
        with pytest.raises(DBAPIError, match="requires matching audit"):
            session.commit()
        session.rollback()
    engine.dispose()


def test_action_history_rejects_delete_and_truncate() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, users = seed_team(session)
        proposal = pending_proposal(organization, workspace, users["editor"])
        session.add(proposal)
        session.flush()
        session.add(proposed_audit(proposal, users["editor"]))
        session.commit()

        session.delete(proposal)
        with pytest.raises(DBAPIError, match="append-only"):
            session.flush()
        session.rollback()

        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(text("TRUNCATE action_audits, action_proposals"))
        session.rollback()
    engine.dispose()


def test_action_audit_rejects_cross_scope_proposal() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        first = seed_team(session)
        second = seed_team(session)
        proposal = pending_proposal(first[0], first[1], first[2]["editor"])
        session.add(proposal)
        session.flush()
        session.add(proposed_audit(proposal, first[2]["editor"]))
        session.commit()
        session.add(
            ActionAudit(
                organization_id=second[0].id,
                workspace_id=second[1].id,
                proposal_id=proposal.id,
                actor_user_id=second[2]["owner"].id,
                event_type="proposed",
                outcome="succeeded",
                metadata_={},
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            session.flush()
        session.rollback()
    engine.dispose()
