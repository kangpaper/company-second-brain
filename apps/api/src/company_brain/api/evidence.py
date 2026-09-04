from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from company_brain.api.dependencies import Principal, get_tenant_scope, require_writer
from company_brain.api.schemas import EvidenceCreate, EvidenceRead
from company_brain.db.session import get_session
from company_brain.domain.models import Evidence, EvidenceLink, Source
from company_brain.domain.repositories import EntityRepository, TenantScope

router = APIRouter(prefix="/api/v1", tags=["evidence"])
SessionDependency = Annotated[Session, Depends(get_session)]
ScopeDependency = Annotated[TenantScope, Depends(get_tenant_scope)]
WriterDependency = Annotated[Principal, Depends(require_writer)]


@router.post(
    "/entities/{entity_id}/evidence",
    response_model=EvidenceRead,
    status_code=status.HTTP_201_CREATED,
)
def attach_entity_evidence(
    entity_id: UUID,
    payload: EvidenceCreate,
    session: SessionDependency,
    scope: ScopeDependency,
    _: WriterDependency,
) -> Evidence:
    if EntityRepository(session, scope).get(entity_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")
    source = Source(
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        source_type=payload.source_type,
        uri=payload.uri,
    )
    session.add(source)
    session.flush()
    evidence = Evidence(
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        source_id=source.id,
        evidence_type=payload.evidence_type,
        pointer=payload.pointer,
        quote=payload.quote,
    )
    session.add(evidence)
    session.flush()
    session.add(
        EvidenceLink(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            evidence_id=evidence.id,
            entity_id=entity_id,
        )
    )
    session.commit()
    session.refresh(evidence)
    return evidence


@router.get("/entities/{entity_id}/evidence", response_model=list[EvidenceRead])
def list_entity_evidence(
    entity_id: UUID, session: SessionDependency, scope: ScopeDependency
) -> list[Evidence]:
    if EntityRepository(session, scope).get(entity_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")
    statement = (
        select(Evidence)
        .join(EvidenceLink, EvidenceLink.evidence_id == Evidence.id)
        .where(
            EvidenceLink.organization_id == scope.organization_id,
            EvidenceLink.workspace_id == scope.workspace_id,
            EvidenceLink.entity_id == entity_id,
        )
    )
    return list(session.scalars(statement))
