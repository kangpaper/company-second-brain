from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Select, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from company_brain.api.dependencies import (
    Principal,
    get_principal,
    get_tenant_scope,
    require_writer,
)
from company_brain.db.session import get_session
from company_brain.domain.models import (
    Entity,
    EntityResolutionAudit,
    EntityResolutionCase,
    EntityType,
    ExternalReference,
)
from company_brain.domain.repositories import TenantScope
from company_brain.entity_resolution.merge import (
    EntityMergeError,
    MergeResult,
    merge_entities,
    split_merge,
)
from company_brain.entity_resolution.service import (
    ResolutionInput,
    find_resolution_candidates,
    normalize_value,
)

router = APIRouter(prefix="/api/v1/entity-resolution", tags=["entity-resolution"])
SessionDependency = Annotated[Session, Depends(get_session)]
ScopeDependency = Annotated[TenantScope, Depends(get_tenant_scope)]
PrincipalDependency = Annotated[Principal, Depends(get_principal)]
WriterDependency = Annotated[Principal, Depends(require_writer)]


class ExternalReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    source_model: str = Field(min_length=1, max_length=255)
    external_id: str = Field(min_length=1, max_length=255)


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: EntityType
    name: str = Field(min_length=1, max_length=500)
    identifiers: dict[str, str] = Field(default_factory=dict)
    external_reference: ExternalReferenceInput | None = None

    @field_validator("identifiers")
    @classmethod
    def validate_identifiers(cls, value: dict[str, str]) -> dict[str, str]:
        ResolutionInput(entity_type=EntityType.CUSTOMER, name="validation", identifiers=value)
        return value


class CandidateResponse(BaseModel):
    entity_id: UUID
    score: float
    reasons: list[str]


class ResolutionResponse(BaseModel):
    outcome: Literal["matched", "review_required"]
    match_method: Literal["external_reference", "exact_identifier", "exact_name"] | None
    entity_id: UUID | None
    case_id: UUID | None
    candidates: list[CandidateResponse]


class ResolutionCaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    entity_type: EntityType
    query_name: str
    identifiers: dict[str, str]
    candidates: list[dict[str, object]]
    status: str
    selected_entity_id: UUID | None
    resolution_action: str | None


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_entity_id: UUID
    target_entity_id: UUID


class CaseDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["match", "dismiss"]
    entity_id: UUID | None = None

    @model_validator(mode="after")
    def validate_action_shape(self) -> "CaseDecisionRequest":
        if self.action == "match" and self.entity_id is None:
            raise ValueError("match requires entity_id")
        if self.action == "dismiss" and self.entity_id is not None:
            raise ValueError("dismiss does not accept entity_id")
        return self


class MergeResponse(BaseModel):
    merge_id: UUID
    source_entity_id: UUID
    target_entity_id: UUID
    status: str


def _merge_response(result: MergeResult) -> MergeResponse:
    return MergeResponse(
        merge_id=result.merge_id,
        source_entity_id=result.source_entity_id,
        target_entity_id=result.target_entity_id,
        status=result.status,
    )


def _external_reference_match(
    session: Session,
    scope: TenantScope,
    reference: ExternalReferenceInput,
    entity_type: EntityType,
) -> UUID | None:
    return session.scalar(
        select(ExternalReference.entity_id)
        .join(
            Entity,
            (Entity.organization_id == ExternalReference.organization_id)
            & (Entity.workspace_id == ExternalReference.workspace_id)
            & (Entity.id == ExternalReference.entity_id),
        )
        .where(
            ExternalReference.organization_id == scope.organization_id,
            ExternalReference.workspace_id == scope.workspace_id,
            ExternalReference.source_id == reference.source_id,
            ExternalReference.source_model == reference.source_model,
            ExternalReference.external_id == reference.external_id,
            Entity.entity_type == entity_type,
            Entity.lifecycle_status == "active",
        )
    )


@router.post("/resolve", response_model=ResolutionResponse)
def resolve_entity(
    payload: ResolutionRequest,
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
    response: Response,
) -> ResolutionResponse:
    if payload.external_reference is not None:
        entity_id = _external_reference_match(
            session, scope, payload.external_reference, payload.entity_type
        )
        if entity_id is not None:
            return ResolutionResponse(
                outcome="matched",
                match_method="external_reference",
                entity_id=entity_id,
                case_id=None,
                candidates=[],
            )

    entities = list(
        session.scalars(
            select(Entity).where(
                Entity.organization_id == scope.organization_id,
                Entity.workspace_id == scope.workspace_id,
                Entity.entity_type == payload.entity_type,
                Entity.lifecycle_status == "active",
            )
        )
    )
    candidates = find_resolution_candidates(
        ResolutionInput(
            entity_type=payload.entity_type,
            name=payload.name,
            identifiers=payload.identifiers,
        ),
        entities,
    )
    exact = [candidate for candidate in candidates if candidate.score == 1.0]
    if len(exact) == 1:
        match_method = "exact_name" if exact[0].reasons == ["exact:name"] else "exact_identifier"
        return ResolutionResponse(
            outcome="matched",
            match_method=match_method,
            entity_id=exact[0].entity_id,
            case_id=None,
            candidates=[],
        )

    candidate_snapshot = [
        {
            "entity_id": str(candidate.entity_id),
            "score": candidate.score,
            "reasons": candidate.reasons,
        }
        for candidate in candidates
    ]
    resolution_case = EntityResolutionCase(
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        requested_by_user_id=principal.user_id,
        entity_type=payload.entity_type,
        query_name=payload.name.strip(),
        normalized_name=normalize_value(payload.name),
        identifiers=payload.identifiers,
        candidates=candidate_snapshot,
    )
    session.add(resolution_case)
    session.commit()
    session.refresh(resolution_case)
    response.status_code = status.HTTP_202_ACCEPTED
    return ResolutionResponse(
        outcome="review_required",
        match_method=None,
        entity_id=None,
        case_id=resolution_case.id,
        candidates=[CandidateResponse(**candidate) for candidate in candidate_snapshot],
    )


