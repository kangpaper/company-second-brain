# API Specification

REST JSON for MVP with OpenAPI. Evidence-bearing fields include `evidence_ids` or `source_refs`.

Requests require a bearer `Authorization` header plus `X-Organization-ID` and
`X-Workspace-ID`. The principal must have a Membership for that exact scope.
`member` is read-only; writer roles are `owner`, `admin`, and `editor`.

## Organization/workspace scope

The current API does not expose organization or workspace CRUD routes. Scope IDs are
provisioned out of band and supplied through `X-Organization-ID` and
`X-Workspace-ID` on every scoped request.

## Entities
`GET /api/v1/entities?type=&q=`, `POST /api/v1/entities`, `GET/PATCH /api/v1/entities/{id}`, `GET /api/v1/entities/{id}/relationships`, `GET /api/v1/entities/{id}/evidence`.

Phase 1 also exposes `DELETE /api/v1/entities/{id}`, `POST /api/v1/relationships`, `GET/PATCH/DELETE /api/v1/relationships/{id}`, and `POST /api/v1/entities/{id}/evidence`. Timeline is implemented through the Phase 3 bounded `GET /api/v1/timeline` route described below.

## Entity resolution

Phase 7 exposes:

- `POST /api/v1/entity-resolution/resolve` — writer-only deterministic resolution.
  Exact source-instance external references take precedence, followed by a
  unique exact identifier or normalized-name match. Fuzzy/ambiguous results
  return `202` and persist a bounded candidate snapshot; they never auto-merge.
- `GET /api/v1/entity-resolution/cases` — member-readable, tenant-scoped review
  queue.
- `POST /api/v1/entity-resolution/cases/{case_id}/decision` — writer-only
  `match` or `dismiss`; a match must select an active entity from the immutable
  candidate snapshot and each case can be decided only once.
- `POST /api/v1/entity-resolution/merge` — writer-only merge of two active,
  same-type entities in the authenticated workspace.
- `POST /api/v1/entity-resolution/merges/{merge_id}/split` — writer-only,
  fail-closed reversal of an active merge journal.

Merge atomically transfers external references, graph edges, events, memories,
and entity evidence links. Edges that would become self-loops and their evidence
links are retained in the reversible journal rather than left in the active
graph. Split validates that journal-owned records have not changed before it
restores them. Merge fails with `409` rather than combining duplicate typed
graph edges, and split preserves later alias history non-destructively. Match,
dismiss, merge, and split write a PostgreSQL-enforced append-only audit record.

## Knowledge

Phase 2 exposes:

- `GET/POST /api/v1/documents`
- `PATCH /api/v1/documents/{id}` — creates an append-only version
- `GET /api/v1/documents/{id}/versions`
- `GET /api/v1/documents/{id}/links`
- `GET /api/v1/documents/{id}/backlinks`
- `GET /api/v1/search?q=&tag=` — PostgreSQL full-text search over current versions

Semantic/vector search is deferred; Phase 2 provides the provider-independent
embedding contract and citation-ready Markdown chunk coordinates only.

## Graph/canvas

Phase 3 exposes:

- `GET /api/v1/graph?root_entity_id=&depth=&relationship_type=&type=&node_limit=&edge_limit=` —
  tenant-safe entity/relationship graph with traversal depth bounded to `0..3`,
  node limit bounded to `1..500`, edge limit bounded to `0..1000`, and a
  `truncated` response flag when a budget is reached.
- `GET /api/v1/timeline?root_entity_id=&depth=&event_type=&from_at=&to_at=&limit=&offset=` —
  newest-first events for a root entity and its bounded reachable graph
  neighborhood. `limit` is bounded to `1..200`; date filters must include a
  timezone offset and are normalized to UTC.
- `POST /api/v1/canvases/import` — validates and persists supported JSON Canvas
  1.0 fields; writer role required.
- `GET /api/v1/canvases/{id}/export` — exports canonical JSON Canvas with
  optional fields omitted when absent.

Canvas import rejects duplicate node/edge IDs, dangling edge endpoints,
unsupported node types/fields, invalid sides/end markers, and invalid file
subpaths. Graph layout is stored separately from canonical business entities
and relationships.

## Data ingestion

Phase 15A extends the Phase 4 parser foundation with a reviewed canonicalization
workflow:

