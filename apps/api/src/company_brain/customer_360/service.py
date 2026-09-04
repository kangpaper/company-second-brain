from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    Entity,
    EntityRevision,
    EntityType,
    Event,
    Evidence,
    EvidenceLink,
    Memory,
    Relationship,
    Source,
)
from company_brain.domain.repositories import TenantScope

RELATIONSHIP_COLLECTIONS: dict[str, tuple[str, EntityType]] = {
    "CUSTOMER_HAS_ORDER": ("orders", EntityType.ORDER),
    "CUSTOMER_HAS_INVOICE": ("invoices", EntityType.INVOICE),
    "CUSTOMER_HAS_OPPORTUNITY": ("opportunities", EntityType.OPPORTUNITY),
    "CUSTOMER_HAS_TICKET": ("tickets", EntityType.TICKET),
    "CUSTOMER_ATTENDED_MEETING": ("meetings", EntityType.MEETING),
    "CUSTOMER_HAS_PROJECT": ("projects", EntityType.PROJECT),
    "CUSTOMER_RELATED_DOCUMENT": ("documents", EntityType.DOCUMENT),
    "CUSTOMER_HAS_DECISION": ("decisions", EntityType.DECISION),
}
COLLECTION_NAMES = tuple(value[0] for value in RELATIONSHIP_COLLECTIONS.values())
COMPLETED_ORDER_STATES = frozenset({"sale", "done", "completed"})
UNPAID_INVOICE_STATES = frozenset({"not_paid", "partial", "overdue"})
MAX_RELATED_RECORDS = 500
MAX_TIMELINE_ITEMS = 100
MAX_MEMORIES = 100
MAX_EVIDENCE_LINKS = 500


EntityState = Entity | EntityRevision


@dataclass(frozen=True)
class CustomerContext:
    customer: EntityState
    collections: dict[str, list[EntityState]]
    relationships: list[Relationship]
    events: list[Event]
    memories: list[Memory]
    evidence: list[Evidence]
    evidence_by_entity: dict[UUID, list[UUID]]
    evidence_by_event: dict[UUID, list[UUID]]
    evidence_by_memory: dict[UUID, list[UUID]]
    evidence_by_relationship: dict[UUID, list[UUID]]
    data_gaps: list[str]


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        return None


def _finite_number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _object_metadata(entity: EntityState, gaps: list[str]) -> dict[str, Any]:
    if isinstance(entity.metadata_, dict):
        return entity.metadata_
    gaps.append(f"invalid_entity_metadata:{entity.id}")
    return {}


def _subtract_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    month_lengths = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return value.replace(
        year=year, month=month, day=min(value.day, month_lengths[month - 1])
    )


def _historical_entity_states(
    session: Session,
    scope: TenantScope,
    entity_ids: set[UUID],
    as_of: datetime,
) -> dict[UUID, EntityState]:
    if not entity_ids:
        return {}
    revisions = list(
        session.scalars(
            select(EntityRevision)
            .where(
                EntityRevision.organization_id == scope.organization_id,
                EntityRevision.workspace_id == scope.workspace_id,
                EntityRevision.entity_id.in_(entity_ids),
                EntityRevision.effective_at <= as_of,
            )
            .order_by(
                EntityRevision.entity_id,
                EntityRevision.effective_at.desc(),
                EntityRevision.revision_id.desc(),
            )
        )
    )
    states: dict[UUID, EntityState] = {}
    deleted_ids: set[UUID] = set()
    for revision in revisions:
        if revision.entity_id in states or revision.entity_id in deleted_ids:
            continue
        if revision.operation == "delete":
            deleted_ids.add(revision.entity_id)
        else:
            states[revision.entity_id] = revision
    return states


