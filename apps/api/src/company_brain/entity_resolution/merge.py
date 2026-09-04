from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    Entity,
    EntityMerge,
    EntityResolutionAudit,
    Event,
    Evidence,
    EvidenceLink,
    ExternalReference,
    Memory,
    Relationship,
)
from company_brain.domain.repositories import TenantScope


class EntityMergeError(ValueError):
    pass


@dataclass(frozen=True)
class MergeResult:
    merge_id: UUID
    source_entity_id: UUID
    target_entity_id: UUID
    status: str


def _entity_statement(scope: TenantScope, entity_ids: list[UUID]) -> Select[tuple[Entity]]:
    return (
        select(Entity)
        .where(
            Entity.organization_id == scope.organization_id,
            Entity.workspace_id == scope.workspace_id,
            Entity.id.in_(entity_ids),
        )
        .order_by(Entity.id)
        .with_for_update()
    )


def _relationship_snapshot(relationship: Relationship) -> dict[str, Any]:
    return {
        "id": str(relationship.id),
        "from_entity_id": str(relationship.from_entity_id),
        "to_entity_id": str(relationship.to_entity_id),
        "relationship_type": relationship.relationship_type,
        "confidence": relationship.confidence,
        "valid_from": relationship.valid_from.isoformat() if relationship.valid_from else None,
        "valid_to": relationship.valid_to.isoformat() if relationship.valid_to else None,
        "metadata": relationship.metadata_,
    }


