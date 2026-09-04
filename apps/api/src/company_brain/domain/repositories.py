from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from company_brain.domain.models import Entity, EntityType


@dataclass(frozen=True)
class TenantScope:
    organization_id: UUID
    workspace_id: UUID


class EntityRepository:
    def __init__(self, session: Session, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope

    def get(self, entity_id: UUID) -> Entity | None:
        statement = select(Entity).where(
            Entity.id == entity_id,
            Entity.organization_id == self._scope.organization_id,
            Entity.workspace_id == self._scope.workspace_id,
        )
        return self._session.scalar(statement)

    def list(self, entity_type: EntityType | None = None, query: str | None = None) -> list[Entity]:
        statement = select(Entity).where(
            Entity.organization_id == self._scope.organization_id,
            Entity.workspace_id == self._scope.workspace_id,
        )
        if entity_type is not None:
            statement = statement.where(Entity.entity_type == entity_type)
        if query:
            statement = statement.where(Entity.normalized_name.contains(query))
        return list(self._session.scalars(statement))
