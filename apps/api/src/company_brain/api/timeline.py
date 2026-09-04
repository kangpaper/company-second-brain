from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from company_brain.api.dependencies import get_tenant_scope
from company_brain.api.graph import scoped_relationships
from company_brain.db.session import get_session
from company_brain.domain.models import Event
from company_brain.domain.repositories import EntityRepository, TenantScope

router = APIRouter(prefix="/api/v1/timeline", tags=["timeline"])
SessionDependency = Annotated[Session, Depends(get_session)]
ScopeDependency = Annotated[TenantScope, Depends(get_tenant_scope)]


class TimelineItem(BaseModel):
    id: UUID
    subject_entity_id: UUID | None
    event_type: str
    occurred_at: datetime
    payload: dict[str, object]


def reachable_entity_ids(
    root_entity_id: UUID,
    depth: int,
    session: Session,
    scope: TenantScope,
) -> set[UUID]:
    node_limit = 500
    edge_limit = 1000
    visited = {root_entity_id}
    frontier = {root_entity_id}
    traversed_edges = 0
    for _ in range(depth):
        if not frontier or len(visited) >= node_limit or traversed_edges >= edge_limit:
            break
        remaining_edges = edge_limit - traversed_edges
        adjacent = scoped_relationships(
            session, scope, frontier, limit=remaining_edges
        )
        traversed_edges += len(adjacent)
        next_frontier: set[UUID] = set()
        for edge in adjacent:
            for entity_id in (edge.from_entity_id, edge.to_entity_id):
                if entity_id not in visited and len(visited) + len(next_frontier) < node_limit:
                    next_frontier.add(entity_id)
        visited.update(next_frontier)
        frontier = next_frontier
    return visited


def aware_utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} must include a timezone offset",
        )
    return value.astimezone(UTC)


@router.get("", response_model=list[TimelineItem])
def get_timeline(
    session: SessionDependency,
    scope: ScopeDependency,
    root_entity_id: UUID | None = None,
    depth: Annotated[int, Query(ge=0, le=3)] = 1,
    event_type: str | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> list[Event]:
    from_at = aware_utc(from_at, "from_at")
    to_at = aware_utc(to_at, "to_at")
    if from_at is not None and to_at is not None and from_at > to_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="from_at must not be after to_at",
        )

    statement = select(Event).where(
        Event.organization_id == scope.organization_id,
        Event.workspace_id == scope.workspace_id,
    )
    if root_entity_id is not None:
        if EntityRepository(session, scope).get(root_entity_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")
        statement = statement.where(
            Event.subject_entity_id.in_(
                reachable_entity_ids(root_entity_id, depth, session, scope)
            )
        )
    if event_type is not None:
        statement = statement.where(Event.event_type == event_type)
    if from_at is not None:
        statement = statement.where(Event.occurred_at >= from_at)
    if to_at is not None:
        statement = statement.where(Event.occurred_at <= to_at)
    statement = statement.order_by(Event.occurred_at.desc(), Event.id.desc())
    return list(session.scalars(statement.offset(offset).limit(limit)))