- `POST /api/v1/ingestions` — writer-only ingestion of base64-encoded content.
- `POST /api/v1/ingestions/upload` — writer-only single-file browser multipart
  upload through the same ingestion pipeline. The UI may invoke this endpoint
  sequentially for a multi-file selection.
- `POST /api/v1/integrations/mcp/resources/intake` — writer-only Phase 16A bridge
  that reads one allowlisted standard text/Markdown resource with a request-scoped
  token and stages it in this same Review Queue pipeline. It preserves a raw asset
  and creates no canonical Document/Evidence before promotion. Remote titles containing
  Unicode control/format characters are rejected before any intake rows are staged.
- `POST /api/v1/integrations/mcp/connections` — writer-only Phase 16B1 creation of a
  tenant-scoped saved connection. It accepts a bounded name, exact allowlisted endpoint,
  and server-owned `credential_key`; it never accepts or returns the secret.
- `GET /api/v1/integrations/mcp/connections` — authenticated tenant-scoped listing,
  capped at 100 records. It exposes endpoint metadata and `credential_configured`, but
  omits both key and secret.
- `POST /api/v1/integrations/mcp/connections/{connection_id}/resources/intake` —
  writer-only one-resource intake using the saved endpoint and server-resolved secret.
  The first observation, changed UTF-8 content, or changed title/MIME metadata returns
  `201` after staging a new immutable asset and pending Review Queue run. An exact replay
  returns the prior successful run with `200` and creates no asset, run, or Review Queue
  item. A per-connection/resource checkpoint advances only after successful staging; no
  canonical Document/Evidence is created before explicit promotion.
- `POST /api/v1/integrations/mcp/connections/{connection_id}/sync-runs` — writer-only
  creation of a persistent Phase 16B3 run for 1–16 unique explicit resource URIs. It
  persists a queued run plus ordered items with a fixed four-worker/three-attempt policy,
  revalidates the current saved endpoint and credential registry, and does not construct a
  connector or issue an MCP remote request.
- `GET /api/v1/integrations/mcp/sync-runs/{sync_run_id}` — member-readable exact-scope
  projection of persistent run counters and ordered item status/attempt/result fields.
  Unknown and cross-scope IDs both return `404`.
- `POST /api/v1/integrations/mcp/sync-runs/{sync_run_id}/execute` — writer-only explicit
  execution. A coordinator lease and per-item leases protect claim ownership; up to four
  separate database sessions process resources concurrently through the Phase 16B2
  checkpoint/intake path. Connector `502` failures retry up to three attempts, terminal
  errors remain sanitized, and any failed item makes the run failed. Database-clock leases
  make either an active coordinator or still-active item work return `409`; an expired
  coordinator is reclaimable only when no item lease remains active. Expired items are
  reclaimed while attempt budget remains; an expired third attempt terminal-fails without
  a fourth connector call. Replaying a terminal run returns its stable projection without
  connector or ingestion side effects.
- `POST /api/v1/integrations/mcp/connections/{connection_id}/resources/discover` —
  writer-only, one bounded standard MCP `resources/list` page per request. It persists at
  most 200 sanitized descriptors and an opaque cursor of at most 2,048 characters. The
  `X-MCP-Discovery-Cycle-Complete` response header is `false` while another page remains
  and `true` after final-page reconciliation. Intermediate pages never mark prior catalog
  entries unavailable; only a completed cycle may do so. Discovery never calls
  `resources/read`, stages intake, advances resource checkpoints, or creates canonical
  knowledge. Current endpoint, enabled state, credentials, and the one-owner discovery
  lease are enforced; rejected and failed attempts are sanitized and audited.
- `GET /api/v1/integrations/mcp/connections/{connection_id}/resources?limit=200&offset=0`
  — member-readable exact-scope catalog projection. `limit` is 1–200 and `offset` is
  0–100,000, allowing catalogs larger than one provider page to be enumerated without
  silent truncation. Rows expose only descriptor fields plus completed-cycle availability.
- `POST /api/v1/integrations/mcp/connections/{connection_id}/schedules` and `GET` on the
  same collection — writer-only creation and member-readable listing (at most 100),
  respectively. Create accepts strict `{name, interval_seconds, resource_uris}` with a
  1–200 character normalized name, interval 300–86,400 seconds, and 1–16 unique resources
  currently available in that connection's catalog. Creation revalidates endpoint policy,
  enabled state, and server-side credentials and performs no connector call.