def merge_entities(
    session: Session,
    scope: TenantScope,
    actor_user_id: UUID,
    source_entity_id: UUID,
    target_entity_id: UUID,
) -> MergeResult:
    if source_entity_id == target_entity_id:
        raise EntityMergeError("source and target must be different")
    entities = list(session.scalars(_entity_statement(scope, [source_entity_id, target_entity_id])))
    by_id = {entity.id: entity for entity in entities}
    source = by_id.get(source_entity_id)
    target = by_id.get(target_entity_id)
    if source is None or target is None:
        raise EntityMergeError("entity not found")
    if source.lifecycle_status != "active" or target.lifecycle_status != "active":
        raise EntityMergeError("both entities must be active")
    if source.entity_type != target.entity_type:
        raise EntityMergeError("entities must have the same type")
    active_merge = session.scalar(
        select(EntityMerge.id).where(
            EntityMerge.organization_id == scope.organization_id,
            EntityMerge.workspace_id == scope.workspace_id,
            EntityMerge.status == "active",
            or_(
                EntityMerge.source_entity_id.in_([source.id, target.id]),
                EntityMerge.target_entity_id.in_([source.id, target.id]),
            ),
        )
    )
    if active_merge is not None:
        raise EntityMergeError("entity already participates in an active merge")

    references = list(
        session.scalars(
            select(ExternalReference)
            .where(
                ExternalReference.organization_id == scope.organization_id,
                ExternalReference.workspace_id == scope.workspace_id,
                ExternalReference.entity_id == source.id,
            )
            .with_for_update()
        )
    )
    relationships = list(
        session.scalars(
            select(Relationship)
            .where(
                Relationship.organization_id == scope.organization_id,
                Relationship.workspace_id == scope.workspace_id,
                or_(
                    Relationship.from_entity_id == source.id,
                    Relationship.to_entity_id == source.id,
                    Relationship.from_entity_id == target.id,
                    Relationship.to_entity_id == target.id,
                ),
            )
            .with_for_update()
        )
    )
    events = list(
        session.scalars(
            select(Event)
            .where(
                Event.organization_id == scope.organization_id,
                Event.workspace_id == scope.workspace_id,
                Event.subject_entity_id == source.id,
            )
            .with_for_update()
        )
    )
    memories = list(
        session.scalars(
            select(Memory)
            .where(
                Memory.organization_id == scope.organization_id,
                Memory.workspace_id == scope.workspace_id,
                Memory.subject_entity_id == source.id,
            )
            .with_for_update()
        )
    )
    evidence_links = list(
        session.scalars(
            select(EvidenceLink)
            .where(
                EvidenceLink.organization_id == scope.organization_id,
                EvidenceLink.workspace_id == scope.workspace_id,
                EvidenceLink.entity_id == source.id,
            )
            .with_for_update()
        )
    )

    source_relationships = [
        relationship
        for relationship in relationships
        if relationship.from_entity_id == source.id or relationship.to_entity_id == source.id
    ]
    existing_keys = {
        (
            relationship.from_entity_id,
            relationship.to_entity_id,
            relationship.relationship_type,
        )
        for relationship in relationships
        if relationship not in source_relationships
    }
    prospective_keys: set[tuple[UUID, UUID, str]] = set()
    for relationship in source_relationships:
        next_from = (
            target.id if relationship.from_entity_id == source.id else relationship.from_entity_id
        )
        next_to = target.id if relationship.to_entity_id == source.id else relationship.to_entity_id
        key = (next_from, next_to, relationship.relationship_type)
        if next_from != next_to and (key in existing_keys or key in prospective_keys):
            raise EntityMergeError("merge would create duplicate relationship")
        prospective_keys.add(key)

    deleted_relationships: list[dict[str, Any]] = []
    deleted_relationship_objects: list[Relationship] = []
    moved_relationship_from: list[str] = []
    moved_relationship_to: list[str] = []
    for relationship in source_relationships:
        next_from = (
            target.id if relationship.from_entity_id == source.id else relationship.from_entity_id
        )
        next_to = target.id if relationship.to_entity_id == source.id else relationship.to_entity_id
        if next_from == next_to:
            deleted_relationships.append(_relationship_snapshot(relationship))
            deleted_relationship_objects.append(relationship)
            continue
        if relationship.from_entity_id == source.id:
            moved_relationship_from.append(str(relationship.id))
            relationship.from_entity_id = target.id
        if relationship.to_entity_id == source.id:
            moved_relationship_to.append(str(relationship.id))
            relationship.to_entity_id = target.id

    deleted_relationship_ids = [item.id for item in deleted_relationship_objects]
    deleted_relationship_links = (
        list(
            session.scalars(
                select(EvidenceLink)
                .where(
                    EvidenceLink.organization_id == scope.organization_id,
                    EvidenceLink.workspace_id == scope.workspace_id,
                    EvidenceLink.relationship_id.in_(deleted_relationship_ids),
                )
                .with_for_update()
            )
        )
        if deleted_relationship_ids
        else []
    )
    deleted_relationship_link_snapshot = [
        {
            "id": str(link.id),
            "evidence_id": str(link.evidence_id),
            "relationship_id": str(link.relationship_id),
        }
        for link in deleted_relationship_links
    ]
    for link in deleted_relationship_links:
        session.delete(link)
    if deleted_relationship_links:
        session.flush()
    for relationship in deleted_relationship_objects:
        session.delete(relationship)

    merge_aliases = list(dict.fromkeys([source.name, *source.aliases]))
    added_target_aliases = [alias for alias in merge_aliases if alias not in target.aliases]
    snapshot: dict[str, Any] = {
        "source_lifecycle_status": source.lifecycle_status,
        "source_metadata": source.metadata_,
        "target_aliases": target.aliases,
        "added_target_aliases": added_target_aliases,
        "external_reference_ids": [str(item.id) for item in references],
        "relationship_from_ids": moved_relationship_from,
        "relationship_to_ids": moved_relationship_to,
        "deleted_relationships": deleted_relationships,
        "deleted_relationship_evidence_links": deleted_relationship_link_snapshot,
        "event_ids": [str(item.id) for item in events],
        "memory_ids": [str(item.id) for item in memories],
        "evidence_link_ids": [str(item.id) for item in evidence_links],
    }
    merge = EntityMerge(
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        source_entity_id=source.id,
        target_entity_id=target.id,
        merged_by_user_id=actor_user_id,
        snapshot=snapshot,
    )
    session.add(merge)
    session.flush()

    for reference in references:
        reference.entity_id = target.id
    for event in events:
        event.subject_entity_id = target.id
    for memory in memories:
        memory.subject_entity_id = target.id
    for link in evidence_links:
        link.entity_id = target.id
    target.aliases = [*target.aliases, *added_target_aliases]
    source.lifecycle_status = "merged"
    source.metadata_ = {**source.metadata_, "merged_into": str(target.id)}
    session.add(
        EntityResolutionAudit(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            actor_user_id=actor_user_id,
            action="merge",
            merge_id=merge.id,
            details={"source_entity_id": str(source.id), "target_entity_id": str(target.id)},
        )
    )
    session.flush()
    return MergeResult(merge.id, source.id, target.id, merge.status)


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def split_merge(
    session: Session,
    scope: TenantScope,
    actor_user_id: UUID,
    merge_id: UUID,
) -> MergeResult:
    merge = session.scalar(
        select(EntityMerge)
        .where(
            EntityMerge.organization_id == scope.organization_id,
            EntityMerge.workspace_id == scope.workspace_id,
            EntityMerge.id == merge_id,
        )
        .with_for_update()
    )
    if merge is None:
        raise EntityMergeError("merge not found")
    if merge.status != "active":
        raise EntityMergeError("merge is already split")
    entities = list(
        session.scalars(_entity_statement(scope, [merge.source_entity_id, merge.target_entity_id]))
    )
    by_id = {entity.id: entity for entity in entities}
    source = by_id.get(merge.source_entity_id)
    target = by_id.get(merge.target_entity_id)
    if source is None or target is None:
        raise EntityMergeError("merge entities not found")
    if (
        source.lifecycle_status != "merged"
        or source.metadata_.get("merged_into") != str(target.id)
        or target.lifecycle_status != "active"
    ):
        raise EntityMergeError("merge state has changed")
    snapshot = merge.snapshot

    def uuids(key: str) -> list[UUID]:
        return [UUID(value) for value in snapshot.get(key, [])]

    def require_complete(actual_ids: set[UUID], expected_ids: list[UUID]) -> None:
        if actual_ids != set(expected_ids):
            raise EntityMergeError("merged records have changed")

    reference_ids = uuids("external_reference_ids")
    references = list(
        session.scalars(
            select(ExternalReference)
            .where(
                ExternalReference.organization_id == scope.organization_id,
                ExternalReference.workspace_id == scope.workspace_id,
                ExternalReference.id.in_(reference_ids),
            )
            .with_for_update()
        )
    )
    require_complete({item.id for item in references}, reference_ids)
    if any(reference.entity_id != target.id for reference in references):
        raise EntityMergeError("merged records have changed")
    relationship_from_ids = uuids("relationship_from_ids")
    from_relationships = list(
        session.scalars(
            select(Relationship)
            .where(
                Relationship.organization_id == scope.organization_id,
                Relationship.workspace_id == scope.workspace_id,
                Relationship.id.in_(relationship_from_ids),
            )
            .with_for_update()
        )
    )
    require_complete({item.id for item in from_relationships}, relationship_from_ids)
    if any(relationship.from_entity_id != target.id for relationship in from_relationships):
        raise EntityMergeError("merged records have changed")
    relationship_to_ids = uuids("relationship_to_ids")
    to_relationships = list(
        session.scalars(
            select(Relationship)
            .where(
                Relationship.organization_id == scope.organization_id,
                Relationship.workspace_id == scope.workspace_id,
                Relationship.id.in_(relationship_to_ids),
            )
            .with_for_update()
        )
    )
    require_complete({item.id for item in to_relationships}, relationship_to_ids)
    if any(relationship.to_entity_id != target.id for relationship in to_relationships):
        raise EntityMergeError("merged records have changed")
    event_ids = uuids("event_ids")
    events = list(
        session.scalars(
            select(Event)
            .where(
                Event.organization_id == scope.organization_id,
                Event.workspace_id == scope.workspace_id,
                Event.id.in_(event_ids),
            )
            .with_for_update()
        )
    )
    require_complete({item.id for item in events}, event_ids)
    memory_ids = uuids("memory_ids")
    memories = list(
        session.scalars(
            select(Memory)
            .where(
                Memory.organization_id == scope.organization_id,
                Memory.workspace_id == scope.workspace_id,
                Memory.id.in_(memory_ids),
            )
            .with_for_update()
        )
    )
    require_complete({item.id for item in memories}, memory_ids)
    evidence_link_ids = uuids("evidence_link_ids")
    evidence_links = list(
        session.scalars(
            select(EvidenceLink)
            .where(
                EvidenceLink.organization_id == scope.organization_id,
                EvidenceLink.workspace_id == scope.workspace_id,
                EvidenceLink.id.in_(evidence_link_ids),
            )
            .with_for_update()
        )
    )
    require_complete({item.id for item in evidence_links}, evidence_link_ids)
    if (
        any(event.subject_entity_id != target.id for event in events)
        or any(memory.subject_entity_id != target.id for memory in memories)
        or any(link.entity_id != target.id for link in evidence_links)
    ):
        raise EntityMergeError("merged records have changed")

    deleted_relationships = snapshot.get("deleted_relationships", [])
    deleted_relationship_ids = [UUID(item["id"]) for item in deleted_relationships]
    if deleted_relationship_ids and session.scalar(
        select(Relationship.id).where(Relationship.id.in_(deleted_relationship_ids)).limit(1)
    ):
        raise EntityMergeError("merge restoration IDs are no longer available")
    deleted_link_items = snapshot.get("deleted_relationship_evidence_links", [])
    deleted_link_ids = [UUID(item["id"]) for item in deleted_link_items]
    if deleted_link_ids and session.scalar(
        select(EvidenceLink.id).where(EvidenceLink.id.in_(deleted_link_ids)).limit(1)
    ):
        raise EntityMergeError("merge restoration IDs are no longer available")
    evidence_ids = {UUID(item["evidence_id"]) for item in deleted_link_items}
    if evidence_ids:
        found_evidence_ids = set(
            session.scalars(
                select(Evidence.id).where(
                    Evidence.organization_id == scope.organization_id,
                    Evidence.workspace_id == scope.workspace_id,
                    Evidence.id.in_(evidence_ids),
                )
            )
        )
        if found_evidence_ids != evidence_ids:
            raise EntityMergeError("merge evidence has changed")

    # The database requires every entity-owned row to reference an active entity.
    # All restoration preflight checks are complete and the source row remains
    # locked, so reactivate it before repointing or recreating owned records.
    source.lifecycle_status = snapshot["source_lifecycle_status"]
    source.metadata_ = snapshot["source_metadata"]
    session.flush()

    for reference in references:
        reference.entity_id = source.id
    for relationship in from_relationships:
        relationship.from_entity_id = source.id
    for relationship in to_relationships:
        relationship.to_entity_id = source.id
    for event in events:
        event.subject_entity_id = source.id
    for memory in memories:
        memory.subject_entity_id = source.id
    for link in evidence_links:
        link.entity_id = source.id
    for item in deleted_relationships:
        session.add(
            Relationship(
                id=UUID(item["id"]),
                organization_id=scope.organization_id,
                workspace_id=scope.workspace_id,
                from_entity_id=UUID(item["from_entity_id"]),
                to_entity_id=UUID(item["to_entity_id"]),
                relationship_type=item["relationship_type"],
                confidence=item["confidence"],
                valid_from=_parse_datetime(item["valid_from"]),
                valid_to=_parse_datetime(item["valid_to"]),
                metadata_=item["metadata"],
            )
        )
    if snapshot.get("deleted_relationships"):
        session.flush()
    for item in deleted_link_items:
        session.add(
            EvidenceLink(
                id=UUID(item["id"]),
                organization_id=scope.organization_id,
                workspace_id=scope.workspace_id,
                evidence_id=UUID(item["evidence_id"]),
                relationship_id=UUID(item["relationship_id"]),
            )
        )

    # Alias history is intentionally non-destructive: a value added by merge may
    # have been independently removed and re-added later, which cannot be
    # distinguished from the JSON array alone.
    merge.status = "split"
    merge.split_by_user_id = actor_user_id
    session.add(
        EntityResolutionAudit(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            actor_user_id=actor_user_id,
            action="split",
            merge_id=merge.id,
            details={"source_entity_id": str(source.id), "target_entity_id": str(target.id)},
        )
    )
    session.flush()
    return MergeResult(merge.id, source.id, target.id, merge.status)
