# Project Structure

The current repository uses a backend-first application layout with a small,
independent React operator client:

```text
company-second-brain/
  apps/
    api/
      migrations/
        versions/
      src/company_brain/
        ai/
        api/
        context_engine/
        customer_360/
        db/
        domain/
        entity_resolution/
        ingestion/
        integrations/
          mcp/
          odoo/
        knowledge/
        risk_engine/
      tests/
        acceptance/
        integration/
        unit/
    web/
      src/
        test/
  docs/
  infra/
    docker-compose.yml
  scripts/
  alembic.ini
  pyproject.toml
```

`apps/api/src/company_brain/api/` owns the FastAPI route boundary. Domain and
feature modules remain separate from provider adapters under `integrations/`.
Alembic revisions live under `apps/api/migrations/versions/`, while the root
`alembic.ini` is the supported migration entrypoint. `scripts/run_mcp_scheduler.py`
is the dedicated external MCP schedule dispatcher/recovery worker; it is separate
from the web process and supports bounded direct-database or loopback/API operation.

`apps/web/src/` contains the React 19/Vite operator workbench, typed API client,
CSS, and component tests. It intentionally has no component framework or shared
package workspace. `infra/docker-compose.yml` currently defines PostgreSQL and
MinIO services and their named volumes; there is no separate `infra/minio/`
directory. The repository currently has no `packages/`, general-purpose queue-worker
framework, or nested `scripts/dev`/`scripts/ingest` directories.
