import re
import unicodedata
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from company_brain.api.dependencies import Principal, get_tenant_scope, require_writer
from company_brain.api.schemas import EntityCreate, EntityRead, EntityUpdate
from company_brain.db.session import get_session
from company_brain.domain.models import Entity, EntityType
from company_brain.domain.repositories import EntityRepository, TenantScope

router = APIRouter(prefix="/api/v1/entities", tags=["entities"])
SessionDependency = Annotated[Session, Depends(get_session)]
ScopeDependency = Annotated[TenantScope, Depends(get_tenant_scope)]
WriterDependency = Annotated[Principal, Depends(require_writer)]


def normalize_name(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", ascii_name.casefold()).strip()


@router.post("", response_model=EntityRead, status_code=status.HTTP_201_CREATED)
def create_entity(
    payload: EntityCreate, session: SessionDependency, scope: ScopeDependency, _: WriterDependency
) -> Entity:
    entity = Entity(
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        entity_type=payload.entity_type,
        name=payload.name,
        normalized_name=normalize_name(payload.name),
        aliases=payload.aliases,
        metadata_=payload.metadata,
    )
    session.add(entity)
    session.commit()
    session.refresh(entity)
    return entity


@router.get("", response_model=list[EntityRead])
def list_entities(
    session: SessionDependency,
    scope: ScopeDependency,
    entity_type: Annotated[EntityType | None, Query(alias="type")] = None,
    q: str | None = None,
) -> list[Entity]:
    normalized_query = normalize_name(q) if q else None
    return EntityRepository(session, scope).list(entity_type, normalized_query)


@router.get("/{entity_id}", response_model=EntityRead)
def get_entity(entity_id: UUID, session: SessionDependency, scope: ScopeDependency) -> Entity:
    entity = EntityRepository(session, scope).get(entity_id)
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")
    return entity


@router.patch("/{entity_id}", response_model=EntityRead)
def update_entity(
    entity_id: UUID,
    payload: EntityUpdate,
    session: SessionDependency,
    scope: ScopeDependency,
    _: WriterDependency,
) -> Entity:
    entity = EntityRepository(session, scope).get(entity_id)
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")
    changes = payload.model_dump(exclude_unset=True)
    if "metadata" in changes:
        entity.metadata_ = changes.pop("metadata")
    if "name" in changes:
        entity.normalized_name = normalize_name(changes["name"])
    for field, value in changes.items():
        setattr(entity, field, value)
    session.commit()
    session.refresh(entity)
    return entity


@router.delete("/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entity(
    entity_id: UUID, session: SessionDependency, scope: ScopeDependency, _: WriterDependency
) -> Response:
    entity = EntityRepository(session, scope).get(entity_id)
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")
    session.delete(entity)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Entity is referenced by other records",
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
