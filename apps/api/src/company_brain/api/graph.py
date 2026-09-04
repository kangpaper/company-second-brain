from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from company_brain.api.dependencies import get_tenant_scope
from company_brain.db.session import get_session
from company_brain.domain.models import Entity, EntityType, Relationship
from company_brain.domain.repositories import EntityRepository, TenantScope

router = APIRouter(prefix="/api/v1/graph", tags=["graph"])
SessionDependency = Annotated[Session, Depends(get_session)]
ScopeDependency = Annotated[TenantScope, Depends(get_tenant_scope)]


class GraphNode(BaseModel):
    id: UUID
    entity_type: EntityType
    name: str
    metadata: dict[str, object]


class GraphEdge(BaseModel):
    id: UUID
    from_entity_id: UUID
    to_entity_id: UUID
    relationship_type: str
    confidence: float
    metadata: dict[str, object]


class GraphRead(BaseModel):
    root_entity_id: UUID | None
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = False


def node_read(entity: Entity) -> GraphNode:
    return GraphNode(
        id=entity.id,
        entity_type=entity.entity_type,
        name=entity.name,
        metadata=entity.metadata_,
    )


def edge_read(relationship: Relationship) -> GraphEdge:
    return GraphEdge(
        id=relationship.id,
        from_entity_id=relationship.from_entity_id,
        to_entity_id=relationship.to_entity_id,
        relationship_type=relationship.relationship_type,
        confidence=relationship.confidence,
        metadata=relationship.metadata_,
    )


def scoped_relationships(
    session: Session,
    scope: TenantScope,
    entity_ids: set[UUID] | None = None,
    relationship_type: str | None = None,
    limit: int | None = None,
) -> list[Relationship]:
    statement = select(Relationship).where(
        Relationship.organization_id == scope.organization_id,
        Relationship.workspace_id == scope.workspace_id,
    )
    if entity_ids is not None:
        statement = statement.where(
            or_(
                Relationship.from_entity_id.in_(entity_ids),
                Relationship.to_entity_id.in_(entity_ids),
            )
        )
    if relationship_type is not None:
        statement = statement.where(Relationship.relationship_type == relationship_type)
    statement = statement.order_by(Relationship.id)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


@router.get("", response_model=GraphRead)
def get_graph(
    session: SessionDependency,
    scope: ScopeDependency,
    root_entity_id: UUID | None = None,
    depth: Annotated[int, Query(ge=0, le=3)] = 1,
    relationship_type: str | None = None,
    entity_type: Annotated[EntityType | None, Query(alias="type")] = None,
    node_limit: Annotated[int, Query(ge=1, le=500)] = 200,
    edge_limit: Annotated[int, Query(ge=0, le=1000)] = 400,
) -> GraphRead:
    if root_entity_id is None:
        entity_statement = select(Entity).where(
            Entity.organization_id == scope.organization_id,
            Entity.workspace_id == scope.workspace_id,
        )
        if entity_type is not None:
            entity_statement = entity_statement.where(Entity.entity_type == entity_type)
        entity_rows = list(
            session.scalars(entity_statement.order_by(Entity.id).limit(node_limit + 1))
        )
        truncated = len(entity_rows) > node_limit
        entities = entity_rows[:node_limit]
        node_ids = {entity.id for entity in entities}
        edge_rows = [
            edge
            for edge in scoped_relationships(
                session, scope, None, relationship_type, edge_limit + 1
            )
            if edge.from_entity_id in node_ids and edge.to_entity_id in node_ids
        ]
        truncated = truncated or len(edge_rows) > edge_limit
        edges = edge_rows[:edge_limit]
        return GraphRead(
            root_entity_id=None,
            nodes=[node_read(entity) for entity in entities],
            edges=[edge_read(edge) for edge in edges],
            truncated=truncated,
        )

    root = EntityRepository(session, scope).get(root_entity_id)
    if root is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")

    visited = {root.id}
    frontier = {root.id}
    included_edges: dict[UUID, Relationship] = {}
    truncated = False
    for _ in range(depth):
        if not frontier:
            break
        remaining_edges = edge_limit - len(included_edges)
        if remaining_edges <= 0:
            truncated = True
            break
        adjacent = scoped_relationships(
            session,
            scope,
            frontier,
            relationship_type,
            remaining_edges + 1,
        )
        if len(adjacent) > remaining_edges:
            truncated = True
            adjacent = adjacent[:remaining_edges]
        next_frontier: set[UUID] = set()
        for edge in adjacent:
            new_ids = {
                entity_id
                for entity_id in (edge.from_entity_id, edge.to_entity_id)
                if entity_id not in visited and entity_id not in next_frontier
            }
            if len(visited) + len(next_frontier) + len(new_ids) > node_limit:
                truncated = True
                continue
            included_edges[edge.id] = edge
            next_frontier.update(new_ids)
        visited.update(next_frontier)
        frontier = next_frontier

    entity_statement = select(Entity).where(
        Entity.organization_id == scope.organization_id,
        Entity.workspace_id == scope.workspace_id,
        Entity.id.in_(visited),
    )
    entities = list(session.scalars(entity_statement.order_by(Entity.id)))
    if entity_type is not None:
        retained = {root.id} | {
            entity.id for entity in entities if entity.entity_type == entity_type
        }
        entities = [entity for entity in entities if entity.id in retained]
        included_edges = {
            edge_id: edge
            for edge_id, edge in included_edges.items()
            if edge.from_entity_id in retained and edge.to_entity_id in retained
        }

    return GraphRead(
        root_entity_id=root.id,
        nodes=[node_read(entity) for entity in entities],
        edges=[edge_read(included_edges[key]) for key in sorted(included_edges, key=str)],
        truncated=truncated,
    )
