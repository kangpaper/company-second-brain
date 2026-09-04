from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from company_brain.domain.models import Entity, ExternalReference, Source
from company_brain.domain.repositories import TenantScope
from company_brain.integrations.odoo.mapping import CanonicalEntityDTO


@dataclass(frozen=True)
class MappingResult:
    entity: Entity
    source: Source
    external_reference: ExternalReference
    created: bool


def _find_source(
    session: Session, scope: TenantScope, source_uri: str
) -> Source | None:
    return session.scalar(
        select(Source).where(
            Source.organization_id == scope.organization_id,
            Source.workspace_id == scope.workspace_id,
            Source.source_type == "odoo_instance",
            Source.uri == source_uri,
        )
    )


def _get_or_create_source(
    session: Session, scope: TenantScope, source_uri: str
) -> Source:
    source = _find_source(session, scope, source_uri)
    if source is not None:
        return source
    try:
        with session.begin_nested():
            source = Source(
                organization_id=scope.organization_id,
                workspace_id=scope.workspace_id,
                source_type="odoo_instance",
                uri=source_uri,
                metadata_={"source_system": "odoo"},
            )
            session.add(source)
            session.flush()
        return source
    except IntegrityError:
        source = _find_source(session, scope, source_uri)
        if source is None:
            raise
        return source


def _find_mapping(
    session: Session,
    scope: TenantScope,
    source: Source,
    dto: CanonicalEntityDTO,
) -> tuple[Entity, ExternalReference] | None:
    reference = session.scalar(
        select(ExternalReference).where(
            ExternalReference.organization_id == scope.organization_id,
            ExternalReference.workspace_id == scope.workspace_id,
            ExternalReference.source_id == source.id,
            ExternalReference.source_model == dto.source_model,
            ExternalReference.external_id == dto.external_id,
        )
    )
    if reference is None:
        return None
    entity = session.scalar(
        select(Entity).where(
            Entity.organization_id == scope.organization_id,
            Entity.workspace_id == scope.workspace_id,
            Entity.id == reference.entity_id,
        )
    )
    if entity is None:
        raise RuntimeError("Odoo mapping reference is inconsistent")
    return entity, reference


def _update_entity(entity: Entity, dto: CanonicalEntityDTO) -> None:
    entity.entity_type = dto.entity_type
    entity.name = dto.name
    entity.normalized_name = dto.normalized_name
    entity.lifecycle_status = dto.lifecycle_status
    entity.metadata_ = dto.attributes


def persist_odoo_mapping(
    session: Session,
    scope: TenantScope,
    dto: CanonicalEntityDTO,
    source_uri: str,
) -> MappingResult:
    source = _get_or_create_source(session, scope, source_uri)
    existing = _find_mapping(session, scope, source, dto)
    if existing is not None:
        entity, reference = existing
        _update_entity(entity, dto)
        session.flush()
        return MappingResult(entity, source, reference, created=False)

    try:
        with session.begin_nested():
            entity = Entity(
                organization_id=scope.organization_id,
                workspace_id=scope.workspace_id,
                entity_type=dto.entity_type,
                name=dto.name,
                normalized_name=dto.normalized_name,
                lifecycle_status=dto.lifecycle_status,
                metadata_=dto.attributes,
            )
            session.add(entity)
            session.flush()
            reference = ExternalReference(
                organization_id=scope.organization_id,
                workspace_id=scope.workspace_id,
                entity_id=entity.id,
                source_id=source.id,
                source_system="odoo",
                source_model=dto.source_model,
                external_id=dto.external_id,
                raw_ref={},
            )
            session.add(reference)
            session.flush()
        return MappingResult(entity, source, reference, created=True)
    except IntegrityError:
        existing = _find_mapping(session, scope, source, dto)
        if existing is None:
            raise
        entity, reference = existing
        _update_entity(entity, dto)
        session.flush()
        return MappingResult(entity, source, reference, created=False)
