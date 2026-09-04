# Implementation Roadmap and Acceptance Tests

## Gate rule
Do not begin a phase until the previous phase acceptance tests pass against a
frozen exact tree. Before the repository is published, this evidence may come from
the documented local PostgreSQL/runtime/static/browser gates and independent
exact-tree review. After publication, the same gates must also pass in hosted CI;
local success must never be represented as hosted-CI evidence.

## Phase 0 — Architecture Specification — PASSED
Acceptance: docs set exists; explicitly covers non-RAG architecture, Obsidian constraints, Odoo MCP research, domain model, ERD, API, security, roadmap; no code implementation.

Closure evidence (revalidated 2026-09-04): all seven required architecture documents
exist; the 16-file Markdown corpus has zero broken local links. Whole-system
documentation and exact-tree review is repeated at final closure.

## Phase 1 — Core Backend — PASSED
Build Organization, User, Workspace, Entity, Relationship, Event, Source, Document, Memory. Acceptance: migrations pass; tenant isolation tests; entity/relationship CRUD; evidence attach; no Odoo public core types.

Closure evidence (revalidated 2026-09-04): the combined Phase 1–4 acceptance slice
passed 105 tests against PostgreSQL with no skips. It includes core persistence,
tenant authorization/isolation, entity and relationship CRUD, evidence attachment,
database constraints, and the health boundary. Alembic was at `073cc76cf70d (head)`
with no model/schema drift.

## Phase 2 — Knowledge Engine — PASSED
Build Markdown, tags, properties, links, backlinks, search, metadata. Acceptance: Markdown versioning; frontmatter parsed; backlinks generated; full-text search; embeddings behind provider abstraction.

Closure evidence (revalidated 2026-09-04): the no-skip 105-test Phase 1–4 slice
includes Markdown/frontmatter parsing, immutable versioning, chunks, links,
backlinks, scoped full-text search, deterministic embedding-provider abstraction,
and PostgreSQL knowledge constraints.

## Phase 3 — Graph + Canvas — PASSED
Build entity/business graph, relationship graph, canvas, timeline. Acceptance: graph returns customer nodes/edges; traversal filters; timeline merges events; JSON Canvas import/export round-trips supported fields.

Closure evidence (revalidated 2026-09-04): the no-skip 105-test Phase 1–4 slice
includes bounded graph traversal, tenant-safe merged timeline, JSON Canvas
import/export round-trip and validation, plus PostgreSQL canvas scope/path
constraints.

## Phase 4 — Data Ingestion — PASSED
Markdown, CSV, XLSX, PDF, DOCX, HTML, web content. Acceptance: fixture parser tests; sources created; extraction candidates stored; failed parse audited.

Closure evidence (revalidated 2026-09-04): the no-skip 105-test Phase 1–4 slice
includes fixture-backed Markdown/CSV/XLSX/PDF/DOCX/HTML parsing, bounded failure
paths, Source/immutable asset/run/candidate persistence, pending review, explicit
promotion/rejection, canonical provenance, and PostgreSQL lifecycle constraints.

## Phase 5 — Odoo MCP Integration — PASSED
Read-only connector. Acceptance: test connection; tool discovery; read-only blocks writes; mocked search/get/aggregate; retry/rate-limit/error tests.

Closure evidence (2026-08-13): 137 PostgreSQL-backed tests passed; Ruff and mypy passed; Alembic drift and migration round-trip passed; real TCP mock MCP runtime passed; independent adversarial review returned `passed: true` with no security concerns or logic errors.

## Phase 6 — Odoo Data Mapping — PASSED
Map Odoo DTOs. Acceptance: partner/order/invoice/opportunity/project/ticket/employee/product/activity mappings; source refs and external refs created.

Closure evidence (2026-08-13): 172 PostgreSQL-backed tests passed; Ruff and mypy passed; fresh zero-to-head migration, Alembic drift, compatible-data downgrade/re-upgrade, and pre-existing ExternalReference data-cycle checks passed; source-aware tenant constraints were inspected in PostgreSQL; real TCP MCP mapping smoke passed; concurrency stress passed 20/20; independent adversarial review returned `passed: true` with no security concerns or logic errors.

