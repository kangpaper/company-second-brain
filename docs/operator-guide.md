# Operator Workbench Guide

The browser workbench provides a bounded, human-reviewed intake lifecycle for
manual documents and generic MCP resources:

```text
manual upload → immutable original → parse/classify → normalized Markdown
→ pending review → explicit promote or reject
→ canonical Document/DocumentVersion/chunks/Source/Evidence → search

workspace scope → test MCP connection → list resources → send to review
→ inspect pending intake → explicit promote or reject
→ search promoted canonical knowledge → inspect sanitized activity
```

The browser is an API client, not a security boundary. FastAPI continues to enforce authentication, organization/workspace membership, writer permissions, endpoint policy, connector bounds, tenant isolation, and audit persistence.

## Prerequisites

- Python 3.11 or newer and `uv`
- Node.js `^20.19.0`, `^22.13.0`, or `>=24.0.0`, plus npm
- Docker with Compose
- An existing opaque API token plus its organization and workspace UUIDs
- Writer membership (`owner`, `admin`, or `editor`) for MCP test/list/intake
- A standards-compatible HTTPS MCP endpoint ending in `/mcp`
- The endpoint hostname present in the server-side `MCP_ALLOWED_HOSTS` setting

The application does not expose a browser sign-up or token-issuance flow. Provision users, memberships, and opaque tokens through the deployment's administrative process before opening the workbench.

## Start the production-like local stack

Run commands from the repository root unless a step says otherwise.

### 1. Install backend dependencies and start PostgreSQL

```bash
uv sync --all-extras
docker compose -f infra/docker-compose.yml up -d postgres
docker compose -f infra/docker-compose.yml exec postgres pg_isready -U brain -d company_brain
```

### 2. Configure and migrate the API

The local Compose credentials below are development-only.

```bash
export DATABASE_URL='postgresql+psycopg://brain:brain@127.0.0.1:5432/company_brain'
export MCP_ALLOWED_HOSTS='approved-mcp.example,another-approved.example'
uv run alembic -c alembic.ini upgrade head
```

`MCP_ALLOWED_HOSTS` is a comma-separated server-side allowlist. An empty value denies every generic MCP endpoint. Do not move this policy into browser code.

### 3. Start FastAPI

```bash
export DATABASE_URL='postgresql+psycopg://brain:brain@127.0.0.1:5432/company_brain'
export MCP_ALLOWED_HOSTS='approved-mcp.example,another-approved.example'
uv run uvicorn company_brain.main:app --app-dir apps/api/src --host 127.0.0.1 --port 8000 --reload
```

Verify:

```bash
curl --fail http://127.0.0.1:8000/health
```

### 4. Install and start the workbench

In a second terminal:

```bash
cd apps/web
npm ci
npm run dev
```

Open <http://127.0.0.1:5173/>. Vite proxies `/api` to `http://127.0.0.1:8000`, so local development does not require a browser CORS exception.

`npm run build` emits static production assets under `apps/web/dist`. This repository does not prescribe a production static host; deploy that directory behind the same origin or route `/api` to FastAPI at the edge.

## Operate document intake and review

Writer membership (`owner`, `admin`, or `editor`) is required for upload, promotion,
and rejection. Review Queue listing is member-readable but remains scoped to the
active organization and workspace.

### Supported inputs and bounds

The browser accepts multiple selections and uploads each file through the same
single-file canonical ingestion endpoint. Supported parsers are:

- UTF-8 Markdown and plain text;
- CSV with non-empty headers;
- XLSX with unique, non-empty worksheet headers;
- DOCX paragraphs and tables;
- text-layer PDF; scanned/image-only PDF has no OCR fallback;
- supplied HTML visible text; scripts, styles, and `noscript` content are ignored.

Each original is limited to 10 MiB and is preserved as an immutable `SourceAsset`
with filename, media type, byte size, SHA-256 content hash, tenant scope, and source
identity. Additional parser bounds are 50 MiB expanded Office content, 2,000,000
extracted characters, 5,000 extraction candidates, and 200 PDF pages. The final
normalized Markdown is separately capped at the canonical 2,000,000-character limit;
an oversized representation fails explicitly without discarding its original asset.

### Upload for review

1. Enter the API token, organization ID, and workspace ID under **Workspace session**.
2. Under **Document intake**, choose one or more files and select **Upload for review**.
3. Inspect the detected business type, confidence, explainable
   `deterministic-rules.v1` reason, warnings, and normalized Markdown preview.

