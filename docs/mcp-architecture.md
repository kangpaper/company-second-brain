# MCP Integration Architecture

## Principle
MCP is the integration/action layer. Company Brain core consumes normalized DTOs and never depends on raw Odoo/MCP complexity.

## Odoo MCP research facts
Odoo MCP Server exposes native `/mcp` Streamable HTTP JSON-RPC, OAuth 2.1/API key auth, read-only consent, per-model permissions, audit logging, and built-in read/write tools. Read tools include `search_records`, `get_record`, `get_fields`, `list_models`, `aggregate_records`, `list_resource_templates`, `get_current_context`. Write tools exist but are disallowed in initial phases. The Odoo app is OPL-1; the separate bridge `ivnvxd/mcp-server-odoo` is MPL-2.0.

## Connector owns
`/integrations/odoo/`: auth, MCP session, tool discovery, invocation, read-only enforcement, allowlists, Odoo model to DTO mapping, source refs, external ID mapping, rate limiting, retries, sanitized errors, connector audit.

## Read-only Odoo connector policy
Allowed: list models/fields, search, get, aggregate, current context. Disallowed: create/update/delete, post_message, call_model_method, non-allowlisted custom tools.

## Mapping
`res.partner` → Customer/Contact; `sale.order` → Order; `account.move` → Invoice; `crm.lead` → Opportunity; `project.project` → Project; `helpdesk.ticket` → Ticket; `hr.employee` → Employee; products → Product; activities/messages → Event/Email/Meeting candidate.

## Security
Prefer OAuth and read-only consent; API keys should be MCP-only when using `/mcp`. Brain still enforces its own read-only policy and audit logs.

## Phase 5 implementation contract

- Streamable HTTP JSON-RPC supports JSON and SSE responses, MCP session IDs,
  three bounded transient retries and local request rate limiting.
- Connector responses are streamed and rejected above 2 MiB.
- Odoo endpoint hosts come only from server-side `ODOO_ALLOWED_HOSTS`; default
  configuration denies all destinations. HTTPS, port 443 and exact `/mcp` are
  mandatory. Every resolved address must be globally routable; the connector
  pins a validated address while retaining the original hostname for Host/SNI.
- API keys remain request-scoped and are absent from database schema and audit.
- Integration audit rows are tenant/actor constrained and database append-only.
- Phase 5 returns provider payloads only. Canonical DTO mapping and external-ID
  resolution begin in Phase 6; the connector does not create Brain entities.

## Phase 13 generic MCP resource contract

Phase 13 factors a common `ReadOnlyMCPAdapter` over the proven Streamable HTTP
transport and accepts a caller endpoint only when its host appears in
server-controlled `MCP_ALLOWED_HOSTS`. The destination policy remains HTTPS,
exact `/mcp`, port 443, no userinfo/query/fragment, globally routable DNS answers,
address pinning, preserved Host/SNI, no redirects, and bounded transport.

The provider-neutral API supports connection testing, `resources/list`, and
`resources/read`-backed reviewed intake. It deliberately does not expose generic
`tools/call`: MCP servers that only expose custom tools require a future
server-owned profile with an explicit read allowlist and deterministic mapper.
Standard text/Markdown resources enter the immutable-asset and Review Queue
pipeline described below; canonical Document/Evidence provenance is created only
after explicit promotion.
Credentials stay request-scoped; sensitive/malformed resource URIs fail before
connector construction or discovery output. Discovery returns only five bounded
public descriptor fields. Audit metadata stores only a resource URI hash, and
PostgreSQL guards integration audits against update, delete, and truncate.

## Phase 16A MCP-to-intake bridge