- `PATCH /api/v1/integrations/mcp/schedules/{schedule_id}` — writer-only strict update of
  `enabled`, bounded interval, and/or 1–16 catalog resource URIs. Empty updates fail.
  Only the exact payload `{ "enabled": false }` may bypass unavailable connection
  authority so an operator can stop future dispatch. Any additional field—including an
  explicitly supplied `null` interval or resource list—and all other mutations, including
  re-enable, revalidate current connection authority.
- `POST /api/v1/integrations/mcp/schedules/{schedule_id}/run-now` — writer-only creation of
  one queued Phase 16B3 run and immutable manual schedule tick. It revalidates schedule,
  connection, credentials, and selected catalog resources but performs no network work.
- `POST /api/v1/integrations/mcp/scheduler/dispatch-due` — writer-only bounded dispatch of
  at most four due schedules using PostgreSQL clock and `FOR UPDATE SKIP LOCKED`.
  `(schedule_id, scheduled_for)` uniqueness makes one due slot produce at most one run.
  Missed intervals coalesce to the next future slot. Invalid schedules are advanced and
  audited as skipped so they cannot permanently starve valid work.
- `POST /api/v1/integrations/mcp/scheduler/run-cycle` — writer-only bounded recovery of at
  most four queued or safely reclaimable scheduled runs through the unchanged Phase 16B3
  execution path. It returns `{attempted_count, terminal_count, sync_run_ids}`; dispatch
  returns `{dispatched_count, sync_run_ids}`. Active coordinator/item leases are not
  overlapped, terminal replay is stable, and connector `502` remains the only retryable
  provider failure. `409`, `422`, and `502` remain sanitized conflict, validation/policy,
  and connector-boundary responses.
- `GET /api/v1/ingestions?review_status=pending&limit=50` — member-readable,
  tenant/workspace-scoped, bounded, deterministic newest-first Review Queue. Status
  accepts `pending`, `promoted`, or `rejected`; limit is 1–200.
- `GET /api/v1/ingestions/{run_id}` — tenant-safe run detail with ordered,
  persisted extraction candidates.
- `POST /api/v1/ingestions/{run_id}/promote` — writer-only, row-locked pending
  transition. A strict `.md` path is required; success creates canonical
  Document/version/chunks/links and Source/Evidence provenance transactionally.
- `POST /api/v1/ingestions/{run_id}/reject` — writer-only, row-locked pending
  transition with a required 3–2,000 character operator reason.

Supported media types are Markdown/plain text, CSV, XLSX, PDF, DOCX, HTML, and
captured web HTML. Decoded/uploaded input is bounded to 10 MiB. Web ingestion
accepts caller-provided HTML plus its URL as provenance; the backend does not fetch
arbitrary URLs.

Every decoded or uploaded original creates a tenant-scoped `Source` and immutable
`SourceAsset` containing original bytes, filename/media type, byte size, and SHA-256
hash. Successful runs retain parsing metadata and candidates, explainable
`deterministic-rules.v1` business classification, provenance-frontmatter normalized
Markdown, and a `pending` review state. Parse or normalization failures commit a
failed audited run that still points to the preserved original, then return `422`
with its `run_id`; failed runs and legacy/incomplete successful rows without the raw
asset, complete classification, and normalized Markdown remain detail-readable but are
excluded from the actionable Review Queue. Malformed base64 is rejected before any
source or asset is created.

Parser work is bounded to 5,000 candidates, 2,000,000 extracted characters, 200 PDF
pages, and 50 MiB expanded OOXML content. Normalized Markdown has its own canonical
2,000,000-character bound; oversized normalization fails explicitly instead of being
silently truncated. Scanned PDFs without an extractable text layer are audited as
failures; OCR is not silently fabricated.

Promotion/rejection requires a complete successful pending run with its original asset,
classification fields, and normalized Markdown, and records reviewer/time; rejection
additionally records the operator reason. Only promotion may attach canonical
canonical document/version IDs. PostgreSQL tenant-composite foreign keys, review
shape checks, and mutation/membership triggers enforce source/asset/reviewer/canonical
scope, terminal review/candidate immutability, append-only chunks with sealed promoted
version membership, and immutable promoted `ingested_document` Evidence/EvidenceLinks
whose post-promotion membership cannot be extended. Original assets reject
update/delete. A decided run cannot be promoted, rejected, rewritten, or deleted.

## Customer 360

Phase 8 exposes member-readable, tenant/workspace-scoped views:

- `GET /api/v1/customers/{id}/360?as_of=<RFC3339 timestamp>` — bounded
  profile and business-context bundle.
