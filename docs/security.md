# Security Architecture

## Goals
Multi-tenant isolation, workspace isolation, least privilege, permission-filtered context, restricted writes, auditability.

## Auth and authorization
Phase 1 uses opaque bearer tokens stored only as SHA-256 hashes, resolves an authenticated `Principal`, and verifies a `Membership` for the requested organization/workspace. `member` is read-only; `owner`, `admin`, and `editor` may write. OIDC/SAML replaces token issuance later without changing the principal/policy boundary. Every API query is organization/workspace-scoped and composite foreign keys enforce the same boundary in PostgreSQL.

## Default operation policy
| Operation | Default | Requirement |
|---|---:|---|
| READ | Allowed if user has permission | audit for sensitive reads |
| WRITE | Restricted | writer membership; external writes require durable proposal + distinct approval |
| CREATE | Restricted | writer membership; external creates require durable proposal + distinct approval |
| DELETE | Highly restricted | elevated proposal; distinct owner approval before connector execution |

## Data protection

Current controls include opaque bearer tokens stored only as hashes, mandatory
tenant/workspace query filters, composite tenant foreign keys, request-scoped
connector credentials, sanitized audit/error projections, and no secrets in AI
prompts. Encrypted secret storage, private object storage with signed URLs, and
PostgreSQL row-level security are future controls and are not enabled by the
current migrations.

## AI safety

The provider sees only the permission-filtered, historical Phase 9 context. Its
output type contains narrative, citation IDs, and uncertainty only; deterministic
metrics and signals are copied from trusted backend context and cannot be
overridden by provider output. A successful answer requires 1–100 unique
citations present in the supplied evidence bundle. No-evidence context fails
closed before provider invocation.

Provider construction, metadata access, generation, and grounding validation are
inside one sanitized audit boundary. Raw provider exceptions are neither returned
nor persisted. Every provider attempt records actor, customer, context hash,
provider/model, prompt version, state, and either grounded output or a generic
error. Audit readback is organization/workspace-scoped. PostgreSQL independently
validates actor workspace membership, active-customer scope and state, then
materializes citations into an append-only association with tenant-composite
Evidence foreign keys. Reasoning audit rows and associations reject mutation and
truncation.

Phase 10 is read-only: it has no tool calls, approvals, or external write path.
Phase 11 composite risk is also read-only and deterministic. Phase 12 introduces
an independent proposal-first write boundary; provider output is never executed
directly. A writer can persist only a strict bounded pending proposal. A
different admin/owner must approve it, and delete requires an owner. Execution
uses the exact immutable approved connector/operation/target/parameters and a
server-selected adapter; the production default is disabled. The proposal UUID
is the connector idempotency key and a PostgreSQL row lock serializes concurrent
attempts. Provider/connector exceptions are reduced to stable generic errors.
Cross-scope proposal access returns the same `404` as an unknown ID.

PostgreSQL independently verifies requester/approver/executor membership,
distinct approval, elevated delete authority, legal one-way transitions, and
immutable authorization payload. A deferred constraint trigger requires one
matching audit before every proposal insert/transition. Audit events are unique
per proposal/event and reject update, delete, and truncate; proposals reject
delete/truncate. This makes omission or rewriting of action history fail closed
even when the API service is bypassed.

Mutable entity state consumed by historical Customer 360/risk projections is captured in
append-only `entity_revisions`; database triggers cover direct PostgreSQL entity
writes and reject revision mutation/truncation, so later metadata/lifecycle
changes cannot rewrite prior `as_of` assessments. The risk API
accepts only customer scope and `as_of`, reuses permission-filtered Customer 360,
and has no provider input; an LLM therefore cannot submit or override score,
severity, signal values, or Evidence.

## Document-intake security

