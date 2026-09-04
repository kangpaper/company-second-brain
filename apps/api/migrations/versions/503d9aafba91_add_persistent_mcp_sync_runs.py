"""Add persistent MCP sync runs and leased resource work items.

Revision ID: 503d9aafba91
Revises: 218a74c42de5
Create Date: 2026-08-26 09:42:02.442441
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "503d9aafba91"
down_revision: str | Sequence[str] | None = "218a74c42de5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_LIFECYCLE = """
(status = 'queued' AND completed_count = 0
 AND lease_owner IS NULL AND lease_expires_at IS NULL
 AND started_at IS NULL AND finished_at IS NULL)
OR
(status = 'running' AND lease_owner IS NOT NULL
 AND lease_expires_at IS NOT NULL
 AND started_at IS NOT NULL AND finished_at IS NULL)
OR
(status = 'succeeded' AND completed_count = requested_count
 AND failed_count = 0
 AND lease_owner IS NULL AND lease_expires_at IS NULL
 AND started_at IS NOT NULL AND finished_at IS NOT NULL)
OR
(status = 'failed' AND completed_count = requested_count
 AND failed_count BETWEEN 1 AND requested_count
 AND lease_owner IS NULL AND lease_expires_at IS NULL
 AND started_at IS NOT NULL AND finished_at IS NOT NULL)
"""

_ITEM_LIFECYCLE = """
(status = 'queued'
 AND lease_owner IS NULL AND lease_expires_at IS NULL AND finished_at IS NULL
 AND ingestion_run_id IS NULL AND content_hash IS NULL AND ingestion_status IS NULL
 AND error_code IS NULL AND error_message IS NULL)
OR
(status = 'running' AND attempt_count >= 1
 AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
 AND started_at IS NOT NULL AND finished_at IS NULL
 AND ingestion_run_id IS NULL AND content_hash IS NULL AND ingestion_status IS NULL
 AND error_code IS NULL AND error_message IS NULL)
OR
(status IN ('changed', 'unchanged') AND attempt_count >= 1
 AND lease_owner IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL
 AND ingestion_run_id IS NOT NULL AND content_hash IS NOT NULL
 AND ingestion_status = 'succeeded'
 AND error_code IS NULL AND error_message IS NULL)
OR
(status = 'failed' AND attempt_count >= 1
 AND lease_owner IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL
 AND ingestion_run_id IS NULL AND content_hash IS NULL AND ingestion_status IS NULL
 AND error_code IS NOT NULL AND error_message IS NOT NULL)