## Phase 7 — Entity Resolution — PASSED
Acceptance: external ref match no duplicate; fuzzy candidates; ambiguity review queue; merge/split audited and relationship-safe.

Closure evidence (2026-08-13): 199 tests passed; focused PostgreSQL entity-resolution suite passed 9 tests; deadlock regression passed 20/20 plus an independent lock-timeout matrix; Ruff and mypy passed; Alembic downgrade/upgrade, single-head lineage, drift check, and fresh zero-to-head migration passed; real TCP resolve/review/decision/merge/split/audit smoke passed; independent adversarial review returned `passed: true` with no security concerns or logic errors.

## Phase 8 — Customer 360 — PASSED
Acceptance: endpoint returns profile, revenue/trend, orders, invoices, opportunities, tickets, meetings, timeline, documents, decisions, AI summary inputs; metrics deterministic; risk evidence present.

Implementation note: Phase 8 is a bounded read model over canonical entities, explicit effective `CUSTOMER_*` relationships, events, approved memories, and the complete EvidenceLink → Evidence → Source provenance chain. Metrics and evidence-backed risk inputs are deterministic at a required timezone-aware `as_of`; malformed or truncated inputs become explicit data gaps; no LLM or composite risk score is used.

Closure evidence (2026-08-13): 225 PostgreSQL-enabled tests passed; focused Customer 360 suite passed 26 tests; Ruff and strict mypy passed; Alembic drift and fresh zero-to-head migration passed with temporary database cleanup; real TCP fresh-PostgreSQL `/360`, `/metrics`, and `/risk` smoke passed with cleanup; `git diff --check` passed. Independent adversarial review `deleg_06430867` returned `passed: true` with no security concerns or logic errors after verifying tenant/workspace scope, historical snapshot semantics, bounded loading, complete aggregate provenance, numeric/date/JSON fail-closed behavior, and the full target → EvidenceLink → Evidence → Source chain.

## Phase 9 — Context Engine — PASSED
Acceptance: “Tình hình ABC thế nào?” maps to `CUSTOMER_360`; schema-valid context; data gaps represented; deterministic snapshots.

Implementation note: Phase 9 provides member-readable `POST /api/v1/context/build` for deterministic Vietnamese/English customer-status intent mapping. It requires an explicit canonical customer ID and timezone-aware `as_of`, reuses the exact Phase 8 bounded/evidenced projection, and returns a versioned envelope with a canonical SHA-256 context hash stable across paraphrases. Its fully anchored allow-only grammar requires named/shorthand labels to match the scoped canonical customer name or sanitized alias. Original Unicode code points are validated before canonical NFD normalization, with controls, compatibility forms, mixed scripts, symbols, and unsupported marks rejected rather than silently removed. Unsupported intents fail closed; no LLM, prose answer, automatic fuzzy entity selection, persistence, or Phase 10 reasoning is included.

Closure evidence (2026-08-13): 234 PostgreSQL-enabled tests passed; focused Context Engine plus Customer 360 verification passed 24 tests; Ruff and strict mypy passed across 43 source files; Alembic drift and fresh zero-to-head migration passed with temporary database cleanup; real TCP fresh-PostgreSQL `/context/build` smoke passed with evidence, data gaps, paraphrase-stable context hashing, and cleanup; exhaustive Unicode injection found no unexpected accepted code points beyond the 15 explicitly allowlisted Latin/Vietnamese combining marks; `git diff --check` passed. Independent adversarial review cycle 5 `deleg_2b3dd253` returned `passed: true` with no security concerns or logic errors after verifying original-code-point validation, NFD semantics, anchored grammar, exact scoped name/alias matching, tenant isolation, error ordering, deterministic hashing, and Phase 8 reuse.

## Phase 10 — AI Orchestrator — PASSED
Acceptance: provider abstraction with mocked providers; LLM not used for metrics; citations included; uncertainty stated; ReasoningRun audited.