The progress indicator counts completed files in the selected batch; it is not a
byte-level transport progress meter. A parsing or normalization failure remains audited
with its raw original preserved, but it does not enter the successful pending Review
Queue. Unknown or low-confidence material remains `unclassified` and requires a human
choice; it is never promoted automatically.

Phase 16A also allows an API operator to send one allowlisted standard MCP
text/Markdown resource through `POST /api/v1/integrations/mcp/resources/intake` using a
request-scoped token. Phase 16B1 also permits API operators to create/list a saved
connection and intake one resource by connection ID. Configure the local server registry
with `MCP_CREDENTIAL_KEYS=knowledge-prod` and provide the matching secret through
`MCP_CREDENTIAL_KNOWLEDGE_PROD=<secret>`, then send only
`credential_key: "knowledge-prod"` when creating the connection. The API never returns
that key or secret. Results appear in this same Review Queue and are not canonical until
explicitly promoted. Re-intaking an unchanged resource returns the same successful run
without another Review Queue item; changed content or normalization-affecting title/MIME
metadata creates a new pending run. The checkpoint advances only after successful intake,
so operators can safely retry a failed fetch or staging attempt.

Phase 16B3 API operators can create a persistent run for 1–16 explicit unique resource
URIs with `POST /api/v1/integrations/mcp/connections/{connection_id}/sync-runs`. Creation
returns a queued run and does not contact MCP. A writer starts its synchronous bounded
execution with `POST /api/v1/integrations/mcp/sync-runs/{run_id}/execute`; scoped members
can inspect it with `GET /api/v1/integrations/mcp/sync-runs/{run_id}`. Execution uses four
worker sessions and at most three connector attempts per item. `changed` and `unchanged`
items link to the exact successful pending ingestion run; failed items expose only a stable
sanitized error code. Repeating execute after the run is terminal is safe and returns the
same state. A `409` means either a non-expired coordinator owns the run or active item
work still holds an item lease after the coordinator lease expired. Do not force reclaim;
retry after the active executor finishes or the blocking coordinator/item lease expires. The
current web workbench has not yet exposed these actions. The Phase 16B4 discovery/scheduler
surface is API/worker operated; graphical operator sync UI remains later work.

### Discover and schedule saved resources

Call `POST /api/v1/integrations/mcp/connections/{connection_id}/resources/discover` until
`X-MCP-Discovery-Cycle-Complete: true`. Each request fetches one metadata-only page and never
reads or stages content. Enumerate the resulting catalog with
`GET .../connections/{connection_id}/resources?limit=200&offset=0`, increasing `offset` until
an empty page. Availability reflects the most recently completed full cursor cycle, not an
intermediate page.

Create a schedule with `POST .../connections/{connection_id}/schedules` using a 300–86,400
second interval and 1–16 unique currently available catalog URIs. Use
`PATCH .../schedules/{schedule_id}` to change interval/resources or enable state, and
`POST .../schedules/{schedule_id}/run-now` to enqueue an immediate run. These operations do
not contact MCP; the worker executes the resulting Phase 16B3 run. A disabled or currently
invalid connection returns a sanitized conflict/policy response. Invalid due schedules are
advanced and audited as skipped rather than blocking later schedules.

### Run the scheduler worker

Set an exact writer service identity and scope:

```bash
export MCP_SCHEDULER_TOKEN='<server-managed writer token>'
export MCP_SCHEDULER_ORGANIZATION_ID='<organization UUID>'
export MCP_SCHEDULER_WORKSPACE_ID='<workspace UUID>'
export MCP_SCHEDULER_POLL_SECONDS=15  # optional, 1..300
```

Preferred external-service mode calls the API and keeps connector/runtime configuration in
the API process:

```bash
export MCP_SCHEDULER_API_URL='https://api.example.com'
uv run python scripts/run_mcp_scheduler.py --once
```

`MCP_SCHEDULER_API_URL` must use HTTPS outside loopback; redirects are not followed. Omit it
only for direct-database mode, where the worker also requires the same `DATABASE_URL`,
`MCP_ALLOWED_HOSTS`, `MCP_CREDENTIAL_KEYS`, and matching `MCP_CREDENTIAL_<KEY>` registry as
the API. `--once` is recommended for CronJob/supervisor cadence. Without it, the worker polls
continuously at the bounded interval and should be supervised like any other long-running
process. Each cycle prints one JSON object with `dispatched_count`, `attempted_count`, and
`terminal_count`; non-zero process exit or HTTP error is an operational alert. Multiple
workers are supported because PostgreSQL due-slot uniqueness and leases arbitrate ownership.
Do not deploy the controlled `phase13_runtime_app` harness.

