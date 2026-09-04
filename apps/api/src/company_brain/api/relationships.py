from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from company_brain.api.dependencies import Principal, get_tenant_scope, require_writer
from company_brain.api.schemas import RelationshipCreate, RelationshipRead, RelationshipUpdate
from company_brain.db.session import get_session
from company_brain.domain.models import Relationship
from company_brain.domain.repositories import EntityRepository, TenantScope

router = APIRouter(prefix="/api/v1", tags=["relationships"])
SessionDependency = Annotated[Session, Depends(get_session)]
ScopeDependency = Annotated[TenantScope, Depends(get_tenant_scope)]
WriterDependency = Annotated[Principal, Depends(require_writer)]


def _commit_relationship(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Relationship conflicts with graph integrity rules",
        ) from error


def get_scoped_relationship(
    relationship_id: UUID, session: Session, scope: TenantScope
) -> Relationship | None:
    return session.scalar(
        select(Relationship).where(
            Relationship.id == relationship_id,
            Relationship.organization_id == scope.organization_id,
            Relationship.workspace_id == scope.workspace_id,
        )
    )


@router.post("/relationships", response_model=RelationshipRead, status_code=status.HTTP_201_CREATED)
def create_relationship(
    payload: RelationshipCreate,
    session: SessionDependency,
    scope: ScopeDependency,
    _: WriterDependency,
) -> Relationship:
    entities = EntityRepository(session, scope)
    if entities.get(payload.from_entity_id) is None or entities.get(payload.to_entity_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")
    relationship = Relationship(
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        from_entity_id=payload.from_entity_id,
        to_entity_id=payload.to_entity_id,
        relationship_type=payload.relationship_type,
        confidence=payload.confidence,
    )
    session.add(relationship)
    _commit_relationship(session)
    session.refresh(relationship)
    return relationship


@router.get("/relationships/{relationship_id}", response_model=RelationshipRead)
def get_relationship(
    relationship_id: UUID, session: SessionDependency, scope: ScopeDependency
) -> Relationship:
    relationship = get_scoped_relationship(relationship_id, session, scope)
    if relationship is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Relationship not found")
    return relationship


@router.patch("/relationships/{relationship_id}", response_model=RelationshipRead)
def update_relationship(
    relationship_id: UUID,
    payload: RelationshipUpdate,
    session: SessionDependency,
    scope: ScopeDependency,
    _: WriterDependency,
) -> Relationship:
    relationship = get_scoped_relationship(relationship_id, session, scope)
    if relationship is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Relationship not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(relationship, field, value)
    _commit_relationship(session)
    session.refresh(relationship)
    return relationship


@router.delete("/relationships/{relationship_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_relationship(
    relationship_id: UUID,
    session: SessionDependency,
    scope: ScopeDependency,
    _: WriterDependency,
) -> None:
    relationship = get_scoped_relationship(relationship_id, session, scope)
    if relationship is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Relationship not found")
    session.delete(relationship)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Relationship is referenced by evidence",
        ) from error


@router.get("/entities/{entity_id}/relationships", response_model=list[RelationshipRead])
def list_relationships(
    entity_id: UUID, session: SessionDependency, scope: ScopeDependency
) -> list[Relationship]:
    if EntityRepository(session, scope).get(entity_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")
    statement = select(Relationship).where(
        Relationship.organization_id == scope.organization_id,
        Relationship.workspace_id == scope.workspace_id,
        or_(Relationship.from_entity_id == entity_id, Relationship.to_entity_id == entity_id),
    )
    return list(session.scalars(statement))