def load_customer_context(
    session: Session,
    scope: TenantScope,
    customer_id: UUID,
    as_of: datetime,
) -> CustomerContext | None:
    customer = _historical_entity_states(
        session, scope, {customer_id}, as_of
    ).get(customer_id)
    if (
        customer is None
        or customer.entity_type != EntityType.CUSTOMER
        or customer.lifecycle_status != "active"
    ):
        return None

    relationships = list(
        session.scalars(
            select(Relationship)
            .where(
                Relationship.organization_id == scope.organization_id,
                Relationship.workspace_id == scope.workspace_id,
                Relationship.from_entity_id == customer.id,
                Relationship.relationship_type.in_(RELATIONSHIP_COLLECTIONS),
                Relationship.created_at <= as_of,
                (Relationship.valid_from.is_(None)) | (Relationship.valid_from <= as_of),
                (Relationship.valid_to.is_(None)) | (Relationship.valid_to > as_of),
            )
            .order_by(Relationship.relationship_type, Relationship.id)
            .limit(MAX_RELATED_RECORDS + 1)
        )
    )
    truncated = len(relationships) > MAX_RELATED_RECORDS
    relationships = relationships[:MAX_RELATED_RECORDS]
    target_ids = {relationship.to_entity_id for relationship in relationships}
    by_id = _historical_entity_states(session, scope, target_ids, as_of)
    collections: dict[str, list[EntityState]] = {name: [] for name in COLLECTION_NAMES}
    valid_relationships: list[Relationship] = []
    data_gaps: list[str] = []
    for relationship in relationships:
        collection_name, expected_type = RELATIONSHIP_COLLECTIONS[
            relationship.relationship_type
        ]
        entity = by_id.get(relationship.to_entity_id)
        if (
            entity is None
            or entity.entity_type != expected_type
            or entity.lifecycle_status != "active"
        ):
            data_gaps.append(f"invalid_related_entity:{relationship.id}")
            continue
        valid_relationships.append(relationship)
        collections[collection_name].append(entity)
    for items in collections.values():
        items.sort(key=lambda item: (item.name.casefold(), str(item.id)))
    if truncated:
        data_gaps.append("related_records_truncated")

    valid_target_ids = {
        relationship.to_entity_id for relationship in valid_relationships
    }
    related_ids = {customer.id, *valid_target_ids}
    events = list(
        session.scalars(
            select(Event)
            .where(
                Event.organization_id == scope.organization_id,
                Event.workspace_id == scope.workspace_id,
                Event.subject_entity_id.in_(related_ids),
                Event.occurred_at <= as_of,
                Event.created_at <= as_of,
            )
            .order_by(Event.occurred_at.desc(), Event.id.desc())
            .limit(MAX_TIMELINE_ITEMS + 1)
        )
    )
    if len(events) > MAX_TIMELINE_ITEMS:
        events = events[:MAX_TIMELINE_ITEMS]
        data_gaps.append("timeline_truncated")

    memories = list(
        session.scalars(
            select(Memory)
            .where(
                Memory.organization_id == scope.organization_id,
                Memory.workspace_id == scope.workspace_id,
                Memory.subject_entity_id == customer.id,
                Memory.review_status == "approved",
                Memory.created_at <= as_of,
            )
            .order_by(Memory.created_at.desc(), Memory.id.desc())
            .limit(MAX_MEMORIES + 1)
        )
    )
    if len(memories) > MAX_MEMORIES:
        memories = memories[:MAX_MEMORIES]
        data_gaps.append("memory_truncated")

    evidence_by_entity: dict[UUID, list[UUID]] = defaultdict(list)
    evidence_by_event: dict[UUID, list[UUID]] = defaultdict(list)
    evidence_by_memory: dict[UUID, list[UUID]] = defaultdict(list)
    evidence_by_relationship: dict[UUID, list[UUID]] = defaultdict(list)
    relationship_ids = {item.id for item in valid_relationships}
    event_ids = {item.id for item in events}
    memory_ids = {item.id for item in memories}
    link_predicates = []
    if related_ids:
        link_predicates.append(EvidenceLink.entity_id.in_(related_ids))
    if relationship_ids:
        link_predicates.append(EvidenceLink.relationship_id.in_(relationship_ids))
    if event_ids:
        link_predicates.append(EvidenceLink.event_id.in_(event_ids))
    if memory_ids:
        link_predicates.append(EvidenceLink.memory_id.in_(memory_ids))
    evidence_ids: set[UUID] = set()
    if link_predicates:
        links = list(
            session.scalars(
                select(EvidenceLink)
                .where(
                    EvidenceLink.organization_id == scope.organization_id,
                    EvidenceLink.workspace_id == scope.workspace_id,
                    EvidenceLink.created_at <= as_of,
                    or_(*link_predicates),
                )
                .order_by(EvidenceLink.evidence_id, EvidenceLink.id)
                .limit(MAX_EVIDENCE_LINKS + 1)
            )
        )
        if len(links) > MAX_EVIDENCE_LINKS:
            links = links[:MAX_EVIDENCE_LINKS]
            data_gaps.append("evidence_truncated")
        for link in links:
            evidence_ids.add(link.evidence_id)
            if link.entity_id is not None:
                evidence_by_entity[link.entity_id].append(link.evidence_id)
            elif link.event_id is not None:
                evidence_by_event[link.event_id].append(link.evidence_id)
            elif link.memory_id is not None:
                evidence_by_memory[link.memory_id].append(link.evidence_id)
            elif link.relationship_id is not None:
                evidence_by_relationship[link.relationship_id].append(link.evidence_id)
    evidence = (
        list(
            session.scalars(
                select(Evidence)
                .join(Source, Source.id == Evidence.source_id)
                .where(
                    Evidence.organization_id == scope.organization_id,
                    Evidence.workspace_id == scope.workspace_id,
                    Evidence.id.in_(evidence_ids),
                    Evidence.created_at <= as_of,
                    Source.organization_id == scope.organization_id,
                    Source.workspace_id == scope.workspace_id,
                    Source.created_at <= as_of,
                )
                .order_by(Evidence.id)
                .limit(MAX_EVIDENCE_LINKS)
            )
        )
        if evidence_ids
        else []
    )
    loaded_evidence_ids = {item.id for item in evidence}

    def loaded_only(mapping: dict[UUID, list[UUID]]) -> dict[UUID, list[UUID]]:
        return {
            target_id: sorted(
                evidence_id
                for evidence_id in evidence_ids_for_target
                if evidence_id in loaded_evidence_ids
            )
            for target_id, evidence_ids_for_target in mapping.items()
            if any(
                evidence_id in loaded_evidence_ids
                for evidence_id in evidence_ids_for_target
            )
        }

    return CustomerContext(
        customer=customer,
        collections=collections,
        relationships=valid_relationships,
        events=events,
        memories=memories,
        evidence=evidence,
        evidence_by_entity=loaded_only(evidence_by_entity),
        evidence_by_event=loaded_only(evidence_by_event),
        evidence_by_memory=loaded_only(evidence_by_memory),
        evidence_by_relationship=loaded_only(evidence_by_relationship),
        data_gaps=sorted(set(data_gaps)),
    )


