import os
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    ActionAudit,
    ActionProposal,
    Membership,
    Organization,
    User,
    Workspace,
)


def _seed_actor(session: Session, role: str, suffix: str) -> dict[str, str]:
    organization = Organization(name=f"Phase 12 {suffix}", slug=f"phase12-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug=f"main-{suffix}",
    )
    token = f"phase12-{suffix}-{role}"
    user = User(
        organization_id=organization.id,
        email=f"{suffix}-{role}@example.com",
        display_name=f"Phase 12 {role}",
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
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": str(organization.id),
        "X-Workspace-ID": str(workspace.id),
    }


def _seed_team(session: Session, suffix: str) -> dict[str, dict[str, str]]:
    organization = Organization(name=f"Phase 12 Team {suffix}", slug=f"p12-team-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug=f"main-{suffix}",
    )
    session.add(workspace)
    session.flush()
    headers: dict[str, dict[str, str]] = {}
    for role in ("editor", "admin", "owner"):
        token = f"phase12-team-{suffix}-{role}"
        user = User(
            organization_id=organization.id,
            email=f"{suffix}-{role}@example.com",
            display_name=f"Phase 12 {role}",
            api_token_hash=sha256(token.encode()).hexdigest(),
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
        headers[role] = {
            "Authorization": f"Bearer {token}",
            "X-Organization-ID": str(organization.id),
            "X-Workspace-ID": str(workspace.id),
        }
    session.commit()
    return headers


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    base_url = os.environ.get("PHASE12_API_URL")
    if not database_url or not base_url:
        raise RuntimeError("DATABASE_URL and PHASE12_API_URL are required")
    engine = create_engine(database_url)
    suffix = uuid4().hex
    with Session(engine) as session:
        headers = _seed_team(session, suffix)
        other_owner = _seed_actor(session, "owner", f"other-{suffix}")

    payload = {
        "connector": "odoo",
        "operation": "update_record",
        "target": {"model": "res.partner", "record_id": "runtime-42"},
        "parameters": {"values": {"email": "runtime@example.com"}},
        "reason": "Verified Phase 12 runtime correction",
    }
    with httpx.Client(base_url=base_url, timeout=10) as client:
        created = client.post("/api/v1/action-proposals", headers=headers["editor"], json=payload)
        created.raise_for_status()
        proposal_id = created.json()["id"]
        assert created.status_code == 201
        assert created.json()["status"] == "pending"

        cross_scope = client.post(
            f"/api/v1/action-proposals/{proposal_id}/approve",
            headers=other_owner,
        )
        assert cross_scope.status_code == 404
        assert cross_scope.json() == {"detail": "Action proposal not found"}

        approved = client.post(
            f"/api/v1/action-proposals/{proposal_id}/approve",
            headers=headers["admin"],
        )
        approved.raise_for_status()
        assert approved.json()["status"] == "approved"

        executed = client.post(
            f"/api/v1/action-proposals/{proposal_id}/execute",
            headers=headers["owner"],
        )
        executed.raise_for_status()
        assert executed.json()["status"] == "executed"

        repeated = client.post(
            f"/api/v1/action-proposals/{proposal_id}/execute",
            headers=headers["owner"],
        )
        assert repeated.status_code == 409

    with Session(engine) as session:
        proposal = session.get(ActionProposal, UUID(proposal_id))
        assert proposal is not None and proposal.status == "executed"
        audits = list(
            session.scalars(
                select(ActionAudit)
                .where(ActionAudit.proposal_id == UUID(proposal_id))
                .order_by(ActionAudit.created_at, ActionAudit.id)
            )
        )
        assert [audit.event_type for audit in audits] == [
            "proposed",
            "approved",
            "execution_succeeded",
        ]
        audits[-1].error_message = "tamper"
        try:
            session.commit()
        except DBAPIError:
            session.rollback()
        else:
            raise AssertionError("action audit update was not rejected")

    engine.dispose()
    print("phase12_tcp_proposal_approval_execution=passed")
    print("phase12_tcp_cross_scope_404=passed")
    print("phase12_tcp_retry_idempotency=passed")
    print("phase12_tcp_append_only_audit=passed")


if __name__ == "__main__":
    main()