- `GET /api/v1/customers/{id}/metrics?window=6m&as_of=<RFC3339 timestamp>` —
  deterministic metrics projection. `6m` is the only supported window.
- `GET /api/v1/customers/{id}/risk?as_of=<RFC3339 timestamp>` — deterministic,
  evidence-backed risk inputs.

`as_of` is required and must contain a timezone offset. Customer lookup requires
an active canonical `customer` entity in the authenticated tenant/workspace;
cross-scope and wrong-type IDs return `404`. Related records are included only
through explicit canonical `CUSTOMER_*` relationships. Customer, relationship,
related-entity, event, memory, source, evidence-link, and evidence `created_at`
values are all bounded by `as_of`; historical reads cannot include records or
provenance created later. Evidence is accepted only when its tenant/workspace-
scoped Source also existed by the snapshot cutoff.
Only correctly typed targets of valid canonical relationships may contribute
related events or evidence; malformed targets cannot leak peripheral context.
Relationship effectiveness uses the half-open interval `[valid_from, valid_to)`:
a null endpoint is open, an exact start is included, and an exact end is excluded.
Collections and timeline queries are bounded and stably ordered. The response
returns at most 500 relationships, 100 timeline events, 100 approved memories,
and 500 evidence links/records. Sentinel rows produce explicit
`related_records_truncated`, `timeline_truncated`, `memory_truncated`, or
`evidence_truncated` gaps instead of silent truncation. Malformed canonical
relationships whose target is missing, inactive, or the wrong type are omitted
from both typed collections and the relationship projection.

The `360` bundle includes profile, orders, invoices, opportunities, tickets,
meetings, projects, documents, decisions, timeline, approved memories,
relationships, evidence records, metrics, signals, and explicit `data_gaps`.
Revenue totals and six-month growth remain grouped by currency; currencies are
never silently combined. Revenue growth compares `[as_of-6m, as_of-3m)` with
`[as_of-3m, as_of)`. Overdue counts use the caller-pinned `as_of` timestamp.
Invalid or missing amount, currency, date, activity history, baseline, or
provenance is represented as a deterministic data gap rather than fabricated.

Phase 8 risk inputs are `REVENUE_DECLINE`, `OVERDUE_PAYMENT`, and
`CUSTOMER_INACTIVITY`. A signal is returned only when it has explicit
`EvidenceLink` provenance for every contributing fact in an aggregate; partial
provenance suppresses the signal and returns a `missing_risk_evidence:*` gap.
Aggregate or derived-growth overflow is omitted and reported as
`revenue_overflow:<currency>` or `revenue_growth_overflow:<currency>` instead of
emitting non-finite JSON. Invalid or out-of-range metadata timestamps are treated
as missing date inputs. Non-finite relationship or memory confidence values are
serialized as `null` and reported with `invalid_*_confidence:<id>` data gaps.
Every untrusted JSON projection (business attributes, timeline payloads, memory
structured facts, and evidence pointers) is recursively normalized: non-finite
numbers, unsupported values, excessive integer magnitudes, and content deeper
than 32 levels become `null` with a stable record-level `invalid_*:<id>` gap;
container cycles are detected and non-string/colliding object keys are reported
as changed. Dict-typed roots are required; malformed customer/entity metadata,
event payloads, memory facts, and evidence pointers fail closed to `{}` with
explicit record-level gaps before metrics or response validation. Malformed
customer aliases are filtered to strings and reported as
`invalid_customer_aliases:<id>`. Phase 8 does not calculate a composite risk
score and does not invoke an LLM.

## Context Engine

Phase 9 exposes one member-readable deterministic context operation:

- `POST /api/v1/context/build` with strict JSON
  `{question, customer_id, as_of}`. `question` is bounded to 2,000 characters,
  `customer_id` is an explicit canonical entity ID, and `as_of` must be
  timezone-aware. Extra fields are rejected.

The current read-only grammar is fully anchored. Generic forms must explicitly
identify `customer`/`khách hàng`; named forms and the shorthand “Tình hình
<customer label> thế nào?” must match the exact normalized canonical customer
name or a sanitized alias from the authenticated historical projection.
Prefixes/suffixes, write/action requests, negations, and unrelated subjects fail
closed with `422`. Original code points are validated before canonical NFD
decomposition; compatibility folding is not used. The allowlist covers ASCII
letters/digits/punctuation/spacing, Latin-script letters, and an explicit set of
Vietnamese/Latin combining diacritics. Controls, format characters, non-Latin or
mixed-script letters, symbols, emoji, variation selectors, enclosing/script-
specific marks, and unsupported combining marks are rejected rather than
silently deleted. Phase 9 never asks an LLM to guess intent. The canonical
customer remains subject to all Phase 8 tenant/workspace, type, lifecycle,
historical, bounds, provenance, and malformed-data controls; inaccessible IDs
return `404`.

