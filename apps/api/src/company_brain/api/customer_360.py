import math
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from company_brain.api.dependencies import get_tenant_scope
from company_brain.customer_360.service import (
    CustomerContext,
    calculate_metrics,
    load_customer_context,
)
from company_brain.db.session import get_session
from company_brain.domain.models import Entity, EntityRevision, Event, Evidence, Memory
from company_brain.domain.repositories import TenantScope

router = APIRouter(prefix="/api/v1/customers", tags=["customer-360"])
SessionDependency = Annotated[Session, Depends(get_session)]
ScopeDependency = Annotated[TenantScope, Depends(get_tenant_scope)]


class CustomerProfile(BaseModel):
    id: UUID
    name: str
    aliases: list[str]
    email: str | None
    phone: str | None


class BusinessRecord(BaseModel):
    id: UUID
    entity_type: str
    name: str
    lifecycle_status: str
    attributes: dict[str, Any]
    evidence_ids: list[UUID]


class TimelineRecord(BaseModel):
    id: UUID
    subject_entity_id: UUID | None
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any]
    evidence_ids: list[UUID]


class RelationshipRecord(BaseModel):
    id: UUID
    from_entity_id: UUID
    to_entity_id: UUID
    relationship_type: str
    confidence: float | None
    evidence_ids: list[UUID]


class MemoryRecord(BaseModel):
    id: UUID
    memory_type: str
    text: str
    structured_facts: dict[str, Any]
    confidence: float | None
    evidence_ids: list[UUID]


class EvidenceRecord(BaseModel):
    id: UUID
    source_id: UUID
    evidence_type: str
    pointer: dict[str, Any]
    quote: str | None


class Customer360Response(BaseModel):
    customer: CustomerProfile
    metrics: dict[str, Any]
    signals: list[dict[str, Any]]
    orders: list[BusinessRecord]
    invoices: list[BusinessRecord]
    opportunities: list[BusinessRecord]
    tickets: list[BusinessRecord]
    meetings: list[BusinessRecord]
    projects: list[BusinessRecord]
    documents: list[BusinessRecord]
    decisions: list[BusinessRecord]
    timeline: list[TimelineRecord]
    relationships: list[RelationshipRecord]
    memories: list[MemoryRecord]
    evidence: list[EvidenceRecord]
    data_gaps: list[str]


class CustomerMetricsResponse(BaseModel):
    customer_id: UUID
    window: Literal["6m"]
    as_of: datetime
    metrics: dict[str, Any]
    data_gaps: list[str]


class CustomerRiskResponse(BaseModel):
    customer_id: UUID
    as_of: datetime
    signals: list[dict[str, Any]]
    data_gaps: list[str]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="as_of must include a timezone offset",
        )
    try:
        return value.astimezone(UTC)
    except OverflowError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="as_of is outside the supported UTC range",
        ) from exc


def _sanitize_json(
    value: Any, *, depth: int = 0, ancestors: frozenset[int] = frozenset()
) -> tuple[Any, bool]:
    if depth > 32:
        return None, True
    if value is None or isinstance(value, (str, bool)):
        return value, False
    if isinstance(value, int):
        return (value, False) if value.bit_length() <= 4096 else (None, True)
    if isinstance(value, float):
        return (value, False) if math.isfinite(value) else (None, True)
    if isinstance(value, (list, dict)):
        identity = id(value)
        if identity in ancestors:
            return None, True
        ancestors = ancestors | {identity}
    if isinstance(value, list):
        sanitized_list = [
            _sanitize_json(item, depth=depth + 1, ancestors=ancestors)
            for item in value
        ]
        return [item for item, _ in sanitized_list], any(
            changed for _, changed in sanitized_list
        )
    if isinstance(value, dict):
        sanitized_dict: dict[str, tuple[Any, bool]] = {}
        changed = False
        for key, item in value.items():
            string_key = key if isinstance(key, str) else str(key)
            if not isinstance(key, str) or string_key in sanitized_dict:
                changed = True
            sanitized_item, item_changed = _sanitize_json(
                item, depth=depth + 1, ancestors=ancestors
            )
            sanitized_dict[string_key] = (sanitized_item, item_changed)
        return (
            {key: item for key, (item, _) in sanitized_dict.items()},
            changed or any(item_changed for _, item_changed in sanitized_dict.values()),
        )
    return None, True


def _sanitize_aliases(value: Any) -> tuple[list[str], bool]:
    if not isinstance(value, list):
        return [], True
    aliases = [alias for alias in value if isinstance(alias, str)]
    return aliases, len(aliases) != len(value)


def _sanitize_object(value: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, dict):
        return {}, True
    sanitized, changed = _sanitize_json(value)
    return sanitized, changed


def _business_record(
    entity: Entity | EntityRevision, evidence_ids: list[UUID], data_gaps: list[str]
) -> BusinessRecord:
    attributes, changed = _sanitize_object(entity.metadata_)
    if changed:
        data_gaps.append(f"invalid_business_attributes:{entity.id}")
    return BusinessRecord(
        id=entity.id,
        entity_type=entity.entity_type.value,
        name=entity.name,
        lifecycle_status=entity.lifecycle_status,
        attributes=attributes,
        evidence_ids=evidence_ids,
    )


def _timeline_record(
    event: Event, evidence_ids: list[UUID], data_gaps: list[str]
) -> TimelineRecord:
    payload, changed = _sanitize_object(event.payload)
    if changed:
        data_gaps.append(f"invalid_timeline_payload:{event.id}")
    return TimelineRecord(
        id=event.id,
        subject_entity_id=event.subject_entity_id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        payload=payload,
        evidence_ids=evidence_ids,
    )


