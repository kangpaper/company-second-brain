# Phase 11 Risk / Insight Engine

## Boundary

Phase 11 is a read-only deterministic projection over the exact Phase 8 Customer
360 historical context. It does not query an external system, invoke an LLM, or
accept caller/provider-supplied scores. Tenant/workspace authorization, canonical
customer validation, relationship filtering, `as_of` cutoffs, bounded loading,
and Evidence provenance are inherited from `build_customer_360_response()`.
Mutable canonical entity fields are resolved from append-only system-time
`entity_revisions`: a later ticket/customer metadata or lifecycle update cannot
rewrite a prior `as_of` assessment. PostgreSQL captures revisions for direct
entity writes and rejects revision mutation or truncation.

## API

```http
GET /api/v1/customers/{customer_id}/risk-assessment?as_of=<RFC3339 timestamp>
```

The operation is member-readable. `as_of` is required, must include a timezone
offset, and is normalized to UTC. Unknown, wrong-type, inactive, future-created,
or cross-scope customer IDs return `404` through the Customer 360 boundary.

The response contains:

- `calculation_version="customer-risk.v1"`
- integer `score` in `0..100`
- `severity`: `low`, `moderate`, `high`, or `critical`
- deterministic `signals` with Evidence IDs
- merged Customer 360 and Phase 11 `data_gaps`

## Signals

### Revenue decline

Phase 11 consumes Phase 8 `REVENUE_DECLINE` signals. Phase 8 calculates each
currency independently over two anchored three-month periods and emits the
signal only when every contributing order has Evidence.

### Payment delay

Phase 8 `OVERDUE_PAYMENT` is normalized to Phase 11 `PAYMENT_DELAY` without
changing its deterministic count, severity, or Evidence. Odoo `account.move`
`invoice_date_due` is mapped at the connector boundary to canonical `due_date`.

### Ticket increase

Canonical tickets require a timezone-aware `opened_at`. Odoo
`helpdesk.ticket.create_date` is mapped to this canonical field.

The calculation compares half-open windows anchored to `as_of`:

```text
prior:  [as_of - 60 days, as_of - 30 days)
recent: [as_of - 30 days, as_of)
```

A signal requires a non-empty prior baseline, `recent - prior >= 3`, and
`recent / prior >= 2`. Severity is `high` when the ratio is at least `3` or the
absolute increase is at least `5`; otherwise it is `medium`. Every ticket in
both counts must have Evidence, because both windows determine the result.
Missing baseline, timestamps, future timestamps, or provenance become explicit
data gaps.

### Delivery complaints

Delivery classification is explicit canonical metadata, never keyword inference
from a ticket title. Accepted normalized values are:

```text
complaint_type=delivery
complaint_type=delivery_complaint
```

The signal counts complaints in `[as_of - 90 days, as_of)`. One or two are
`medium`; three or more are `high`. Every counted complaint must have Evidence.
A source connector that cannot supply an explicit trustworthy classification
must leave this field absent rather than fabricate one.

## Composite risk formula

`customer-risk.v1` uses fixed weights:

| Signal | Medium | High |
|---|---:|---:|
| `REVENUE_DECLINE` | 25 | 35 |
| `PAYMENT_DELAY` | 20 | 30 |
| `TICKET_INCREASE` | 15 | 25 |
| `DELIVERY_COMPLAINTS` | 20 | 30 |

Each signal type contributes at most once, using its highest active severity.
This prevents multiple revenue currencies or duplicate detail rows from
multiplying one component. The total is capped at `100`.

Severity thresholds are:

| Score | Severity |
|---:|---|
| `0..24` | `low` |
| `25..49` | `moderate` |
| `50..74` | `high` |
| `75..100` | `critical` |

Signals retain all deterministic detail rows and Evidence IDs even though score
aggregation is capped by signal type.

## Evidence and AI invariants

A Phase 11-derived signal is suppressed if any fact contributing to its count,
ratio, value, or severity lacks Evidence. The service reports a stable data gap
instead of borrowing nearby Evidence or emitting an unsupported risk.

The endpoint has no provider dependency and accepts no narrative, score, signal,
or severity input. Phase 10 providers can return narrative, citations, and
uncertainty only; they cannot write to Phase 11 signal values or composite risk.
Any future AI explanation must copy this deterministic assessment unchanged.