Implementation note: Phase 10 reuses the exact Phase 9 deterministic context and
exposes member-readable `POST /api/v1/ai/ask` plus tenant-safe
`GET /api/v1/reasoning-runs/{id}`. Provider output is restricted to bounded
narrative, 1–100 unique context-evidence citations, and required uncertainty;
trusted metrics/signals are copied directly from context. No-evidence contexts
fail closed before provider invocation. Provider construction, metadata,
generation, and grounding failures are sanitized and audited. PostgreSQL enforces
insertion-time actor membership and active-customer scope, materializes citations
into an append-only tenant-composite association with durable Evidence foreign
keys, constrains state/shape, and rejects parent/association mutation or truncation.

Closure evidence (2026-08-21): the exact PostgreSQL-enabled tree passed 266 tests;
the focused Customer 360, Context Engine, and AI Orchestrator slice passed 49 tests;
the ReasoningRun PostgreSQL suite passed 18 tests, while independent review passed
all 54 PostgreSQL integration tests. Ruff, strict mypy across 49 source files, and
`git diff --check` passed. Alembic single-head, downgrade/re-upgrade, drift, and
fresh zero-to-head checks passed. Real TCP Uvicorn verification proved grounded
success, deterministic metric preservation, citation/uncertainty, durable audit
readback, sanitized/audited provider failure without secret leakage, cross-tenant
`404`, and cleanup. Independent blocker-closure review `deleg_c9644e98` returned
three CLEAN verdicts after fresh PostgreSQL HTTP, integrity/concurrency, docs, and
runtime probes.

## Phase 11 — Risk / Insight Engine — PASSED
Acceptance: revenue decline, payment delay, ticket increase, delivery complaints signals; deterministic composite risk; AI cannot override signal values.

Implementation note: Phase 11 adds member-readable `GET /api/v1/customers/{id}/risk-assessment` over the exact Phase 8 historical, tenant-scoped Customer 360 projection. `customer-risk.v1` normalizes evidenced Phase 8 revenue/payment inputs, calculates evidenced ticket-increase and explicit delivery-complaint signals, applies fixed per-type weights with a 0–100 cap, and returns stable severity plus data gaps. Odoo due/open timestamps are normalized at the connector boundary to canonical `due_date`/`opened_at`; complaint classification is never inferred from ticket names. Append-only `entity_revisions`, PostgreSQL direct-write capture triggers, revision-only historical selection, and old-scope tombstones on tenant/workspace transfer prevent later metadata, lifecycle, deletion, or scope changes from rewriting or leaking prior `as_of` assessments. No LLM or provider input exists on the calculation path.

Closure evidence (2026-08-21): the exact PostgreSQL-enabled tree passed 279 tests; the focused fresh PostgreSQL/real-TCP Phase 11 verifier passed 51 tests and proved deterministic score/evidence, authentication, timezone validation, cross-tenant `404`, stable historical replay after direct metadata update and physical deletion, all four scope-transfer quadrants, append-only revision enforcement, and temporary-database cleanup. The verifier reserves an OS-assigned ephemeral loopback port and passed unchanged while an unrelated server occupied its former fixed port `8026`. Ruff, strict mypy across 49 source files, `git diff --check`, source-security scanning, Alembic single-head/current/drift checks, and downgrade/re-upgrade passed. Independent exact-tree blocker-closure review `deleg_fcd2ed13` returned `CLEAN` after focused adversarial tests, migration/static inspection, AI-boundary inspection, and disposable PostgreSQL/API probes; targeted collision-safety review `deleg_f80ae2cf` independently repeated the occupied-port fresh runtime and returned `CLEAN`.

## Phase 12 — Action / Agent — PASSED
Acceptance: write request creates proposal only; permission checks; approval required; delete elevated; execution audited through connector test double.

Implementation note: Phase 12 adds strict tenant/workspace-scoped
`POST /api/v1/action-proposals`, `/{id}/approve`, and `/{id}/execute`. A writer
can only create a pending immutable proposal; standard approval requires a
different admin/owner and delete requires a different owner. Execution is
admin/owner-only through a server-selected adapter, binds retries to the durable
proposal UUID idempotency key, and serializes competing attempts with a
PostgreSQL row lock. The production adapter remains disabled by default. Raw
connector failures are sanitized. PostgreSQL independently enforces requester,
approver, executor, scope, immutable payload, one-way state transitions, and a
unique matching append-only audit for every insert/transition. Proposal deletion
and truncation are rejected; audit update, deletion, and truncation are rejected.