def _memory_record(
    memory: Memory, evidence_ids: list[UUID], data_gaps: list[str]
) -> MemoryRecord:
    structured_facts, changed = _sanitize_object(memory.structured_facts)
    if changed:
        data_gaps.append(f"invalid_memory_structured_facts:{memory.id}")
    return MemoryRecord(
        id=memory.id,
        memory_type=memory.memory_type.value,
        text=memory.text,
        structured_facts=structured_facts,
        confidence=_finite_confidence(memory.confidence),
        evidence_ids=evidence_ids,
    )


def _evidence_record(evidence: Evidence, data_gaps: list[str]) -> EvidenceRecord:
    pointer, changed = _sanitize_object(evidence.pointer)
    if changed:
        data_gaps.append(f"invalid_evidence_pointer:{evidence.id}")
    return EvidenceRecord(
        id=evidence.id,
        source_id=evidence.source_id,
        evidence_type=evidence.evidence_type,
        pointer=pointer,
        quote=evidence.quote,
    )


def _finite_confidence(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _context_metrics(
    customer_id: UUID,
    session: Session,
    scope: TenantScope,
    as_of: datetime,
) -> tuple[
    CustomerContext, dict[str, Any], list[dict[str, Any]], list[str], datetime
]:
    normalized_as_of = _aware_utc(as_of)
    if normalized_as_of < datetime(1, 7, 1, tzinfo=UTC):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="as_of must support a complete six-month window",
        )
    context = load_customer_context(session, scope, customer_id, normalized_as_of)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    try:
        metrics, signals, data_gaps = calculate_metrics(context, normalized_as_of)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="as_of must support a complete six-month window",
        ) from exc
    return context, metrics, signals, data_gaps, normalized_as_of


@router.get("/{customer_id}/metrics", response_model=CustomerMetricsResponse)
def get_customer_metrics(
    customer_id: UUID,
    session: SessionDependency,
    scope: ScopeDependency,
    as_of: Annotated[datetime, Query()],
    window: Annotated[Literal["6m"], Query()] = "6m",
) -> CustomerMetricsResponse:
    _, metrics, _, data_gaps, normalized_as_of = _context_metrics(
        customer_id, session, scope, as_of
    )
    return CustomerMetricsResponse(
        customer_id=customer_id,
        window=window,
        as_of=normalized_as_of,
        metrics=metrics,
        data_gaps=data_gaps,
    )


@router.get("/{customer_id}/risk", response_model=CustomerRiskResponse)
def get_customer_risk(
    customer_id: UUID,
    session: SessionDependency,
    scope: ScopeDependency,
    as_of: Annotated[datetime, Query()],
) -> CustomerRiskResponse:
    _, _, signals, data_gaps, normalized_as_of = _context_metrics(
        customer_id, session, scope, as_of
    )
    return CustomerRiskResponse(
        customer_id=customer_id,
        as_of=normalized_as_of,
        signals=signals,
        data_gaps=data_gaps,
    )


def build_customer_360_response(
    customer_id: UUID,
    session: Session,
    scope: TenantScope,
    as_of: datetime,
) -> tuple[Customer360Response, datetime]:
    context, metrics, signals, data_gaps, normalized_as_of = _context_metrics(
        customer_id, session, scope, as_of
    )
    metadata, metadata_changed = _sanitize_object(context.customer.metadata_)
    if metadata_changed:
        data_gaps.append(f"invalid_customer_metadata:{context.customer.id}")
    aliases, aliases_changed = _sanitize_aliases(context.customer.aliases)
    if aliases_changed:
        data_gaps.append(f"invalid_customer_aliases:{context.customer.id}")
    for relationship in context.relationships:
        if not math.isfinite(relationship.confidence):
            data_gaps.append(f"invalid_relationship_confidence:{relationship.id}")
    for memory in context.memories:
        if not math.isfinite(memory.confidence):
            data_gaps.append(f"invalid_memory_confidence:{memory.id}")
    collections = {
        name: [
            _business_record(
                entity, context.evidence_by_entity.get(entity.id, []), data_gaps
            )
            for entity in entities
        ]
        for name, entities in context.collections.items()
    }
    response = Customer360Response(
        customer=CustomerProfile(
            id=context.customer.id,
            name=context.customer.name,
            aliases=aliases,
            email=metadata.get("email") if isinstance(metadata.get("email"), str) else None,
            phone=metadata.get("phone") if isinstance(metadata.get("phone"), str) else None,
        ),
        metrics=metrics,
        signals=signals,
        timeline=[
            _timeline_record(
                event, context.evidence_by_event.get(event.id, []), data_gaps
            )
            for event in context.events
        ],
        relationships=[
            RelationshipRecord(
                id=item.id,
                from_entity_id=item.from_entity_id,
                to_entity_id=item.to_entity_id,
                relationship_type=item.relationship_type,
                confidence=_finite_confidence(item.confidence),
                evidence_ids=context.evidence_by_relationship.get(item.id, []),
            )
            for item in context.relationships
        ],
        memories=[
            _memory_record(
                item, context.evidence_by_memory.get(item.id, []), data_gaps
            )
            for item in context.memories
        ],
        evidence=[_evidence_record(item, data_gaps) for item in context.evidence],
        data_gaps=sorted(set(data_gaps)),
        **collections,
    )
    return response, normalized_as_of


@router.get("/{customer_id}/360", response_model=Customer360Response)
def get_customer_360(
    customer_id: UUID,
    session: SessionDependency,
    scope: ScopeDependency,
    as_of: Annotated[datetime, Query()],
) -> Customer360Response:
    response, _ = build_customer_360_response(customer_id, session, scope, as_of)
    return response