### Refresh and decide the Review Queue

Select **Refresh review queue** to load at most 50 newest successful pending runs for
the exact organization/workspace. Failed audited runs and legacy/incomplete successful
rows without the Phase 15 raw asset, classification, and Markdown shape remain available
by detail ID but never appear as actionable Promote/Reject items. Ordering is
deterministic by creation time and run ID.

To promote a run:

1. confirm the classification and normalized Markdown;
2. enter a canonical path ending in `.md` without backslashes;
3. select **Promote**.

Promotion and rejection are complete-successful-pending-only and transactional; direct
transition URLs enforce the same original-asset/classification/Markdown invariant as the
queue. Promotion creates the canonical `Document`, its first immutable
`DocumentVersion`, chunks and links, plus Evidence pointing to the exact ingestion run,
original source asset, version, and content hash. Accepted
candidates and reviewer/time audit fields are recorded. PostgreSQL prevents later
mutation/deletion of the terminal review, terminal candidates, generated chunks, or
its `ingested_document` Evidence/EvidenceLink provenance, and rejects post-promotion
inserts that would extend the sealed chunk/provenance membership. A path conflict
returns a controlled conflict without partially promoting the run.

To reject a run, enter a 3–2,000 character operator reason and select **Reject**.
Rejected candidates, reviewer, time, and reason are recorded. Rejected runs do not
receive canonical document/version IDs. Promoted or rejected runs cannot be decided
a second time.

After promotion, use **Search workspace memory** to confirm the canonical document is
visible within the same tenant scope.

## Controlled MCP developer demo

Use this path only for disposable local QA. It overrides the connector dependency so a synthetic public-looking URL reaches the loopback mock; normal application startup does not contain this override.

The database must already contain a disposable user, writer membership, organization, workspace, and API token.

Terminal 1:

```bash
export PHASE13_MCP_PORT=18131
uv run python scripts/phase13_mock_mcp_server.py
```

Terminal 2:

```bash
export DATABASE_URL='postgresql+psycopg://brain:brain@127.0.0.1:5432/<disposable-database>'
export PHASE13_MCP_PORT=18131
uv run uvicorn scripts.phase13_runtime_app:app --host 127.0.0.1 --port 8000
```

Use these synthetic connector values in the browser:

```text
MCP endpoint:     https://runtime.mcp.example/mcp
MCP access token: runtime-mcp-token
```

Never deploy `scripts.phase13_runtime_app:app`; it is an explicit controlled-test harness.

## Operate the workbench

### 1. Enter workspace scope

Provide:

- **API token** — opaque bearer token for the current user
- **Organization ID** — organization UUID
- **Workspace ID** — workspace UUID within that organization

Every scoped request sends:

```text
Authorization: Bearer <token>
X-Organization-ID: <organization UUID>
X-Workspace-ID: <workspace UUID>
```

Changing browser IDs cannot bypass membership checks. Cross-scope or inaccessible canonical objects remain unavailable.

### 2. Enter the MCP connection

Provide:

- **MCP endpoint** — server-allowlisted HTTPS URL on port 443 with exact `/mcp` path
- **MCP access token** — request-scoped credential for that server

The backend rejects userinfo, query strings, fragments, controls, non-public DNS destinations, redirects, unapproved hosts, and other malformed endpoints before provider traffic.

### 3. Test the connection

Select **Test connection**. A successful result shows the bounded MCP server name and version. A failure shows a generic operator-safe message; provider exceptions, credentials, and sensitive endpoint details are not rendered.

### 4. Load resources

Select **Load resources**. The register displays only the bounded public MCP descriptor:

```text
uri, name, description, mimeType, size
```

Unknown provider fields are discarded. A descriptor without an optional public name
is shown as **Unnamed MCP resource**; the URI is not repurposed as display text.
Credential-bearing or malformed resource URIs fail closed. The generic connector
never exposes arbitrary `tools/call`.

### 5. Send an MCP resource to review

