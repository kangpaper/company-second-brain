# Company Second Brain

Business-context and organizational-memory platform. This is a monorepo; all application, infrastructure, packages, scripts, and documentation stay under this root.

## Current product slice

The backend includes the closed foundation through human-reviewed document intake:
bounded browser upload preserves immutable original bytes, parses supported business
formats, applies explainable deterministic classification, normalizes the result to
Markdown, and queues it for explicit promotion or rejection. Promotion creates the
canonical Document, immutable DocumentVersion, chunks, Source/Evidence provenance,
and searchable knowledge in one transaction.

Generic MCP supports scoped connection testing, explicit bounded `resources/list`
catalog discovery, persistent multi-resource sync runs, interval schedules, manual
run-now, and an external recovery worker. Connector reads create Review Queue intake;
they never auto-promote canonical knowledge. Credentials stay server-owned, destinations
require `MCP_ALLOWED_HOSTS`, and scheduler concurrency/recovery authority is enforced by
PostgreSQL leases and immutable history. The production action adapter remains disabled
by default. Production AI provider configuration and the graphical operator sync UI are
not part of this backend slice.

See [`docs/operator-guide.md`](docs/operator-guide.md) for prerequisites,
backend/frontend startup, the controlled MCP developer demo, credential handling,
the end-to-end operator workflow, scheduler worker operation, troubleshooting, and cleanup.

## Local setup
```bash
uv sync --all-extras
docker compose -f infra/docker-compose.yml up -d postgres
export DATABASE_URL='postgresql+psycopg://brain:brain@127.0.0.1:5432/company_brain'
uv run alembic -c alembic.ini upgrade head
uv run uvicorn company_brain.main:app --app-dir apps/api/src --reload
```

In another terminal:

```bash
cd apps/web
npm ci
npm run dev
```

## Quality gates
```bash
# Use an already migrated disposable database; PostgreSQL tests skip without this.
export TEST_DATABASE_URL='postgresql+psycopg://brain:brain@127.0.0.1:5432/<migrated-disposable-database>'
uv run pytest
uv run ruff check .
uv run mypy apps/api/src
cd apps/web
npm test
npm run typecheck
npm run lint
npm run build
```