Closure evidence (2026-08-21): the exact PostgreSQL-enabled tree passed 298
tests. The fresh PostgreSQL/real-Uvicorn verifier passed 19 focused tests and
proved zero-to-head migration, downgrade/re-upgrade, no Alembic drift,
proposal/approval/execution over an OS-assigned ephemeral TCP port, cross-scope
`404`, retry `409` without a second execution, append-only audit enforcement,
late-failure process/database cleanup, and zero residual temporary databases.
Ruff, strict mypy across 50 source files, Python compilation, `git diff --check`,
and source-security scans passed. Independent exact-tree review
`deleg_b7d12874` returned two `CLEAN` verdicts after code/security/database,
concurrency stress, runtime, migration, documentation, and cleanup probes.

## Phase 13 — Additional MCP — PASSED
Acceptance: each connector implements common adapter, canonical mapping, read-only mode, permissions, source/evidence refs.

Implementation note: Phase 13 introduced a generic URL-based `ReadOnlyMCPAdapter`
for standard MCP resources. Its writer-scoped connection/list/import routes used a
deny-by-default server hostname allowlist and expose no arbitrary `tools/call`.
Text/Markdown resources map idempotently to canonical Document/version,
MCP-instance Source, Evidence, and EvidenceLink, with request-scoped credentials
and sanitized append-only integration audit. Odoo retains its explicit
provider-specific read-tool mapper while implementing the common MCP lifecycle.

Closure evidence: 25 focused unit/acceptance/PostgreSQL tests pass. The
first independent review found unprojected discovery output and an unguarded
audit `TRUNCATE`; RED→GREEN fixes added canonical projection and forward revision
`187025f68e30`. The second review found C1/Unicode-format controls bypassing the
ASCII-only URI check and documentation ambiguity about unknown fields; shared
Unicode-category rejection now covers transport and API regressions, while docs
state that unknown fields are discarded and malformed/credential-bearing URIs
are rejected. The third review found runtime-verifier cleanup could mask database
creation or later primary failures; database ownership tracking and primary-error
precedence now have focused regressions. The fourth review found process cleanup
could still short-circuit owned-database deletion; reservation, process, database,
and engine cleanup now run independently, collect sanitized errors, and always
attempt owned DB drop. The fifth review found first port reservation ownership was
recorded only after the second acquisition; ownership is now transferred
immediately after each successful reservation. The sixth review found helper-local
`bind()`/`getsockname()` failures could leak the just-created socket before
ownership reached `main`; the helper now closes locally and preserves the original
acquisition failure with sanitized cleanup notes. Six verifier cleanup cases pass.
The exact remediation tree passes 329 PostgreSQL tests, fresh zero-to-head,
downgrade/re-upgrade, and Alembic drift check. Controlled mock MCP plus real
Uvicorn TCP passed on OS-assigned ports with canonical mapping, bounded discovery,
permissions, read-only denial, idempotency, provenance/audit, normal cleanup, and
injected late-failure cleanup. Final independent exact-tree review
`deleg_d6057d5c` returned `CLEAN` with a stable 153-file fingerprint, no blockers,
no non-blocking issues, and no tree drift. Phase 13 is PASSED.

The Phase 13 direct-canonical import route was removed when Phase 16 made reviewed
intake the only public standard-resource ingestion path.

## Phase 14 — Productization & Operator UX — PASSED

Acceptance at Phase 14 closure: an operator could enter an authenticated
organization/workspace scope, test a server-allowlisted generic MCP endpoint, list
bounded resources, import one into canonical knowledge, inspect
Document/Source/Evidence identifiers, search the
same workspace, and read a bounded sanitized activity feed. The browser must not
persist credentials or weaken backend authorization/allowlist policy. Unit/static,
PostgreSQL/migration, real-browser responsive/accessibility, documentation, and
independent exact-tree review gates must pass.

