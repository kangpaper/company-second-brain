from datetime import UTC, datetime
from uuid import uuid4

from company_brain.api.customer_360 import _sanitize_json
from company_brain.customer_360.service import CustomerContext, calculate_metrics
from company_brain.domain.models import Entity, EntityType, Event


def record(entity_type: EntityType, name: str, metadata: dict[str, object]) -> Entity:
    return Entity(
        id=uuid4(),
        organization_id=uuid4(),
        workspace_id=uuid4(),
        entity_type=entity_type,
        name=name,
        normalized_name=name.casefold(),
        metadata_=metadata,
    )


def test_json_sanitizer_handles_cycles_and_key_collisions() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    sanitized_cycle, cycle_changed = _sanitize_json(cyclic)
    sanitized_keys, keys_changed = _sanitize_json({1: "first", "1": "second"})

    assert sanitized_cycle == [None]
    assert cycle_changed is True
    assert sanitized_keys == {"1": "second"}
    assert keys_changed is True


def test_revenue_growth_and_decline_signal_are_deterministic_and_evidenced() -> None:
    customer = record(EntityType.CUSTOMER, "Acme", {})
    prior = record(
        EntityType.ORDER,
        "Prior",
        {
            "state": "sale",
            "amount_total": 1000,
            "currency": "USD",
            "date_order": "2026-03-15T00:00:00+00:00",
        },
    )
    recent = record(
        EntityType.ORDER,
        "Recent",
        {
            "state": "sale",
            "amount_total": 600,
            "currency": "USD",
            "date_order": "2026-06-15T00:00:00+00:00",
        },
    )
    prior_evidence, recent_evidence = uuid4(), uuid4()
    context = CustomerContext(
        customer=customer,
        collections={
            "orders": [prior, recent],
            "invoices": [],
            "opportunities": [],
            "tickets": [],
            "meetings": [],
            "projects": [],
            "documents": [],
            "decisions": [],
        },
        relationships=[],
        events=[],
        memories=[],
        evidence=[],
        evidence_by_entity={prior.id: [prior_evidence], recent.id: [recent_evidence]},
        evidence_by_event={},
        evidence_by_memory={},
        evidence_by_relationship={},
        data_gaps=[],
    )

    metrics, signals, gaps = calculate_metrics(
        context, datetime(2026, 8, 1, tzinfo=UTC)
    )

    assert metrics["revenue_growth_6m"] == {
        "values": [{"currency": "USD", "value": -0.4}],
        "evidence_ids": [str(item) for item in sorted([prior_evidence, recent_evidence])],
        "calculation": "(most recent 3m revenue - prior 3m revenue) / prior 3m revenue",
    }
    assert signals == [
        {
            "type": "REVENUE_DECLINE",
            "severity": "high",
            "currency": "USD",
            "value": -0.4,
            "evidence_ids": [str(item) for item in sorted([prior_evidence, recent_evidence])],
        }
    ]
    assert gaps == ["missing_activity_history"]


def test_overdue_payment_and_inactivity_signals_use_as_of_and_evidence() -> None:
    customer = record(EntityType.CUSTOMER, "Acme", {})
    invoice = record(
        EntityType.INVOICE,
        "INV-1",
        {"payment_state": "not_paid", "due_date": "2026-01-01T00:00:00Z"},
    )
    invoice_evidence, event_evidence = uuid4(), uuid4()
    event = Event(
        id=uuid4(),
        organization_id=customer.organization_id,
        workspace_id=customer.workspace_id,
        subject_entity_id=customer.id,
        event_type="meeting",
        occurred_at=datetime(2026, 2, 1, tzinfo=UTC),
        payload={},
    )
    context = CustomerContext(
        customer=customer,
        collections={
            "orders": [],
            "invoices": [invoice],
            "opportunities": [],
            "tickets": [],
            "meetings": [],
            "projects": [],
            "documents": [],
            "decisions": [],
        },
        relationships=[],
        events=[event],
        memories=[],
        evidence=[],
        evidence_by_entity={invoice.id: [invoice_evidence]},
        evidence_by_event={event.id: [event_evidence]},
        evidence_by_memory={},
        evidence_by_relationship={},
        data_gaps=[],
    )

    _, signals, gaps = calculate_metrics(
        context, datetime(2026, 8, 1, tzinfo=UTC)
    )

    assert signals == [
        {
            "type": "CUSTOMER_INACTIVITY",
            "severity": "high",
            "days": 181,
            "evidence_ids": [str(event_evidence)],
        },
        {
            "type": "OVERDUE_PAYMENT",
            "severity": "medium",
            "count": 1,
            "evidence_ids": [str(invoice_evidence)],
        },
    ]
    assert gaps == []


