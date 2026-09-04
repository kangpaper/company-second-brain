from collections.abc import AsyncIterator
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from company_brain.api.action_proposals import get_action_connector
from company_brain.db.session import get_session
from company_brain.domain.models import (
    ActionAudit,
    ActionProposal,
    Membership,
    Organization,
    User,
    Workspace,
)
from company_brain.main import app


class StubActionConnector:
    def __init__(self) -> None:
        self.calls: list[
            tuple[str, dict[str, object], dict[str, object], str]
        ] = []

    def execute(
        self,
        *,
        operation: str,
        target: dict[str, object],
        parameters: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        self.calls.append((operation, target, parameters, idempotency_key))
        return {"remote_reference": "safe-ref-42"}


class ExplodingActionConnector:
    def execute(
        self,
        *,
        operation: str,
        target: dict[str, object],
        parameters: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        del operation, target, parameters, idempotency_key
        raise RuntimeError("connector-secret-details")


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


def seed_actor(
    session: Session,
    *,
    role: str,
    suffix: str,
) -> dict[str, str]:
    organization = Organization(name=f"Actions {suffix}", slug=f"actions-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug=f"main-{suffix}",
    )
    token = f"action-token-{suffix}"
    user = User(
        organization_id=organization.id,
        email=f"actions-{suffix}@example.com",
        display_name=f"Actions {suffix}",
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


def seed_team(
    session: Session,
    *,
    suffix: str,
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    organization = Organization(name=f"Action Team {suffix}", slug=f"action-team-{suffix}")
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
    user_ids: dict[str, str] = {}
    for role in ("editor", "admin", "owner", "member"):
        token = f"action-team-{suffix}-{role}"
        user = User(
            organization_id=organization.id,
            email=f"{suffix}-{role}@example.com",
            display_name=f"{role.title()} {suffix}",
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
        user_ids[role] = str(user.id)
    session.commit()
    return headers, user_ids


def update_payload() -> dict[str, object]:
    return {
        "connector": "odoo",
        "operation": "update_record",
        "target": {"model": "res.partner", "record_id": "42"},
        "parameters": {"values": {"email": "finance@example.com"}},
        "reason": "Correct the billing contact after customer confirmation.",
    }


@pytest.mark.anyio
async def test_write_request_creates_pending_proposal_only(
    client: httpx.AsyncClient,
    session: Session,
) -> None:
    headers = seed_actor(session, role="editor", suffix=uuid4().hex)

    response = await client.post(
        "/api/v1/action-proposals",
        headers=headers,
        json=update_payload(),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["connector"] == "odoo"
    assert body["operation"] == "update_record"
    assert body["status"] == "pending"
    assert body["risk_level"] == "standard"
    assert body["approved_by_user_id"] is None
    assert body["approved_at"] is None
    assert body["executed_by_user_id"] is None
    assert body["executed_at"] is None


@pytest.mark.anyio
async def test_read_only_member_cannot_create_action_proposal(
    client: httpx.AsyncClient,
    session: Session,
) -> None:
    headers = seed_actor(session, role="member", suffix=uuid4().hex)

    response = await client.post(
        "/api/v1/action-proposals",
        headers=headers,
        json=update_payload(),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Write access denied"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "credential_field",
    ["apiKey", "api_key", "api.key", "access_token", "clientSecret", "authorization"],
)
async def test_credential_like_parameter_fields_are_rejected_before_persistence(
    client: httpx.AsyncClient,
    session: Session,
    credential_field: str,
) -> None:
    headers = seed_actor(session, role="editor", suffix=uuid4().hex)
    payload = update_payload()
    payload["parameters"] = {"values": {credential_field: "must-not-persist"}}

    response = await client.post(
        "/api/v1/action-proposals",
        headers=headers,
        json=payload,
    )

    assert response.status_code == 422
    assert (
        session.scalar(
            select(ActionProposal).where(ActionProposal.reason == payload["reason"])
        )
        is None
    )


@pytest.mark.anyio
async def test_standard_proposal_requires_distinct_admin_approval(
    client: httpx.AsyncClient,
    session: Session,
) -> None:
    headers, user_ids = seed_team(session, suffix=uuid4().hex)
    created = await client.post(
        "/api/v1/action-proposals", headers=headers["editor"], json=update_payload()
    )
    proposal_id = created.json()["id"]

    self_approval = await client.post(
        f"/api/v1/action-proposals/{proposal_id}/approve",
        headers=headers["editor"],
    )
    assert self_approval.status_code == 403
    assert self_approval.json() == {
        "detail": "A proposal requires approval by a different user"
    }

    member_approval = await client.post(
        f"/api/v1/action-proposals/{proposal_id}/approve",
        headers=headers["member"],
    )
    assert member_approval.status_code == 403
    assert member_approval.json() == {"detail": "Approval access denied"}

    approved = await client.post(
        f"/api/v1/action-proposals/{proposal_id}/approve",
        headers=headers["admin"],
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["approved_by_user_id"] == user_ids["admin"]
    assert approved.json()["approved_at"] is not None


@pytest.mark.anyio
async def test_delete_proposal_requires_distinct_owner_approval(
    client: httpx.AsyncClient,
    session: Session,
) -> None:
    headers, user_ids = seed_team(session, suffix=uuid4().hex)
    payload = update_payload()
    payload["operation"] = "delete_record"
    payload["parameters"] = {"values": {}}
    created = await client.post(
        "/api/v1/action-proposals", headers=headers["editor"], json=payload
    )
    assert created.status_code == 201
    assert created.json()["risk_level"] == "elevated"
    proposal_id = created.json()["id"]

    admin_approval = await client.post(
        f"/api/v1/action-proposals/{proposal_id}/approve",
        headers=headers["admin"],
    )
    assert admin_approval.status_code == 403
    assert admin_approval.json() == {"detail": "Delete proposals require owner approval"}

    approved = await client.post(
        f"/api/v1/action-proposals/{proposal_id}/approve",
        headers=headers["owner"],
    )
    assert approved.status_code == 200
    assert approved.json()["approved_by_user_id"] == user_ids["owner"]


@pytest.mark.anyio
async def test_pending_execution_and_cross_tenant_approval_fail_closed(
    client: httpx.AsyncClient,
    session: Session,
) -> None:
    headers, _ = seed_team(session, suffix=uuid4().hex)
    other_headers, _ = seed_team(session, suffix=uuid4().hex)
    created = await client.post(
        "/api/v1/action-proposals", headers=headers["editor"], json=update_payload()
    )
    proposal_id = created.json()["id"]

    pending_execution = await client.post(
        f"/api/v1/action-proposals/{proposal_id}/execute",
        headers=headers["owner"],
    )
    assert pending_execution.status_code == 409
    assert pending_execution.json() == {"detail": "Proposal is not approved"}

    cross_tenant = await client.post(
        f"/api/v1/action-proposals/{proposal_id}/approve",
        headers=other_headers["owner"],
    )
    assert cross_tenant.status_code == 404
    assert cross_tenant.json() == {"detail": "Action proposal not found"}


@pytest.mark.anyio
async def test_approved_proposal_executes_once_through_test_double_and_is_audited(
    client: httpx.AsyncClient,
    session: Session,
) -> None:
    headers, user_ids = seed_team(session, suffix=uuid4().hex)
    connector = StubActionConnector()
    app.dependency_overrides[get_action_connector] = lambda: connector
    created = await client.post(
        "/api/v1/action-proposals", headers=headers["editor"], json=update_payload()
    )
    proposal_id = created.json()["id"]
    approved = await client.post(
        f"/api/v1/action-proposals/{proposal_id}/approve",
        headers=headers["admin"],
    )
    assert approved.status_code == 200

    executed = await client.post(
        f"/api/v1/action-proposals/{proposal_id}/execute",
        headers=headers["owner"],
    )

    assert executed.status_code == 200
    assert executed.json()["status"] == "executed"
    assert executed.json()["executed_by_user_id"] == user_ids["owner"]
    assert executed.json()["executed_at"] is not None
    assert connector.calls == [
        (
            "update_record",
            {"model": "res.partner", "record_id": "42"},
            {"values": {"email": "finance@example.com"}},
            proposal_id,
        )
    ]
    repeated = await client.post(
        f"/api/v1/action-proposals/{proposal_id}/execute",
        headers=headers["owner"],
    )
    assert repeated.status_code == 409
    assert len(connector.calls) == 1

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
    assert str(audits[-1].actor_user_id) == user_ids["owner"]
    assert audits[-1].outcome == "succeeded"
    assert audits[-1].error_code is None


@pytest.mark.anyio
async def test_connector_failure_is_sanitized_and_audited(
    client: httpx.AsyncClient,
    session: Session,
) -> None:
    headers, _ = seed_team(session, suffix=uuid4().hex)
    app.dependency_overrides[get_action_connector] = lambda: ExplodingActionConnector()
    created = await client.post(
        "/api/v1/action-proposals", headers=headers["editor"], json=update_payload()
    )
    proposal_id = created.json()["id"]
    await client.post(
        f"/api/v1/action-proposals/{proposal_id}/approve",
        headers=headers["admin"],
    )

    response = await client.post(
        f"/api/v1/action-proposals/{proposal_id}/execute",
        headers=headers["owner"],
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Action connector execution failed"}
    proposal = session.get(ActionProposal, UUID(proposal_id))
    assert proposal is not None
    assert proposal.status == "failed"
    audit = session.scalar(
        select(ActionAudit).where(
            ActionAudit.proposal_id == UUID(proposal_id),
            ActionAudit.event_type == "execution_failed",
        )
    )
    assert audit is not None
    assert audit.outcome == "failed"
    assert audit.error_code == "connector_error"
    assert audit.error_message == "Action connector execution failed"
    assert "connector-secret-details" not in str(audit.metadata_)
    assert "connector-secret-details" not in audit.error_message
