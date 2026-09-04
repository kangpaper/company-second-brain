import json
import re
from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from company_brain.api.dependencies import Principal, get_principal, require_writer
from company_brain.db.session import get_session
from company_brain.domain.models import ActionAudit, ActionProposal, utc_now

router = APIRouter(prefix="/api/v1/action-proposals", tags=["action-proposals"])
SessionDependency = Annotated[Session, Depends(get_session)]
PrincipalDependency = Annotated[Principal, Depends(get_principal)]
WriterDependency = Annotated[Principal, Depends(require_writer)]
MODEL_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.]{0,99}$"
RECORD_ID_PATTERN = r"^[^\x00-\x1f\x7f]{1,255}$"
FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,99}$")
FIELD_WORD_PATTERN = re.compile(r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
SENSITIVE_FIELD_PARTS = frozenset(
    {"authorization", "credential", "password", "secret", "token"}
)
Scalar = str | int | float | bool | None


class ActionConnector(Protocol):
    def execute(
        self,
        *,
        operation: str,
        target: dict[str, object],
        parameters: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]: ...


class DisabledActionConnector:
    def execute(
        self,
        *,
        operation: str,
        target: dict[str, object],
        parameters: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        del operation, target, parameters, idempotency_key
        raise RuntimeError("action connector disabled")


def get_action_connector() -> ActionConnector:
    return DisabledActionConnector()


ConnectorDependency = Annotated[ActionConnector, Depends(get_action_connector)]


class ActionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(pattern=MODEL_PATTERN)
    record_id: str = Field(pattern=RECORD_ID_PATTERN)


class ActionParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    values: dict[str, Scalar] = Field(default_factory=dict, max_length=50)

    @field_validator("values")
    @classmethod
    def validate_values(cls, values: dict[str, Scalar]) -> dict[str, Scalar]:
        for field_name, value in values.items():
            if FIELD_PATTERN.fullmatch(field_name) is None:
                raise ValueError("invalid field name")
            words = [
                match.group(0).casefold()
                for segment in re.split(r"[._]", field_name)
                for match in FIELD_WORD_PATTERN.finditer(segment)
            ]
            if set(words) & SENSITIVE_FIELD_PARTS or any(
                left == "api" and right == "key"
                for left, right in zip(words, words[1:], strict=False)
            ):
                raise ValueError("credential-like fields are not allowed")
            if isinstance(value, str) and (len(value) > 4096 or "\x00" in value):
                raise ValueError("field value is invalid")
        try:
            encoded = json.dumps(values, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ValueError("values must be strict JSON") from error
        if len(encoded.encode()) > 16 * 1024:
            raise ValueError("values are too large")
        return values


class ActionProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connector: Literal["odoo"]
    operation: Literal["update_record", "delete_record"]
    target: ActionTarget
    parameters: ActionParameters
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("reason is invalid")
        return value.strip()

    @model_validator(mode="after")
    def validate_operation_shape(self) -> "ActionProposalCreate":
        if self.operation == "update_record" and not self.parameters.values:
            raise ValueError("update_record requires at least one value")
        if self.operation == "delete_record" and self.parameters.values:
            raise ValueError("delete_record does not accept values")
        return self


class ActionProposalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    connector: str
    operation: str
    target: dict[str, object]
    parameters: dict[str, object]
    reason: str
    risk_level: str
    status: str
    requested_by_user_id: UUID
    approved_by_user_id: UUID | None
    approved_at: datetime | None
    executed_by_user_id: UUID | None
    executed_at: datetime | None


def _add_audit(
    session: Session,
    proposal: ActionProposal,
    principal: Principal,
    event_type: str,
    outcome: str = "succeeded",
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    session.add(
        ActionAudit(
            organization_id=proposal.organization_id,
            workspace_id=proposal.workspace_id,
            proposal_id=proposal.id,
            actor_user_id=principal.user_id,
            event_type=event_type,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
            metadata_={
                "connector": proposal.connector,
                "operation": proposal.operation,
                "risk_level": proposal.risk_level,
                "target": proposal.target,
            },
        )
    )


@router.post("", response_model=ActionProposalResponse, status_code=status.HTTP_201_CREATED)
def create_action_proposal(
    payload: ActionProposalCreate,
    session: SessionDependency,
    principal: WriterDependency,
) -> ActionProposal:
    proposal = ActionProposal(
        organization_id=principal.scope.organization_id,
        workspace_id=principal.scope.workspace_id,
        requested_by_user_id=principal.user_id,
        connector=payload.connector,
        operation=payload.operation,
        target=payload.target.model_dump(mode="json"),
        parameters=payload.parameters.model_dump(mode="json"),
        reason=payload.reason,
        risk_level="elevated" if payload.operation == "delete_record" else "standard",
        status="pending",
    )
    session.add(proposal)
    session.flush()
    _add_audit(session, proposal, principal, "proposed")
    session.commit()
    session.refresh(proposal)
    return proposal


def _locked_proposal(
    session: Session,
    proposal_id: UUID,
    principal: Principal,
) -> ActionProposal:
    proposal = session.scalar(
        select(ActionProposal)
        .where(
            ActionProposal.id == proposal_id,
            ActionProposal.organization_id == principal.scope.organization_id,
            ActionProposal.workspace_id == principal.scope.workspace_id,
        )
        .with_for_update()
    )
    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Action proposal not found",
        )
    return proposal


@router.post("/{proposal_id}/approve", response_model=ActionProposalResponse)
def approve_action_proposal(
    proposal_id: UUID,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ActionProposal:
    proposal = _locked_proposal(session, proposal_id, principal)
    if proposal.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Proposal is not pending",
        )
    if proposal.requested_by_user_id == principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A proposal requires approval by a different user",
        )
    if principal.role not in {"owner", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Approval access denied",
        )
    if proposal.operation == "delete_record" and principal.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Delete proposals require owner approval",
        )
    proposal.status = "approved"
    proposal.approved_by_user_id = principal.user_id
    proposal.approved_at = utc_now()
    session.flush()
    _add_audit(session, proposal, principal, "approved")
    session.commit()
    session.refresh(proposal)
    return proposal


@router.post("/{proposal_id}/execute", response_model=ActionProposalResponse)
def execute_action_proposal(
    proposal_id: UUID,
    session: SessionDependency,
    principal: PrincipalDependency,
    connector: ConnectorDependency,
) -> ActionProposal:
    proposal = _locked_proposal(session, proposal_id, principal)
    if proposal.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Proposal is not approved",
        )
    if principal.role not in {"owner", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Execution access denied",
        )
    try:
        connector.execute(
            operation=proposal.operation,
            target=dict(proposal.target),
            parameters=dict(proposal.parameters),
            idempotency_key=str(proposal.id),
        )
    except Exception as error:
        proposal.status = "failed"
        proposal.executed_by_user_id = principal.user_id
        proposal.executed_at = utc_now()
        session.flush()
        _add_audit(
            session,
            proposal,
            principal,
            "execution_failed",
            "failed",
            error_code="connector_error",
            error_message="Action connector execution failed",
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Action connector execution failed",
        ) from error
    proposal.status = "executed"
    proposal.executed_by_user_id = principal.user_id
    proposal.executed_at = utc_now()
    session.flush()
    _add_audit(session, proposal, principal, "execution_succeeded")
    session.commit()
    session.refresh(proposal)
    return proposal