Select **Send to review** for a text or Markdown item. The operation preserves the
exact resource as an immutable SourceAsset, parses/classifies it, creates normalized
Markdown, and adds a pending item to the same Review Queue as manual uploads. It
does not create a canonical Document or Evidence at this point. The resource card
shows the pending ingestion ID rather than canonical identifiers.

Inspect the pending item and choose **Promote** with a canonical `.md` path or
**Reject** with a bounded reason. Only promotion creates the canonical Document,
immutable version/chunks, Source/Evidence provenance, and searchable knowledge.

### 6. Search workspace knowledge

After promotion, enter a query under **Search workspace memory** and select
**Search**. Results come from the current authenticated organization/workspace
only. Use the title, snippet, and Document ID to verify the promoted canonical
record. A merely pending MCP intake must not appear.

### 7. Inspect recent activity

Select **Refresh audit**. The newest-first list requests at most 20 MCP events and displays only:

- operation;
- bounded MCP tool name when present;
- outcome;
- stable error code/message when a failure was sanitized;
- UTC creation time.

The API is member-readable but organization/workspace-scoped. It filters to `provider=mcp`; it does not return endpoint URLs, raw resource URIs, request metadata, credentials, or provider exceptions. Audit rows remain PostgreSQL-enforced append-only.

## Credential handling

The API token and MCP access token live only in React component memory for the open page. The workbench does not write them to:

- `localStorage`;
- `sessionStorage`;
- URL/query parameters;
- the backend database through frontend behavior;
- rendered logs or errors.

Refreshing or closing the page clears the entered values. Browser password managers may offer their own storage independently; follow your organization's browser policy.

Do not paste production credentials into screenshots, issue reports, browser console commands, or controlled QA fixtures.

## Expected states

Each remote operation has an explicit pending state and a sanitized failure state.
Resource, search, audit, and Review Queue sections distinguish **not requested**
from a successful **empty** result. Buttons disable while their operation is
pending; MCP intakes are globally serialized so a second resource cannot start
until the active intake settles.

Editing any workspace or connector field immediately clears derived connection,
resource/intake, review, search, and audit results. Responses started under the previous
field generation are ignored rather than rendered under the new visible scope.

## Troubleshooting

### `401 Unauthorized`

The Bearer header is absent or malformed. Re-enter the provisioned token. Do not put it in the URL.

### `403 Forbidden`

The bearer token is unknown, or the authenticated user lacks membership or a writer role for the requested operation. Audit and search are readable by a scoped member; MCP test/list/intake require a writer.

### `422 Unprocessable Entity`

Check UUIDs, endpoint format, `/mcp` path, resource URI, field lengths, and request shape. Unknown fields are rejected by strict request models.

### Endpoint is rejected before connection

Confirm the hostname is in backend `MCP_ALLOWED_HOSTS`, resolves only to public addresses, uses HTTPS on port 443, and has no userinfo, query, or fragment. Local/private endpoints are intentionally denied by the production connector; use the controlled harness for local QA.

### `502 Bad Gateway`

The connector, response validation, resource read, or cleanup failed. The public error is intentionally generic. Use server-side observability and the sanitized audit outcome; do not expect a raw provider exception in the browser.

### Vite reports API connection failure

Confirm FastAPI is healthy on `127.0.0.1:8000` and the workbench was started with the repository's `apps/web/vite.config.ts` proxy.

## Quality gates

```bash
# Backend
export TEST_DATABASE_URL='postgresql+psycopg://brain:brain@127.0.0.1:5432/<migrated-disposable-database>'
uv run pytest
uv run ruff check .
uv run mypy apps/api/src
uv run python -m compileall -q apps/api/src scripts

# Frontend
cd apps/web
npm test
npm run typecheck
npm run lint
npm run build
npm audit --audit-level=low
```

Run Alembic from the repository root with `alembic.ini`:

```bash
export DATABASE_URL='postgresql+psycopg://brain:brain@127.0.0.1:5432/<database>'
uv run alembic -c alembic.ini current
uv run alembic -c alembic.ini heads
uv run alembic -c alembic.ini check
```

Use a disposable database before testing `downgrade base`; that command removes application data.

## Stop and clean up local services

Stop foreground API, Vite, and controlled MCP processes with `Ctrl+C`. To stop Compose services without deleting the development database volume:

```bash
docker compose -f infra/docker-compose.yml stop postgres
```

Use `docker compose ... down -v` only when you explicitly intend to delete every Compose-managed PostgreSQL and MinIO volume.