`POST /api/v1/integrations/mcp/resources/intake` uses the same endpoint allowlist,
request-scoped token, standard `resources/read`, bounded content, cleanup, and sanitized
audit lifecycle as Phase 13. The former direct-canonical import route is no longer
exposed. The exact UTF-8 resource content is preserved as an immutable
`SourceAsset` under the tenant-scoped MCP-instance Source, then the shared intake service
parses, classifies, and normalizes it into a pending `IngestionRun`. Resource names with
Unicode control/format characters are rejected before staging. Operators must use the
Phase 15 Review Queue to promote or reject it.

## Phase 16B1 saved connection tracer

`mcp_connections` binds a tenant/workspace-scoped operator name and bounded
`credential_key` to the canonical MCP-instance Source. The secret is resolved at request
time from a bounded server-owned key allowlist (`MCP_CREDENTIAL_KEYS`) and the matching
per-key environment variable (for example, key `knowledge-prod` resolves
`MCP_CREDENTIAL_KNOWLEDGE_PROD`) into `SecretStr`; connection APIs never
accept or return it. Creation and one-resource intake revalidate tenant role, exact endpoint
policy, and current registry availability before connector construction. Missing credentials
fail closed with sanitized audit. PostgreSQL constrains credential-key format, tenant/source
ownership and MCP source type in both directions: connection writes take a shared row lock
on the Source before validating its type, while referenced Sources cannot later change away
from `mcp_instance`. This serializes connection insert/update against concurrent Source-type
mutation rather than relying on an unlocked check-then-act trigger.

## Phase 16B2 resource checkpoint tracer

`mcp_resource_checkpoints` gives each bounded resource URI a persistent identity scoped
by exact organization, workspace, saved connection, and MCP-instance Source. The URI
and accepted UTF-8 payload use SHA-256 fingerprints. A checkpoint references the exact
successful `IngestionRun`, its Source, content hash, and status through tenant-composite
foreign keys; PostgreSQL also prevents mutation of the checkpoint identity tuple.

Saved intake fetches and validates the resource before comparing its payload hash and
normalization-affecting title/MIME metadata with the referenced successful run. First-seen
or changed representations enter the existing `stage_intake()` pipeline and advance the
checkpoint only after successful connector cleanup and staging. Exact replays return the
same run without creating another immutable asset, ingestion run, or Review Queue item.
A transaction-scoped PostgreSQL advisory lock derived from tenant, workspace, connection,
and URI hash is acquired before connector construction/read, so remote observation,
absent-row creation, comparison, and checkpoint update execute in one serialized critical
section; the row is then read `FOR UPDATE`. This prevents a slower stale fetch from moving
the checkpoint backward and causing a later duplicate snapshot. Failures preserve the
prior successful checkpoint.

## Phase 16B3 persistent sync-run tracer

`mcp_sync_runs` persists one operator-requested execution boundary for 1–16 explicit,
unique resource URIs; `mcp_sync_items` persists their stable order, URI hash, attempts,
lease and terminal result. Creating a run validates the exact saved connection, endpoint
policy and server credential registry, but performs no connector construction or remote
read. Execution is an explicit writer request, not a scheduler: one coordinator lease
owns the run and a fixed pool of at most four workers uses a separate SQLAlchemy session
per item. Each item reuses the Phase 16B2 lock-before-read checkpoint/intake path.

Connector-boundary `502` failures retry up to three persisted attempts; validation and
intake failures fail the item without blind retry. Item results are `changed`, `unchanged`
or sanitized `failed`; one failed item makes the aggregate run failed. PostgreSQL's clock
is authoritative for claims. Active coordinators and active item work receive `409` rather
than being overlapped by a reclaimed coordinator. Expired item leases are reclaimable while
budget remains; an expired third attempt terminal-fails without attempt four. Owner
compare-and-set completion prevents a stale worker from overwriting a newer claim.
Terminal execute replay returns the stable database projection without another connector
call or ingestion side effect.