The response contains `schema_version=customer_360.v1`, intent, canonical
entity reference, normalized UTC `as_of`, the exact Phase 8 Customer 360 bundle,
and a SHA-256 `context_hash` over canonical strict JSON. Question text is not
hashed, so paraphrases for the same intent/entity/snapshot produce the same
hash; snapshot or context changes produce a different hash. Phase 9 does not
persist context snapshots, call an LLM, generate prose, or resolve ambiguous
names automatically.

## AI Orchestrator

Phase 10 exposes two member-readable, tenant/workspace-scoped operations:

- `POST /api/v1/ai/ask` with the same strict `{question, customer_id, as_of}`
  request contract as Context Engine. Extra fields are rejected.
- `GET /api/v1/reasoning-runs/{id}` for tenant-safe audit readback; unknown or
  cross-scope IDs return `404`.

Example ask request:

```json
{
  "question": "Tình hình khách hàng ABC hiện tại thế nào?",
  "customer_id": "00000000-0000-0000-0000-000000000000",
  "as_of": "2026-08-14T00:00:00Z"
}
```

Successful answers contain a required uncertainty statement and 1–100 unique
citation UUIDs that must belong to Evidence in the exact Context Engine bundle.
Metrics and deterministic signals are copied from trusted context and are not
accepted from the provider. No-evidence context fails closed with `422` and an
audited `insufficient_evidence` run. Invalid grounding or provider-boundary
failure returns sanitized `502` and persists a failed run without raw exception
text. See `docs/ai-orchestrator.md` for provider and database invariants.

## Risk / Insight Engine

Phase 11 exposes one member-readable deterministic operation:

- `GET /api/v1/customers/{id}/risk-assessment?as_of=<RFC3339 timestamp>`

It reuses the exact Phase 8 historical Customer 360 projection and returns
`calculation_version=customer-risk.v1`, a bounded integer score, severity,
evidence-backed revenue-decline/payment-delay/ticket-increase/delivery-complaint
signals, and explicit data gaps. Score components use fixed weights and each
signal type contributes at most once at its highest active severity. Ticket
increase compares anchored 30-day windows; delivery complaints use explicit
canonical classification in a 90-day window. Missing contributing Evidence
suppresses the derived signal. The endpoint has no provider input or LLM path,
so AI cannot override deterministic signal values. See
`docs/risk-insight-engine.md` for the complete formula and invariants.

## Action proposals

Phase 12 exposes three tenant/workspace-scoped write operations:

- `POST /api/v1/action-proposals` — writer-only creation of a durable `pending`
  proposal. This operation never invokes a connector.
- `POST /api/v1/action-proposals/{id}/approve` — explicit approval by a different
  `admin` or `owner`; delete proposals require an `owner`.
- `POST /api/v1/action-proposals/{id}/execute` — `admin`/`owner` execution of an
  `approved` proposal through the server-selected connector dependency.

The request is strict JSON with `connector=odoo`, an allowlisted operation
(`update_record` or `delete_record`), bounded `{model, record_id}` target,
bounded scalar `parameters.values`, and a non-empty reason. Unknown fields,
credential-like field names, malformed identifiers, oversized values, update
requests without values, and delete requests with values are rejected before
persistence or connector construction. Delete proposals receive
`risk_level=elevated`; updates receive `standard`.

The approved connector, operation, target, parameters, reason, risk, requester,
and tenant scope are immutable. Execution passes the proposal UUID as the
connector idempotency key and PostgreSQL row locking serializes concurrent
attempts; stale/repeated execution returns `409` without a second connector
call. The production default connector is disabled and fails closed until a
server-controlled adapter is configured. Connector exceptions return a generic
`502`, persist only stable sanitized error fields, and never expose raw provider
text. Cross-scope IDs are indistinguishable from unknown IDs and return `404`.
Every committed proposal/approval/execution lifecycle transition has exactly
one append-only matching `action_audits` event. Rejected authorization and stale
state attempts do not create lifecycle events.