Current behavior supersedes the Phase 14 direct-canonical import: Phase 16 routes
browser MCP resources through immutable intake and the Review Queue, and the
legacy `/resources/import` route is no longer exposed.

Implementation note: `apps/web` is a React 19/TypeScript/Vite workbench with a
mineral-ledger responsive shell, scoped API client, explicit pending/error/empty
states, visible focus, and reduced-motion handling. FastAPI adds member-readable
`GET /api/v1/integration-audits`, projected to operation/tool/outcome/sanitized
error/time only and filtered by exact tenant/workspace/provider. The official
startup and workflow documentation is `docs/operator-guide.md`.

Closure evidence (2026-08-25): the exact PostgreSQL-enabled tree passed 330 tests;
Ruff, strict mypy across 54 source files, Python compilation, Alembic current/head,
drift, and lifecycle migration gates passed. The frontend passed 10 Vitest tests,
TypeScript, ESLint, production build, and npm audit with zero vulnerabilities.
Real browser verification exercised authenticated scope, MCP connection/list/import,
canonical identifiers, search, and sanitized audit readback through Vite, FastAPI,
a controlled MCP server, and PostgreSQL. Desktop/tablet/mobile widths had no
horizontal overflow; keyboard focus, 44-pixel targets, reduced motion, semantic
regions, and a clean browser console were verified. Independent exact-tree
backend/security and Hallmark UI reviews returned `CLEAN`. Whole-repository
documentation review returned `CLEAN` against stable pre/post hash
`946714df3b9e7f651836df3386060a03f5b518769ac273e3d2d11d6eabbc709b` after all
route, model, schema, project-tree, current-versus-target, and runbook drift was
reconciled.

## Phase 15 — Human-reviewed Document Intake — PASSED

Acceptance: an operator can upload bounded supported documents, preserve immutable
original bytes, inspect technical parsing, explainable business classification and
normalized Markdown, list a scoped pending Review Queue, explicitly promote or reject,
and search promoted canonical knowledge. Promotion must create immutable versioned
knowledge plus Source/Evidence provenance; review decisions must be writer-authorized,
audited, pending-only, tenant/workspace-safe, and database-constrained. Backend,
migration, frontend, real-browser, responsive/accessibility, documentation, and
independent exact-tree review gates must pass.

Implementation note: Phase 15A adds immutable `SourceAsset`, extended audited
`IngestionRun` review state, deterministic `deterministic-rules.v1` classification,
provenance-frontmatter Markdown normalization, multipart browser upload, bounded
newest-first Review Queue listing, and row-locked promote/reject transitions.
Promotion transactionally creates the canonical Document, first immutable
DocumentVersion, chunks/links, Evidence/EvidenceLink, and accepts extraction
candidates. Rejection records a bounded operator reason and rejects pending
candidates. The React workbench provides multi-selection upload, batch-completion
progress, classification/warnings, Markdown preview, queue refresh, canonical path,
promote/reject controls, stale-scope guards, explicit states, visible focus,
responsive geometry, bounded wrapping of backend-valid external strings including
provider content and 500-character filenames, and reduced-motion handling. Failed
audited runs and incomplete legacy rows are excluded from the actionable queue;
PostgreSQL seals promoted chunk and ingestion-provenance membership against later
inserts as well as mutation/deletion.

Closure evidence (2026-08-25): the exact PostgreSQL-enabled tree passed 354 backend
tests; Ruff, strict mypy, Python compilation, Alembic single-head/current/drift and
upgrade/downgrade/re-upgrade lifecycle gates passed. The frontend passed 14 Vitest
tests, TypeScript, ESLint, production build, and npm audit with zero vulnerabilities.
Exact runtime verification exercised upload, classification, promotion, canonical
search, failed-run isolation, and direct transition rejection for incomplete legacy
runs. Real-browser checks covered 320/375/414/768/1440 widths, keyboard focus,
44-pixel targets, reduced motion, semantic structure, clean console output, and
contract-maximum unbroken resource/search/500-character filename rendering without
page overflow. Independent exact-tree backend/security and Hallmark UI closure reviews
returned `CLEAN` after reproducing the prior transition and layout probes.