PostgreSQL composite foreign keys bind run/item tenant, workspace, connection, MCP Source,
retry policy and successful ingestion target. URI/hash consistency, immutable execution
identity, legal state transitions and deferred terminal counters are database-enforced;
both history tables reject row deletion and statement-level truncation.
No sync result creates canonical knowledge before the existing explicit Review Queue
promotion. Operator sync UI remains later work.

## Phase 16B4 discovery, schedules, and recovery

Explicit discovery uses only standard `resources/list`, one page and at most 200 bounded
descriptors per request. A connection row is the lease/cursor authority: PostgreSQL clock,
an owner token, and a bounded 2,048-character opaque cursor prevent overlapping connector
construction. Page catalog writes and cursor progress commit atomically. Intermediate pages
merge observations without negative inference; only `next_cursor = null` completes a cycle
and marks resources not seen anywhere in that cycle unavailable. The API exposes cycle
completion and paginates the persistent catalog with bounded limit/offset. Discovery does
not read resource content, mutate Phase 16B2 checkpoints, stage intake, or promote knowledge.

`mcp_sync_schedules` stores a bounded 300–86,400 second interval and next due instant;
normalized child rows select 1–16 currently available catalog URIs. Create, re-enable,
resource change, manual run, and dispatch revalidate current endpoint/credential/catalog
authority. A completed later discovery cycle may make a selected resource unavailable;
the schedule remains configured, but dispatch advances its due time, emits a sanitized skip
audit, and creates no tick/run until authority becomes valid again.

Due dispatch locks at most four rows with `FOR UPDATE SKIP LOCKED` and reads PostgreSQL time.
Each accepted slot advances directly to its next future interval, coalescing missed periods.
An immutable `mcp_schedule_ticks` row binds tenant, workspace, schedule, connection, Source,
scheduled instant, trigger, and generated Phase 16B3 run. Database uniqueness on schedule and
scheduled instant prevents duplicate assignment under competing scheduler processes; tick
identity and history reject mutation, deletion, and truncation.

Recovery is a bounded external-worker cycle, not an in-process API timer. It dispatches due
slots and executes at most four queued or safely reclaimable scheduled runs through the
existing Phase 16B3 coordinator/item leases, four-worker cap, three-attempt cap, 502-only
retry policy, checkpoint path, and no-promotion boundary. `scripts/run_mcp_scheduler.py`
supports one-shot supervisor/CronJob operation and bounded continuous polling. It can run
against the database directly or call the authenticated scheduler API; API mode requires
HTTPS except for explicit loopback development endpoints and never follows redirects.

## Research Sources

- [S1] Obsidian API: MIT type definitions; concepts: App, Vault, Workspace, MetadataCache, commands/views/settings — https://github.com/obsidianmd/obsidian-api
- [S2] JSON Canvas: MIT open `.canvas` format; top-level `nodes`/`edges`; node types `text`, `file`, `link`, `group` — https://github.com/obsidianmd/jsoncanvas
- [S3] Obsidian Importer: MIT, converts many exports/file formats to durable Markdown; fixture-based tests — https://github.com/obsidianmd/obsidian-importer
- [S4] Obsidian Web Clipper: MIT source; captures/highlights web to durable Markdown; trademarks/marketing assets excluded — https://github.com/obsidianmd/obsidian-clipper
- [S5] Obsidian Maps: MIT, property-driven map view for notes/coordinates — https://github.com/obsidianmd/obsidian-maps
- [S6] Obsidian Sample Plugin: 0BSD TypeScript plugin template/build conventions — https://github.com/obsidianmd/obsidian-sample-plugin
- [S7] Odoo MCP Server app: third-party Odoo 19 module, OPL-1, technical name `mcp_server`, exposes `/mcp`, OAuth 2.1/API key, read-only consent, per-model permissions, audit log — https://apps.odoo.com/apps/modules/19.0/mcp_server
- [S8] `ivnvxd/mcp-server-odoo`: MPL-2.0 local stdio bridge/YOLO mode for Odoo access — https://github.com/ivnvxd/mcp-server-odoo