def test_risk_signal_is_suppressed_when_evidence_is_missing() -> None:
    customer = record(EntityType.CUSTOMER, "Acme", {})
    invoice = record(
        EntityType.INVOICE,
        "INV-1",
        {"payment_state": "not_paid", "due_date": "2026-01-01T00:00:00Z"},
    )
    context = CustomerContext(
        customer=customer,
        collections={
            "orders": [],
            "invoices": [invoice],
            "opportunities": [],
            "tickets": [],
            "meetings": [],
            "projects": [],
            "documents": [],
            "decisions": [],
        },
        relationships=[],
        events=[],
        memories=[],
        evidence=[],
        evidence_by_entity={},
        evidence_by_event={},
        evidence_by_memory={},
        evidence_by_relationship={},
        data_gaps=[],
    )

    _, signals, gaps = calculate_metrics(
        context, datetime(2026, 8, 1, tzinfo=UTC)
    )

    assert signals == []
    assert gaps == [
        "missing_activity_history",
        "missing_risk_evidence:OVERDUE_PAYMENT",
    ]


def test_revenue_total_excludes_future_and_undated_orders_at_as_of() -> None:
    customer = record(EntityType.CUSTOMER, "Acme", {})
    historical = record(
        EntityType.ORDER,
        "Historical",
        {
            "state": "sale",
            "amount_total": 100,
            "currency": "USD",
            "date_order": "2026-07-01T00:00:00Z",
        },
    )
    future = record(
        EntityType.ORDER,
        "Future",
        {
            "state": "sale",
            "amount_total": 900,
            "currency": "USD",
            "date_order": "2026-09-01T00:00:00Z",
        },
    )
    undated = record(
        EntityType.ORDER,
        "Undated",
        {"state": "sale", "amount_total": 500, "currency": "USD"},
    )
    context = CustomerContext(
        customer=customer,
        collections={
            "orders": [historical, future, undated],
            "invoices": [],
            "opportunities": [],
            "tickets": [],
            "meetings": [],
            "projects": [],
            "documents": [],
            "decisions": [],
        },
        relationships=[],
        events=[],
        memories=[],
        evidence=[],
        evidence_by_entity={},
        evidence_by_event={},
        evidence_by_memory={},
        evidence_by_relationship={},
        data_gaps=[],
    )

    metrics, _, gaps = calculate_metrics(
        context, datetime(2026, 8, 1, tzinfo=UTC)
    )

    assert metrics["revenue_total"]["values"] == [
        {"currency": "USD", "value": 100.0}
    ]
    assert f"future_order_excluded:{future.id}" in gaps
    assert f"missing_order_date:{undated.id}" in gaps


def test_aggregate_overflow_becomes_data_gap_not_infinity() -> None:
    customer = record(EntityType.CUSTOMER, "Acme", {})
    orders = [
        record(
            EntityType.ORDER,
            f"Huge {index}",
            {
                "state": "sale",
                "amount_total": 1e308,
                "currency": "USD",
                "date_order": "2026-07-01T00:00:00Z",
            },
        )
        for index in range(2)
    ]
    context = CustomerContext(
        customer=customer,
        collections={
            "orders": orders,
            "invoices": [],
            "opportunities": [],
            "tickets": [],
            "meetings": [],
            "projects": [],
            "documents": [],
            "decisions": [],
        },
        relationships=[], events=[], memories=[], evidence=[],
        evidence_by_entity={}, evidence_by_event={}, evidence_by_memory={},
        evidence_by_relationship={}, data_gaps=[],
    )

    metrics, signals, gaps = calculate_metrics(
        context, datetime(2026, 8, 1, tzinfo=UTC)
    )

    assert metrics["revenue_total"]["values"] == []
    assert signals == []
    assert "revenue_overflow:USD" in gaps


def test_extreme_integer_order_amount_becomes_missing_amount_gap() -> None:
    customer = record(EntityType.CUSTOMER, "Acme", {})
    order = record(
        EntityType.ORDER,
        "Unrepresentable",
        {
            "state": "sale",
            "amount_total": 10**10000,
            "currency": "USD",
            "date_order": "2026-07-01T00:00:00Z",
        },
    )
    context = CustomerContext(
        customer=customer,
        collections={
            "orders": [order], "invoices": [], "opportunities": [],
            "tickets": [], "meetings": [], "projects": [], "documents": [],
            "decisions": [],
        },
        relationships=[], events=[], memories=[], evidence=[],
        evidence_by_entity={}, evidence_by_event={}, evidence_by_memory={},
        evidence_by_relationship={}, data_gaps=[],
    )

    metrics, signals, gaps = calculate_metrics(
        context, datetime(2026, 8, 1, tzinfo=UTC)
    )

    assert metrics["revenue_total"]["values"] == []
    assert signals == []
    assert f"missing_order_amount:{order.id}" in gaps