@router.post("/cases/{case_id}/decision", response_model=ResolutionCaseResponse)
def decide_resolution_case(
    case_id: UUID,
    payload: CaseDecisionRequest,
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
) -> EntityResolutionCase:
    resolution_case = session.scalar(
        select(EntityResolutionCase)
        .where(
            EntityResolutionCase.organization_id == scope.organization_id,
            EntityResolutionCase.workspace_id == scope.workspace_id,
            EntityResolutionCase.id == case_id,
        )
        .with_for_update()
    )
    if resolution_case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Resolution case not found"
        )
    if resolution_case.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Resolution case is closed"
        )

    if payload.action == "match":
        assert payload.entity_id is not None
        candidate_ids = {candidate["entity_id"] for candidate in resolution_case.candidates}
        if str(payload.entity_id) not in candidate_ids:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Entity is not a candidate for this case",
            )
        selected = session.scalar(
            _candidate_for_decision_statement(scope, payload.entity_id, resolution_case.entity_type)
        )
        if selected is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Candidate is no longer eligible",
            )
        resolution_case.status = "resolved"
        resolution_case.selected_entity_id = selected.id
        resolution_case.resolution_action = "match"
    else:
        resolution_case.status = "dismissed"
        resolution_case.resolution_action = "dismiss"

    session.add(
        EntityResolutionAudit(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            actor_user_id=principal.user_id,
            action=payload.action,
            details={
                "case_id": str(resolution_case.id),
                "entity_id": str(payload.entity_id) if payload.entity_id else None,
            },
        )
    )
    session.commit()
    session.refresh(resolution_case)
    return resolution_case


def _candidate_for_decision_statement(
    scope: TenantScope, entity_id: UUID, entity_type: EntityType
) -> Select[tuple[Entity]]:
    return (
        select(Entity)
        .where(
            Entity.organization_id == scope.organization_id,
            Entity.workspace_id == scope.workspace_id,
            Entity.id == entity_id,
            Entity.entity_type == entity_type,
            Entity.lifecycle_status == "active",
        )
        .with_for_update()
    )


@router.post("/merge", response_model=MergeResponse)
def merge_entity_records(
    payload: MergeRequest,
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
) -> MergeResponse:
    try:
        result = merge_entities(
            session,
            scope,
            principal.user_id,
            payload.source_entity_id,
            payload.target_entity_id,
        )
        session.commit()
    except EntityMergeError as error:
        session.rollback()
        status_code = (
            status.HTTP_404_NOT_FOUND
            if str(error) in {"entity not found", "merge entities not found"}
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    except DBAPIError as error:
        session.rollback()
        message = str(error)
        if "active merge" in message:
            detail = "Entity already participates in an active merge"
        elif "uq_relationship_typed_edge" in message:
            detail = "Merge would create a duplicate relationship"
        else:
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from error
    return _merge_response(result)


@router.post("/merges/{merge_id}/split", response_model=MergeResponse)
def split_entity_records(
    merge_id: UUID,
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
) -> MergeResponse:
    try:
        result = split_merge(session, scope, principal.user_id, merge_id)
        session.commit()
    except EntityMergeError as error:
        session.rollback()
        status_code = (
            status.HTTP_404_NOT_FOUND
            if str(error) in {"merge not found", "merge entities not found"}
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Merge restoration conflicts with current data",
        ) from error
    return _merge_response(result)


@router.get("/cases", response_model=list[ResolutionCaseResponse])
def list_resolution_cases(
    session: SessionDependency,
    scope: ScopeDependency,
    _: PrincipalDependency,
) -> list[EntityResolutionCase]:
    return list(
        session.scalars(
            select(EntityResolutionCase)
            .where(
                EntityResolutionCase.organization_id == scope.organization_id,
                EntityResolutionCase.workspace_id == scope.workspace_id,
            )
            .order_by(EntityResolutionCase.created_at, EntityResolutionCase.id)
        )
    )