def calculate_metrics(
    context: CustomerContext, as_of: datetime
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    gaps = list(context.data_gaps)
    revenue_by_currency: dict[str, float] = defaultdict(float)
    recent_revenue: dict[str, float] = defaultdict(float)
    prior_revenue: dict[str, float] = defaultdict(float)
    growth_evidence: dict[str, set[UUID]] = defaultdict(set)
    growth_contributors: dict[str, set[UUID]] = defaultdict(set)
    revenue_evidence: set[UUID] = set()
    overflow_currencies: set[str] = set()
    recent_start = _subtract_months(as_of, 3)
    prior_start = _subtract_months(as_of, 6)
    for order in context.collections["orders"]:
        metadata = _object_metadata(order, gaps)
        state = metadata.get("state")
        if state not in COMPLETED_ORDER_STATES:
            continue
        amount = _finite_number(metadata.get("amount_total"))
        currency = metadata.get("currency")
        if amount is None:
            gaps.append(f"missing_order_amount:{order.id}")
            continue
        if not isinstance(currency, str) or not currency.strip():
            gaps.append(f"missing_order_currency:{order.id}")
            continue
        normalized_currency = currency.strip().upper()
        order_date = _parse_datetime(metadata.get("date_order"))
        if order_date is None:
            gaps.append(f"missing_order_date:{order.id}")
            continue
        if order_date >= as_of:
            gaps.append(f"future_order_excluded:{order.id}")
            continue
        revenue_by_currency[normalized_currency] += amount
        if not math.isfinite(revenue_by_currency[normalized_currency]):
            overflow_currencies.add(normalized_currency)
            gaps.append(f"revenue_overflow:{normalized_currency}")
        order_evidence = context.evidence_by_entity.get(order.id, [])
        revenue_evidence.update(order_evidence)
        if recent_start <= order_date:
            recent_revenue[normalized_currency] += amount
            growth_contributors[normalized_currency].add(order.id)
            growth_evidence[normalized_currency].update(order_evidence)
        elif prior_start <= order_date < recent_start:
            prior_revenue[normalized_currency] += amount
            growth_contributors[normalized_currency].add(order.id)
            growth_evidence[normalized_currency].update(order_evidence)

    overdue_count = 0
    overdue_contributors: set[UUID] = set()
    overdue_evidence: set[UUID] = set()
    for invoice in context.collections["invoices"]:
        metadata = _object_metadata(invoice, gaps)
        payment_state = metadata.get("payment_state")
        if payment_state not in UNPAID_INVOICE_STATES:
            continue
        due_date = _parse_datetime(metadata.get("due_date"))
        if due_date is None:
            gaps.append(f"missing_invoice_due_date:{invoice.id}")
            continue
        if due_date < as_of:
            overdue_count += 1
            overdue_contributors.add(invoice.id)
            overdue_evidence.update(context.evidence_by_entity.get(invoice.id, []))

    growth_values: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    for currency in sorted(set(recent_revenue) | set(prior_revenue)):
        if currency in overflow_currencies or not math.isfinite(
            recent_revenue[currency]
        ) or not math.isfinite(prior_revenue[currency]):
            gaps.append(f"revenue_overflow:{currency}")
            continue
        baseline = prior_revenue[currency]
        if baseline <= 0:
            gaps.append(f"missing_revenue_growth_baseline:{currency}")
            continue
        growth = (recent_revenue[currency] - baseline) / baseline
        if not math.isfinite(growth):
            gaps.append(f"revenue_growth_overflow:{currency}")
            continue
        growth = round(growth, 10)
        growth_values.append({"currency": currency, "value": growth})
        if growth <= -0.2:
            signal_evidence = sorted(growth_evidence[currency])
            fully_evidenced = all(
                context.evidence_by_entity.get(entity_id)
                for entity_id in growth_contributors[currency]
            )
            if signal_evidence and fully_evidenced:
                signals.append(
                    {
                        "type": "REVENUE_DECLINE",
                        "severity": "high" if growth <= -0.3 else "medium",
                        "currency": currency,
                        "value": growth,
                        "evidence_ids": [str(item) for item in signal_evidence],
                    }
                )
            else:
                gaps.append(f"missing_risk_evidence:REVENUE_DECLINE:{currency}")

    if context.events:
        latest_event = max(context.events, key=lambda item: (item.occurred_at, item.id))
        latest_at = latest_event.occurred_at
        if latest_at.tzinfo is None or latest_at.utcoffset() is None:
            latest_at = latest_at.replace(tzinfo=UTC)
        inactivity_days = (as_of - latest_at.astimezone(UTC)).days
        if inactivity_days > 90:
            signal_evidence = context.evidence_by_event.get(latest_event.id, [])
            if signal_evidence:
                signals.append(
                    {
                        "type": "CUSTOMER_INACTIVITY",
                        "severity": "high" if inactivity_days > 180 else "medium",
                        "days": inactivity_days,
                        "evidence_ids": [str(item) for item in signal_evidence],
                    }
                )
            else:
                gaps.append("missing_risk_evidence:CUSTOMER_INACTIVITY")
    else:
        gaps.append("missing_activity_history")

    if overdue_count:
        fully_evidenced = all(
            context.evidence_by_entity.get(entity_id)
            for entity_id in overdue_contributors
        )
        if overdue_evidence and fully_evidenced:
            signals.append(
                {
                    "type": "OVERDUE_PAYMENT",
                    "severity": "high" if overdue_count >= 3 else "medium",
                    "count": overdue_count,
                    "evidence_ids": [str(item) for item in sorted(overdue_evidence)],
                }
            )
        else:
            gaps.append("missing_risk_evidence:OVERDUE_PAYMENT")
    signals.sort(key=lambda item: (str(item["type"]), str(item.get("currency", ""))))

    metrics = {
        "revenue_total": {
            "values": [
                {"currency": currency, "value": revenue_by_currency[currency]}
                for currency in sorted(revenue_by_currency)
                if currency not in overflow_currencies
            ],
            "evidence_ids": [str(item) for item in sorted(revenue_evidence)],
            "calculation": "sum completed order amount_total grouped by currency",
        },
        "overdue_invoice_count": {
            "value": overdue_count,
            "as_of": as_of,
            "evidence_ids": [str(item) for item in sorted(overdue_evidence)],
            "calculation": "count unpaid invoices with due_date before as_of",
        },
        "revenue_growth_6m": {
            "values": growth_values,
            "evidence_ids": [
                str(item)
                for item in sorted(
                    {evidence for items in growth_evidence.values() for evidence in items}
                )
            ],
            "calculation": "(most recent 3m revenue - prior 3m revenue) / prior 3m revenue",
        },
    }
    return metrics, signals, sorted(set(gaps))