Out of scope for Phase 15A: OCR for image-only PDFs, byte-level upload progress,
persistent scheduled multi-MCP sync/cursors, production AI provider registry/vault,
and the final unified Customer Intelligence Copilot orchestration.

## Phase 16 — Persistent Multi-MCP Sync — PASSED

Acceptance: MCP resources must enter the same immutable raw-asset, classification,
normalized Markdown, and operator Review Queue pipeline as manual uploads. Saved
connections must keep credentials server-owned; sync runs must be tenant/workspace
scoped, bounded, audited, cursor-aware, idempotent for unchanged resources, and safe
under concurrent execution. A failed resource must preserve its accepted raw content
without creating canonical knowledge. Scheduler, migration, runtime, UI, and independent
security/Hallmark review gates must pass before this phase is marked `PASSED`.

### Phase 16A — MCP-to-intake Bridge — PASSED

`POST /api/v1/integrations/mcp/resources/intake` retains the proven
request-scoped credential and endpoint/URI policy, reads one standard text/Markdown
resource, preserves it as an immutable `SourceAsset`, and stages a classified normalized
`IngestionRun` for explicit review. It creates no Document or Evidence before promotion
and reuses the same flush-only intake service as JSON and multipart upload. Remote names
containing Unicode category-C control/format characters are rejected before staging.

Closure evidence (2026-08-25): the exact PostgreSQL-enabled tree passed 359 backend
tests, Ruff, strict mypy, Python compilation, Alembic current/check at single head
`f1fa3b2c77ae`, and clean diff checks. The fresh disposable-database TCP verifier passed
31 focused connector tests, migration lifecycle/drift checks, raw-asset preservation,
pending Review Queue insertion, no pre-promotion canonical creation, and cleanup. After
a RED→GREEN Unicode control-title regression, independent exact-hash backend/security
rereview `deleg_b56e1c0b` returned `CLEAN` and reproduced sanitized rejection with zero
intake/canonical rows plus a URI-hash-only failed audit.

Persistent connection records are introduced by the Phase 16B1 tracer below.
Cursors, changed-resource identity/idempotency, multi-resource concurrency, scheduling,
and operator UI remain pending later Phase 16 work.

### Phase 16B1 — Saved MCP connection tracer — PASSED

Tenant/workspace-scoped `mcp_connections` bind a bounded operator name and
server-owned `credential_key` to the canonical MCP instance `Source`. Secrets are
resolved through a bounded server-owned `MCP_CREDENTIAL_KEYS` allowlist and matching
per-key environment variables; secret values are never parsed from JSON, persisted,
accepted or returned by saved-connection APIs. Operators can create/list connections
and intake one resource by connection ID through the Phase 15 Review Queue pipeline.

Closure evidence (2026-08-25): the exact PostgreSQL-enabled tree passed 374 backend
tests, Ruff, strict mypy, Python compilation, clean diff checks, and Alembic current/check
at single head `263a4ca7e76c`. The fresh disposable-database verifier passed 39 focused
tests plus real TCP saved intake, zero-to-head and downgrade/re-upgrade migration checks,
no drift, and cleanup. RED→GREEN security regressions cover legacy JSON-secret validation
leaks, bounded per-key credential loading, non-reflective 422 responses, reverse Source-type
integrity, and the two-session Source-mutation/connection-insert race. The race probe passed
five consecutive runs with an observable PostgreSQL lock wait, SQLSTATE `23514`, and zero
post-commit mismatches. Independent exact-hash backend/security/concurrency review
`deleg_f346c6bf` returned `CLEAN` and independently verified both transaction orderings.

This tracer does **not** itself provide resource checkpoints, bulk discovery intake,
concurrent leases/retries, scheduling, or operator UI. Resource-level checkpointing is the
separate Phase 16B2 tracer below.

### Phase 16B2 — Saved-resource identity and checkpoint tracer — PASSED