## Integrations/actions

Phase 5 exposes writer-only, tenant-audited read operations:

- `POST /api/v1/integrations/odoo/test-connection`
- `POST /api/v1/integrations/odoo/discover-tools`
- `POST /api/v1/integrations/odoo/search`
- `POST /api/v1/integrations/odoo/records/{model}/{record_id}`
- `POST /api/v1/integrations/odoo/aggregate`

Credentials are request-scoped `SecretStr` values and are never persisted.
Endpoints must use HTTPS, an allowlisted server-configured hostname and exact
`/mcp` path on port 443, without userinfo, query or fragment. Redirects are
disabled. DNS resolution rejects any non-global address and the checked public
address is pinned for the connection while preserving the original host for
HTTP Host and TLS SNI, preventing validation/connect DNS rebinding.
Search limits are bounded to 200 records, offsets to 10,000, domains to 50
validated clauses/16 KiB, and connector responses to 2 MiB. Only the fixed
read-tool allowlist can reach Odoo; write and unknown tools are denied locally.
Every attempted connector operation persists an append-only audit record.

Phase 6 adds one writer-only canonical mapping operation:

- `POST /api/v1/integrations/odoo/map/{model}/{record_id}`

The route accepts only the explicit Phase 6 model registry (`res.partner`,
`sale.order`, `account.move`, `crm.lead`, `project.project`,
`helpdesk.ticket`, `hr.employee`, `product.product`, and `mail.activity`). It
calls only the read-only `get_record` MCP tool with a server-owned field
allowlist. The returned record ID must exactly match the requested ID before
strict mapping can create or update a canonical entity.

Canonical `Entity`, `Source`, and `ExternalReference` writes and the successful
integration audit commit atomically after connector cleanup. Mapping, connector,
or cleanup failures roll back canonical writes before the sanitized failure audit
is committed. External identity is workspace- and source-instance-scoped and idempotent by
`source_id`, Odoo model, and record ID. Request-scoped credentials and arbitrary
provider fields are never persisted.

Phase 13 adds a provider-neutral URL-based MCP resource connector:

- `POST /api/v1/integrations/mcp/test-connection`
- `POST /api/v1/integrations/mcp/resources/list`
- `POST /api/v1/integrations/mcp/resources/intake`

All routes require writer membership and strict `{endpoint_url, access_token}`
input; intake additionally requires a bounded `resource_uri`. Endpoints must use
HTTPS, port 443, exact `/mcp`, and a hostname from server-controlled
`MCP_ALLOWED_HOSTS`. Userinfo, query strings, fragments, control characters,
private/non-global DNS answers, redirects, and unapproved hosts fail closed.
Credentials remain request-scoped. Credential-bearing or control-character
resource URIs are rejected before connector construction, and audits retain only
a SHA-256 resource-identity hash rather than the raw caller URI.

The public generic connector exposes only standard MCP `initialize`,
`resources/list`, and `resources/read`; there is no caller-controlled
`tools/call` route. Resource discovery revalidates every provider URI and returns
only bounded `uri`, `name`, `description`, `mimeType`, and `size` fields; malformed
or credential-bearing resource URIs fail closed, while every unrecognized
provider field is discarded rather than reflected.
Text or Markdown resources up to 2 MiB enter the shared immutable-asset and
pending Review Queue pipeline. They do not create a canonical `Document`,
`Evidence`, or `EvidenceLink` until explicit operator promotion. The former
direct-canonical `/resources/import` route is no longer exposed.
Connector and cleanup failures return sanitized `502` responses, roll back
intake writes, and persist only stable generic audit errors.

Phase 14 adds the bounded operator activity feed:

- `GET /api/v1/integration-audits?provider=mcp&limit=20&offset=0`

The route is member-readable and requires the same bearer token plus organization
and workspace headers as every scoped operation. `provider` defaults to `mcp` and
is bounded to 50 characters; `limit` is bounded to `1..100` and defaults
to 20; `offset` is bounded to `0..10000` and defaults to 0. Rows are filtered by
the authenticated organization/workspace and provider, ordered newest-first with
stable ID tie-breaking, and only then paginated by offset/limit. The projection contains
only `id`, `provider`, `operation`, `tool_name`, `outcome`, stable sanitized
`error_code`/`error_message`, and UTC `created_at`. It does not expose endpoint
URLs, resource URIs or hashes, request metadata, credentials, or raw provider
exceptions.