"""


def _create_column_indexes(table: str, columns: tuple[str, ...]) -> None:
    for column in columns:
        op.create_index(op.f(f"ix_{table}_{column}"), table, [column], unique=False)


def upgrade() -> None:
    op.create_table(
        "mcp_sync_runs",
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("changed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unchanged_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_concurrency", sa.Integer(), server_default="4", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("lease_owner", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "workspace_id", "id"),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "id",
            "connection_id",
            "source_id",
            "max_attempts",
            name="uq_mcp_sync_runs_item_scope_policy",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_mcp_sync_run_status",
        ),
        sa.CheckConstraint(
            "requested_count BETWEEN 1 AND 16",
            name="ck_mcp_sync_run_requested_count",
        ),
        sa.CheckConstraint(
            "max_concurrency BETWEEN 1 AND 4",
            name="ck_mcp_sync_run_max_concurrency",
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 3",
            name="ck_mcp_sync_run_max_attempts",
        ),
        sa.CheckConstraint(
            "completed_count >= 0 AND changed_count >= 0 "
            "AND unchanged_count >= 0 AND failed_count >= 0 "
            "AND completed_count = changed_count + unchanged_count + failed_count "
            "AND completed_count <= requested_count",
            name="ck_mcp_sync_run_counts",
        ),
        sa.CheckConstraint(_RUN_LIFECYCLE, name="ck_mcp_sync_run_lifecycle"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_mcp_sync_runs_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_mcp_sync_runs_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            ["users.organization_id", "users.id"],
            name="fk_mcp_sync_runs_created_by_user",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "connection_id", "source_id"],
            [
                "mcp_connections.organization_id",
                "mcp_connections.workspace_id",
                "mcp_connections.id",
                "mcp_connections.source_id",
            ],
            name="fk_mcp_sync_runs_connection_source",
        ),
    )
    _create_column_indexes(
        "mcp_sync_runs",
        (
            "connection_id",
            "created_by_user_id",
            "lease_expires_at",
            "organization_id",
            "source_id",
            "status",
            "workspace_id",
        ),
    )

    op.create_table(
        "mcp_sync_items",
        sa.Column("sync_run_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("resource_uri", sa.String(length=2048), nullable=False),
        sa.Column("resource_uri_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("lease_owner", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingestion_run_id", sa.Uuid(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("ingestion_status", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=255), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "workspace_id", "id"),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "sync_run_id",
            "ordinal",
            name="uq_mcp_sync_items_ordinal",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "sync_run_id",
            "resource_uri_hash",
            name="uq_mcp_sync_items_resource",
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 15",
            name="ck_mcp_sync_item_ordinal",
        ),
        sa.CheckConstraint(
            "resource_uri_hash ~ '^[0-9a-f]{64}$'",
            name="ck_mcp_sync_item_uri_hash",
        ),
        sa.CheckConstraint(
            "resource_uri_hash = "
            "encode(sha256(convert_to(resource_uri, 'UTF8')), 'hex')",
            name="ck_mcp_sync_item_uri_hash_matches",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'changed', 'unchanged', 'failed')",
            name="ck_mcp_sync_item_status",
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 3 "
            "AND attempt_count BETWEEN 0 AND max_attempts",
            name="ck_mcp_sync_item_attempts",
        ),
        sa.CheckConstraint(_ITEM_LIFECYCLE, name="ck_mcp_sync_item_lifecycle"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_mcp_sync_items_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_mcp_sync_items_workspace",
        ),
        sa.ForeignKeyConstraint(
            [
                "organization_id",
                "workspace_id",
                "sync_run_id",
                "connection_id",
                "source_id",
                "max_attempts",
            ],
            [
                "mcp_sync_runs.organization_id",
                "mcp_sync_runs.workspace_id",
                "mcp_sync_runs.id",
                "mcp_sync_runs.connection_id",
                "mcp_sync_runs.source_id",
                "mcp_sync_runs.max_attempts",
            ],
            name="fk_mcp_sync_items_run_scope_policy",
        ),
        sa.ForeignKeyConstraint(
            [
                "organization_id",
                "workspace_id",
                "source_id",
                "ingestion_run_id",
                "content_hash",
                "ingestion_status",
            ],
            [
                "ingestion_runs.organization_id",
                "ingestion_runs.workspace_id",
                "ingestion_runs.source_id",
                "ingestion_runs.id",
                "ingestion_runs.content_hash",
                "ingestion_runs.status",
            ],
            name="fk_mcp_sync_items_successful_ingestion",
        ),
    )
    _create_column_indexes(
        "mcp_sync_items",
        (
            "connection_id",
            "ingestion_run_id",
            "lease_expires_at",
            "organization_id",
            "source_id",
            "status",
            "sync_run_id",
            "workspace_id",
        ),
    )
    op.create_index(
        "ix_mcp_sync_items_tenant_run_status_ordinal",
        "mcp_sync_items",
        ["organization_id", "workspace_id", "sync_run_id", "status", "ordinal"],
        unique=False,
    )

    op.execute(
        """
        CREATE FUNCTION enforce_mcp_sync_run_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF ROW(
                NEW.organization_id, NEW.workspace_id, NEW.id,
                NEW.connection_id, NEW.source_id, NEW.created_by_user_id,
                NEW.requested_count, NEW.max_concurrency, NEW.max_attempts
            ) IS DISTINCT FROM ROW(
                OLD.organization_id, OLD.workspace_id, OLD.id,
                OLD.connection_id, OLD.source_id, OLD.created_by_user_id,
                OLD.requested_count, OLD.max_concurrency, OLD.max_attempts
            ) THEN
                RAISE EXCEPTION 'MCP sync run identity and policy are immutable'
                    USING ERRCODE = '23514';
            END IF;

            IF OLD.status IN ('succeeded', 'failed') THEN
                RAISE EXCEPTION 'Terminal MCP sync runs are immutable'
                    USING ERRCODE = '23514';
            ELSIF OLD.status = 'queued'
                AND NEW.status NOT IN ('queued', 'running') THEN
                RAISE EXCEPTION 'Invalid MCP sync run transition'
                    USING ERRCODE = '23514';
            ELSIF OLD.status = 'running'
                AND NEW.status NOT IN ('running', 'succeeded', 'failed') THEN
                RAISE EXCEPTION 'Invalid MCP sync run transition'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_mcp_sync_run_transition
        BEFORE UPDATE ON mcp_sync_runs
        FOR EACH ROW
        EXECUTE FUNCTION enforce_mcp_sync_run_transition()
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_mcp_sync_item_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF ROW(
                NEW.organization_id, NEW.workspace_id, NEW.id,
                NEW.sync_run_id, NEW.connection_id, NEW.source_id,
                NEW.ordinal, NEW.resource_uri, NEW.resource_uri_hash,
                NEW.max_attempts
            ) IS DISTINCT FROM ROW(
                OLD.organization_id, OLD.workspace_id, OLD.id,
                OLD.sync_run_id, OLD.connection_id, OLD.source_id,
                OLD.ordinal, OLD.resource_uri, OLD.resource_uri_hash,
                OLD.max_attempts
            ) THEN
                RAISE EXCEPTION 'MCP sync item identity and policy are immutable'
                    USING ERRCODE = '23514';
            END IF;

            IF OLD.status IN ('changed', 'unchanged', 'failed') THEN
                RAISE EXCEPTION 'Terminal MCP sync items are immutable'
                    USING ERRCODE = '23514';
            ELSIF OLD.status = 'queued'
                AND NEW.status NOT IN ('queued', 'running') THEN
                RAISE EXCEPTION 'Invalid MCP sync item transition'
                    USING ERRCODE = '23514';
            ELSIF OLD.status = 'running'
                AND NEW.status NOT IN (
                    'running', 'queued', 'changed', 'unchanged', 'failed'
                ) THEN
                RAISE EXCEPTION 'Invalid MCP sync item transition'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_mcp_sync_item_transition
        BEFORE UPDATE ON mcp_sync_items
        FOR EACH ROW
        EXECUTE FUNCTION enforce_mcp_sync_item_transition()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_mcp_sync_history_removal()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'MCP sync history is append-only'
                USING ERRCODE = '23514';
        END;
        $$
        """
    )
    for table_name in ("mcp_sync_runs", "mcp_sync_items"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_reject_delete
            BEFORE DELETE ON {table_name}
            FOR EACH ROW
            EXECUTE FUNCTION reject_mcp_sync_history_removal()
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_reject_truncate
            BEFORE TRUNCATE ON {table_name}
            FOR EACH STATEMENT
            EXECUTE FUNCTION reject_mcp_sync_history_removal()
            """
        )
    op.execute(
        """
        CREATE FUNCTION validate_mcp_sync_run_aggregate()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            target_organization uuid;
            target_workspace uuid;
            target_run uuid;
            run_status text;
            requested integer;
            completed integer;
            changed integer;
            unchanged integer;
            failed integer;
            actual_total integer;
            actual_changed integer;
            actual_unchanged integer;
            actual_failed integer;
        BEGIN
            IF TG_TABLE_NAME = 'mcp_sync_runs' THEN
                target_organization := NEW.organization_id;
                target_workspace := NEW.workspace_id;
                target_run := NEW.id;
            ELSE
                target_organization := COALESCE(NEW.organization_id, OLD.organization_id);
                target_workspace := COALESCE(NEW.workspace_id, OLD.workspace_id);
                target_run := COALESCE(NEW.sync_run_id, OLD.sync_run_id);
            END IF;

            SELECT status, requested_count, completed_count,
                   changed_count, unchanged_count, failed_count
            INTO run_status, requested, completed, changed, unchanged, failed
            FROM mcp_sync_runs
            WHERE organization_id = target_organization
              AND workspace_id = target_workspace
              AND id = target_run;

            IF NOT FOUND OR run_status NOT IN ('succeeded', 'failed') THEN
                RETURN NULL;
            END IF;

            SELECT count(*),
                   count(*) FILTER (WHERE status = 'changed'),
                   count(*) FILTER (WHERE status = 'unchanged'),
                   count(*) FILTER (WHERE status = 'failed')
            INTO actual_total, actual_changed, actual_unchanged, actual_failed
            FROM mcp_sync_items
            WHERE organization_id = target_organization
              AND workspace_id = target_workspace
              AND sync_run_id = target_run;

            IF actual_total <> requested
                OR actual_total <> completed
                OR actual_changed <> changed
                OR actual_unchanged <> unchanged
                OR actual_failed <> failed
                OR actual_total <>
                    actual_changed + actual_unchanged + actual_failed THEN
                RAISE EXCEPTION 'MCP sync run aggregate does not match items'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_mcp_sync_run_aggregate_from_run
        AFTER INSERT OR UPDATE ON mcp_sync_runs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION validate_mcp_sync_run_aggregate()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_mcp_sync_run_aggregate_from_item
        AFTER INSERT OR UPDATE OR DELETE ON mcp_sync_items
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION validate_mcp_sync_run_aggregate()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_mcp_sync_run_aggregate_from_item "
        "ON mcp_sync_items"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_mcp_sync_run_aggregate_from_run "
        "ON mcp_sync_runs"
    )
    for table_name in ("mcp_sync_runs", "mcp_sync_items"):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table_name}_reject_truncate ON {table_name}"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table_name}_reject_delete ON {table_name}"
        )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_mcp_sync_item_transition ON mcp_sync_items"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_mcp_sync_run_transition ON mcp_sync_runs"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_mcp_sync_run_aggregate()")
    op.execute("DROP FUNCTION IF EXISTS reject_mcp_sync_history_removal()")
    op.execute("DROP FUNCTION IF EXISTS enforce_mcp_sync_item_transition()")
    op.execute("DROP FUNCTION IF EXISTS enforce_mcp_sync_run_transition()")
    op.drop_table("mcp_sync_items")
    op.drop_table("mcp_sync_runs")