Each bounded MCP resource URI has a persistent identity scoped by exact tenant, workspace,
saved connection, and MCP-instance Source. First-seen or changed content enters the shared
immutable-asset/Review Queue pipeline; exact unchanged representations return the prior
successful run without another asset, run, or queue item. Normalization-affecting title or
MIME changes also create a new snapshot. A PostgreSQL advisory transaction lock held from
before remote read through checkpoint update serializes same-resource observations,
tenant-composite foreign keys bind checkpoints to exact successful runs/content hashes,
and the resource identity tuple is database-immutable. Failed staging or connector cleanup
cannot advance the prior successful checkpoint. No canonical graph is created before
explicit operator promotion.

Closure evidence (2026-08-26): the exact PostgreSQL-enabled tree passed 379 backend
tests with no skips, a six-test focused boundary slice, Ruff, strict mypy across 56 source
files, Python compilation/AST parsing, clean diff checks, and Alembic current/check at
single head `218a74c42de5`. The fresh disposable-database verifier passed 44 focused tests,
real TCP first/unchanged/changed saved-resource intake, zero-to-head and downgrade/re-upgrade
migration checks, no drift, closed ports, and database cleanup. The stale-observation
RED→GREEN regression passed five consecutive parent runs and five independent runs: a
concurrent old/new observation followed by replay of the new content leaves exactly two
assets/runs, one new-hash run, and the checkpoint on the new representation. Independent
exact-hash backend/security/concurrency review `deleg_60b56744` returned `CLEAN`, matched
all 13 supplied hashes before and after review, verified scoped database invariants and
made no repository changes.

Persistent sync-run records, leases/retries and bounded worker execution are delivered by
Phase 16B3 below. Resource discovery, periodic scheduling and autonomous recovery remain
Phase 16B4/later work.

### Phase 16B3 — Persistent sync runs, leases, retries, and bounded execution — PASSED

A writer can persist a queued run for 1–16 explicit unique resource URIs without connector
construction or MCP remote access, inspect it through a member-readable scoped projection,
and explicitly execute it through the saved MCP connection. Runs and ordered items persist
counters, attempts, coordinator/item leases, changed/unchanged/failed outcomes and exact
ingestion targets. PostgreSQL's clock controls claims; active item work blocks coordinator
reclaim, and an expired third attempt terminal-fails without an attempt-four connector call.
Execution uses at most four independent worker sessions and three attempts per connector
failure, reuses the Phase 16B2 lock-before-read checkpoint path, sanitizes terminal errors,
and returns terminal runs idempotently. PostgreSQL composite foreign keys, SHA-256 checks,
identity/transition triggers, append-only delete/truncate guards and a deferred aggregate
trigger enforce scope, policy and terminal consistency independently of the API.

Closure evidence (2026-08-28): the exact PostgreSQL-enabled tree passed 393 backend
tests and a 44-test Phase 16B3 acceptance/PostgreSQL slice; Ruff, strict mypy,
compileall, staged/unstaged diff checks and Alembic single-head/no-drift checks passed.
The fresh disposable-database verifier passed 58 focused tests plus real MCP/Uvicorn
TCP create/read/execute/replay, zero-to-head, downgrade/re-upgrade and cleanup. A
deterministic eight-item tracer observed exactly four simultaneous remote reads, one
active coordinator, eight terminal items and no pre-promotion canonical Document. Both
coordinator lock-wait expiry regressions passed 10/10 runs, the Phase 16B2 advisory-lock
regression passed 5/5, the frontend passed 14 tests plus typecheck/lint/build with zero
reported vulnerabilities, and no disposable runtime databases remained. Independent
exact-hash backend/security/concurrency and runtime/migration/API/documentation reviewers
in `deleg_2d2167c1` matched their supplied manifests before and after review, made no
repository changes and both returned `CLEAN`.

The final operator sync UI remains later work outside Phase 16B4.

### Phase 16B4 — Resource discovery, scheduling, and autonomous recovery — PASSED

Acceptance: a writer can explicitly discover a bounded saved-connection resource catalog
without reading resource content or creating intake/canonical rows; scoped members can list
the persisted catalog. A writer can create, list, enable and disable interval schedules over
1–16 exact catalog resources. PostgreSQL atomically assigns each due schedule slot to at
most one sync run, advances schedule state from the database clock, and prevents duplicate
runs under concurrent scheduler ticks. A bounded scheduler tick and CLI recover queued or
expired scheduled runs, execute due work through the unchanged Phase 16B3 worker path, and
leave active leases untouched. Schedule/resource identity remains tenant/workspace/
connection/source scoped and database-protected; credentials stay server-owned, only
connector `502` failures retry, and no discovery or scheduled execution promotes canonical
knowledge before operator review.