def test_metadata_datetime_utc_overflow_becomes_date_gaps() -> None:
    customer = record(EntityType.CUSTOMER, "Acme", {})
    order = record(
        EntityType.ORDER,
        "Boundary order",
        {
            "state": "sale",
            "amount_total": 1,
            "currency": "USD",
            "date_order": "9999-12-31T23:59:59-14:00",
        },
    )
    invoice = record(
        EntityType.INVOICE,
        "Boundary invoice",
        {
            "payment_state": "not_paid",
            "due_date": "0001-01-01T00:00:00+14:00",
        },
    )
    context = CustomerContext(
        customer=customer,
        collections={
            "orders": [order], "invoices": [invoice], "opportunities": [],
            "tickets": [], "meetings": [], "projects": [], "documents": [],
            "decisions": [],
        },
        relationships=[], events=[], memories=[], evidence=[],
        evidence_by_entity={}, evidence_by_event={}, evidence_by_memory={},
        evidence_by_relationship={}, data_gaps=[],
    )

    metrics, signals, gaps = calculate_metrics(
        context, datetime(2026, 8, 1, tzinfo=UTC)
    )

    assert metrics["revenue_total"]["values"] == []
    assert metrics["overdue_invoice_count"]["value"] == 0
    assert signals == []
    assert f"missing_order_date:{order.id}" in gaps
    assert f"missing_invoice_due_date:{invoice.id}" in gaps


def test_non_finite_derived_growth_is_omitted_with_explicit_gap() -> None:
    customer = record(EntityType.CUSTOMER, "Acme", {})
    prior = record(EntityType.ORDER, "Prior", {
        "state": "sale", "amount_total": 5e-324, "currency": "USD",
        "date_order": "2026-03-01T00:00:00Z",
    })
    recent = record(EntityType.ORDER, "Recent", {
        "state": "sale", "amount_total": 1e308, "currency": "USD",
        "date_order": "2026-07-01T00:00:00Z",
    })
    context = CustomerContext(
        customer=customer,
        collections={
            "orders": [prior, recent], "invoices": [], "opportunities": [],
            "tickets": [], "meetings": [], "projects": [], "documents": [],
            "decisions": [],
        },
        relationships=[], events=[], memories=[], evidence=[],
        evidence_by_entity={prior.id: [uuid4()], recent.id: [uuid4()]},
        evidence_by_event={}, evidence_by_memory={}, evidence_by_relationship={},
        data_gaps=[],
    )

    metrics, signals, gaps = calculate_metrics(
        context, datetime(2026, 8, 1, tzinfo=UTC)
    )

    assert metrics["revenue_growth_6m"]["values"] == []
    assert signals == []
    assert "revenue_growth_overflow:USD" in gaps


def test_non_finite_growth_from_subtraction_is_omitted() -> None:
    customer = record(EntityType.CUSTOMER, "Acme", {})
    prior = record(EntityType.ORDER, "Prior", {
        "state": "sale", "amount_total": 1e308, "currency": "USD",
        "date_order": "2026-03-01T00:00:00Z",
    })
    recent = record(EntityType.ORDER, "Recent credit", {
        "state": "sale", "amount_total": -1e308, "currency": "USD",
        "date_order": "2026-07-01T00:00:00Z",
    })
    context = CustomerContext(
        customer=customer,
        collections={
            "orders": [prior, recent], "invoices": [], "opportunities": [],
            "tickets": [], "meetings": [], "projects": [], "documents": [],
            "decisions": [],
        },
        relationships=[], events=[], memories=[], evidence=[],
        evidence_by_entity={prior.id: [uuid4()], recent.id: [uuid4()]},
        evidence_by_event={}, evidence_by_memory={}, evidence_by_relationship={},
        data_gaps=[],
    )

    metrics, signals, gaps = calculate_metrics(
        context, datetime(2026, 8, 1, tzinfo=UTC)
    )

    assert metrics["revenue_growth_6m"]["values"] == []
    assert signals == []
    assert "revenue_growth_overflow:USD" in gaps


def test_aggregate_risk_requires_evidence_for_every_contributor() -> None:
    customer = record(EntityType.CUSTOMER, "Acme", {})
    prior = record(EntityType.ORDER, "Prior", {
        "state": "sale", "amount_total": 1000, "currency": "USD",
        "date_order": "2026-03-01T00:00:00Z",
    })
    recent = record(EntityType.ORDER, "Recent", {
        "state": "sale", "amount_total": 100, "currency": "USD",
        "date_order": "2026-07-01T00:00:00Z",
    })
    invoices = [
        record(EntityType.INVOICE, f"INV-{index}", {
            "payment_state": "not_paid", "due_date": "2026-01-01T00:00:00Z"
        })
        for index in range(2)
    ]
    context = CustomerContext(
        customer=customer,
        collections={
            "orders": [prior, recent], "invoices": invoices,
            "opportunities": [], "tickets": [], "meetings": [], "projects": [],
            "documents": [], "decisions": [],
        },
        relationships=[], events=[], memories=[], evidence=[],
        evidence_by_entity={prior.id: [uuid4()], invoices[0].id: [uuid4()]},
        evidence_by_event={}, evidence_by_memory={}, evidence_by_relationship={},
        data_gaps=[],
    )

    _, signals, gaps = calculate_metrics(context, datetime(2026, 8, 1, tzinfo=UTC))

    assert signals == []
    assert "missing_risk_evidence:REVENUE_DECLINE:USD" in gaps
    assert "missing_risk_evidence:OVERDUE_PAYMENT" in gaps