Phase 15A upload, promote, and reject routes require writer membership; list/detail
remain member-readable only within the exact authenticated organization/workspace.
The browser sends credentials in headers and never weakens backend authorization.
Multipart and decoded inputs are bounded before parsing. Original bytes are stored in
a tenant/source-composite `SourceAsset`; PostgreSQL rejects cross-source references
and all asset updates/deletes.

Successful complete runs remain pending until an explicit row-locked decision. Queue
listing and direct promote/reject transitions require the same original asset, complete
classification, and normalized Markdown shape. Promotion and rejection record the
authenticated reviewer and UTC time; rejection requires a bounded reason. Database checks prevent pending reviewer/canonical fields and terminal
states without reviewer/time, rejected canonical links, and promoted mismatched
Document/DocumentVersion links. Promotion commits versioned knowledge and
Source/Evidence provenance atomically; integrity/path conflicts roll back and return a
sanitized conflict. Database triggers reject terminal review/candidate changes,
post-promotion chunk or `ingested_document` Evidence/EvidenceLink membership inserts,
and later mutation/deletion of those promoted rows. Parse/normalization failures preserve
the original and a stable audited failure, are excluded from the actionable Review Queue,
and do not expose arbitrary parser exceptions for unexpected failures.

## Odoo security
Use Odoo MCP read-only consent/per-model permissions where possible, but do not rely solely on Odoo; Brain enforces policy and audit.

## Generic MCP security

Phase 13 accepts URL-based MCP endpoints without accepting arbitrary remote
authority. `MCP_ALLOWED_HOSTS` is deny-by-default; HTTPS, port 443, exact `/mcp`,
no userinfo/query/fragment, public DNS validation, address pinning, preserved
Host/TLS SNI, disabled redirects, bounded timeouts, retries, rate limiting, and a
2 MiB streamed response cap apply before remote content reaches canonical data.

The common adapter exposes standard read-only resources only. No generic
`tools/call` API exists, so remote write or unknown tools cannot be selected by a
caller. Import and resource intake require writer membership; credentials are
request-scoped and never persisted. Phase 16B1 saved connections instead persist only a
bounded lowercase `credential_key`; its `SecretStr` is resolved from the server-owned
bounded `MCP_CREDENTIAL_KEYS` allowlist and matching per-key environment variable only
after tenant and endpoint authorization. Secret values are not parsed as JSON settings;
Saved-connection responses omit both key and secret. Missing registry entries fail before
connector construction with generic audited errors. PostgreSQL rejects invalid key formats
and connection rows whose Source is not an MCP instance. Connection writes acquire a
shared lock on the Source row before discriminator validation; the reverse trigger blocks
referenced Source-type changes, closing the concurrent insert-versus-mutation race.
Request-validation responses expose
only stable error type/location metadata and never echo rejected input values. Phase 16A
intake preserves exact
accepted resource content in an immutable tenant/source-scoped asset, stages a pending
run through the shared review pipeline, and creates no canonical knowledge before
operator promotion. Remote MCP names containing Unicode control or format categories,
including bidi overrides, are rejected before SourceAsset/IngestionRun staging.
Credential-bearing/control-character resource URIs fail before
connector construction or public discovery output. Saved-resource checkpoints are scoped
by tenant, workspace, connection and MCP-instance Source. Tenant-composite foreign keys
bind each non-empty checkpoint to the exact successful ingestion run/content hash;
PostgreSQL makes its identity tuple immutable and an advisory transaction lock acquired
before connector construction/read serializes remote observation through checkpoint update
for the same connection/resource, including the absent-row case. This prevents a delayed
older fetch from moving the checkpoint backward and causing a later duplicate snapshot.
Checkpoint state advances only after successful staging and connector cleanup; unchanged
replays do not create another immutable asset or pending run, and failures retain the prior
successful target.