Phase 16B4 excludes an in-process timer daemon, arbitrary cron expressions, webhook/event
triggers, unbounded pagination, automatic canonical promotion and the final operator sync UI.
Deployment invokes the idempotent scheduler CLI periodically. Closure requires migration
round-trip/no-drift, real PostgreSQL concurrency and crash-recovery probes, real MCP/API/CLI
runtime evidence, sanitized audit projections, full regressions and independent exact-tree
backend/security/runtime/documentation `CLEAN` verdicts.

Closure evidence (2026-09-04): the exact 179-file pre-publication tree at manifest
SHA-256 `fb83397dd9bfa2fa1d7f62060e7856ada108c5bfdbb50ca206d8779c80c1188d`
passed 414 backend tests, including required PostgreSQL suites, a 105-test Phase 1–4
PostgreSQL slice with zero skips, and 40/40 focused authority/concurrency stress runs.
Ruff, mypy across 56 source files, strict mypy across the three worker/runtime scripts,
compileall, staged/unstaged diff checks, frontend 15 tests/typecheck/lint/build, npm
audit, and the production Python dependency audit passed with no known
vulnerabilities. The disposable runtime verifier passed 79 tests plus real MCP
TCP/API/worker flows, fresh zero-to-head and downgrade/re-upgrade migrations,
Alembic no-drift, and database cleanup. OpenAPI exposed all seven bounded Phase 16B4
route groups with legacy direct canonical import and generic `tools/call` absent.

A fresh isolated-Chrome workflow proved not-loaded/loading/confirmed-empty Review
Queue states, connection, resource listing, reviewed intake, no canonical search
result before promotion, explicit promotion, and a canonical result afterwards.
The same run passed 375/1440 responsive geometry, six visible mobile navigation
labels, 44-pixel targets, keyboard focus, reduced-motion and zero console errors.
The exact-tree secret scan found zero hits, and all QA listeners, disposable databases,
profiles, scripts and scope credentials were removed after verification. Independent
backend/data/security and Hallmark UI/product/documentation reviewers matched all 179
manifest files before and after review, found no blockers and both returned `CLEAN`.
The earlier v10 review findings for an empty fragment delimiter (`resource#`) and
ambiguous Review Queue not-loaded/loading states were closed through RED→GREEN
regressions before the final v11 review.

## Research Sources

- [S1] Obsidian API: MIT type definitions; concepts: App, Vault, Workspace, MetadataCache, commands/views/settings — https://github.com/obsidianmd/obsidian-api
- [S2] JSON Canvas: MIT open `.canvas` format; top-level `nodes`/`edges`; node types `text`, `file`, `link`, `group` — https://github.com/obsidianmd/jsoncanvas
- [S3] Obsidian Importer: MIT, converts many exports/file formats to durable Markdown; fixture-based tests — https://github.com/obsidianmd/obsidian-importer
- [S4] Obsidian Web Clipper: MIT source; captures/highlights web to durable Markdown; trademarks/marketing assets excluded — https://github.com/obsidianmd/obsidian-clipper
- [S5] Obsidian Maps: MIT, property-driven map view for notes/coordinates — https://github.com/obsidianmd/obsidian-maps
- [S6] Obsidian Sample Plugin: 0BSD TypeScript plugin template/build conventions — https://github.com/obsidianmd/obsidian-sample-plugin
- [S7] Odoo MCP Server app: third-party Odoo 19 module, OPL-1, technical name `mcp_server`, exposes `/mcp`, OAuth 2.1/API key, read-only consent, per-model permissions, audit log — https://apps.odoo.com/apps/modules/19.0/mcp_server
- [S8] `ivnvxd/mcp-server-odoo`: MPL-2.0 local stdio bridge/YOLO mode for Odoo access — https://github.com/ivnvxd/mcp-server-odoo