Phase 16B3 persistent sync runs accept only 1–16 explicit unique resource URIs and persist
ordered work items without connector construction or MCP remote access during run creation.
Creation and execution are writer-only; scoped members may read run state. PostgreSQL's
clock is authoritative for coordinator and item leases. Either a second active executor or
still-active item work after coordinator expiry receives `409`; reclaim never overlaps an
active lease. Expired items are reclaimed only while attempt budget remains, and an expired
third attempt becomes a sanitized terminal failure without attempt four. Owner and active-
lease checks after row locking prevent stale workers or coordinators from committing terminal
state. Four independent worker sessions bound parallelism; connector-boundary failures retry
at most three persisted attempts. Public and persisted terminal errors contain stable
codes/messages rather than raw exceptions.
PostgreSQL independently binds run/item scope, connection, MCP Source, retry policy and
successful ingestion target, verifies URI/SHA-256 consistency, freezes identity fields,
constrains legal lifecycle shapes/transitions, checks terminal aggregate counters at
transaction commit, and rejects sync-history deletion or truncation. Terminal replay is
side-effect free and no run creates canonical knowledge before explicit promotion.

Phase 16B4 discovery is writer-triggered and uses only `resources/list`; it never performs
content read, intake, checkpoint mutation, or canonical promotion. A PostgreSQL-clock lease
is committed before connector construction, and owner/expiry authority is revalidated before
catalog/cursor completion. Descriptor pages and cursors are bounded, malformed or
credential-bearing URIs fail closed, and only a completed cursor cycle may mark unseen rows
unavailable. Prior catalog/cursor state survives connector or cleanup failure. Endpoint,
disabled connection, missing credential, active lease, connector failure, and manual-run
rejections produce generic audited outcomes without tokens or raw provider exceptions.

Schedules are tenant/workspace/connection/Source scoped and may select only 1–16 available
catalog resources. Creation, re-enable, mutation, manual run, and due dispatch revalidate
current allowlist, connection, credential, and catalog authority; only the exact
`{"enabled": false}` payload remains available for remediation when authority is unavailable.
Any additional field, including an explicit `null`, requires full authority validation.
Invalid due work is advanced and audited rather than occupying every batch. All B4 audit
endpoint identities are reconstructed through the allowlist-aware sanitizer, so mutable
Source URI userinfo, path, query, fragment, and unapproved hosts are never persisted.
PostgreSQL clock and row locks determine due ownership, and a database unique key permits one
immutable tick per schedule/slot. Catalog/schedule/tick scope and hash guards reject retargets;
tick update/delete/truncate protections preserve history.

Autonomous recovery runs outside the web process with an explicit writer principal and exact
organization/workspace scope. It remains bounded to four runs per cycle and reuses all Phase
16B3 lease, retry, and no-promotion controls. Scheduler API mode sends the bearer token only
to an operator-configured API URL, rejects non-HTTP(S) values, requires HTTPS outside
loopback, sets bounded timeouts, and disables redirects. Secrets are supplied through server
environment/registry and are never accepted in schedule payloads or returned in projections.

Discovery projects only five
bounded MCP descriptor fields, discards every unknown field, and rejects
malformed or credential-bearing resource URIs.
Audit rows store a resource URI hash, not the raw caller
identifier, and connector exceptions are reduced to stable generic errors.
Canonical Document, Source, Evidence, EvidenceLink, and success audit writes
commit atomically only after connector cleanup; failures roll them back before a
sanitized failure audit is committed.
PostgreSQL rejects integration-audit `UPDATE`, `DELETE`, and statement-level
`TRUNCATE`; revision `187025f68e30` adds the final truncate guard without
rewriting the deployed Phase 5 audit migration.

Phase 14 exposes those events through member-readable
`GET /api/v1/integration-audits`. Reads require the exact authenticated
organization/workspace scope, apply a bounded provider filter and limit, and use
newest-first stable ordering. The response schema deliberately omits endpoint
URLs, resource URIs and hashes, request metadata, credentials, and arbitrary
provider details. The browser receives only stable operation/tool/outcome fields,
sanitized error fields, and the event time; it has no direct database path and
cannot weaken append-only enforcement.
