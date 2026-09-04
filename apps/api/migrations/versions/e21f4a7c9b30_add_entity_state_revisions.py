"""add append-only entity state revisions

Revision ID: e21f4a7c9b30
Revises: 9d4e7b8c1f20
Create Date: 2026-08-21 10:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e21f4a7c9b30"
down_revision: str | Sequence[str] | None = "9d4e7b8c1f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    entity_type = postgresql.ENUM(name="entitytype", create_type=False)
    op.create_table(
        "entity_revisions",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", entity_type, nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("normalized_name", sa.String(length=500), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "operation IN ('insert', 'update', 'delete')",
            name="ck_entity_revision_operation",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint("organization_id", "workspace_id", "revision_id"),
    )
    for column in (
        "entity_id",
        "entity_type",
        "effective_at",
        "organization_id",
        "workspace_id",
    ):
        op.create_index(f"ix_entity_revisions_{column}", "entity_revisions", [column])
    op.create_index(
        "ix_entity_revisions_tenant_entity_effective",
        "entity_revisions",
        ["organization_id", "workspace_id", "entity_id", "effective_at", "revision_id"],
    )
    op.execute(
        """
        INSERT INTO entity_revisions (
            revision_id, entity_id, entity_type, name, normalized_name, aliases,
            metadata, lifecycle_status, effective_at, operation, created_at,
            organization_id, workspace_id
        )
        SELECT
            gen_random_uuid(), id, entity_type, name, normalized_name, aliases,
            metadata, lifecycle_status, created_at, 'insert', created_at,
            organization_id, workspace_id
        FROM entities
        """
    )
    op.execute(
        """
        CREATE FUNCTION capture_entity_revision()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            row_state entities%ROWTYPE;
            revision_operation text;
            revision_effective_at timestamptz;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                row_state := OLD;
                revision_operation := 'delete';
                revision_effective_at := clock_timestamp();
            ELSE
                row_state := NEW;
                revision_operation := lower(TG_OP);
                IF TG_OP = 'INSERT' THEN
                    revision_effective_at := NEW.created_at;
                ELSE
                    revision_effective_at := clock_timestamp();
                    IF OLD.organization_id IS DISTINCT FROM NEW.organization_id
                       OR OLD.workspace_id IS DISTINCT FROM NEW.workspace_id THEN
                        INSERT INTO entity_revisions (
                            revision_id, entity_id, entity_type, name,
                            normalized_name, aliases, metadata, lifecycle_status,
                            effective_at, operation, created_at,
                            organization_id, workspace_id
                        ) VALUES (
                            gen_random_uuid(), OLD.id, OLD.entity_type, OLD.name,
                            OLD.normalized_name, OLD.aliases, OLD.metadata,
                            OLD.lifecycle_status, revision_effective_at, 'delete',
                            clock_timestamp(), OLD.organization_id, OLD.workspace_id
                        );
                    END IF;
                END IF;
            END IF;

            INSERT INTO entity_revisions (
                revision_id, entity_id, entity_type, name, normalized_name, aliases,
                metadata, lifecycle_status, effective_at, operation, created_at,
                organization_id, workspace_id
            ) VALUES (
                gen_random_uuid(), row_state.id, row_state.entity_type, row_state.name,
                row_state.normalized_name, row_state.aliases, row_state.metadata,
                row_state.lifecycle_status, revision_effective_at,
                revision_operation, clock_timestamp(), row_state.organization_id,
                row_state.workspace_id
            );
            RETURN COALESCE(NEW, OLD);
        END;
        $$;

        CREATE TRIGGER entities_capture_revision
        AFTER INSERT OR UPDATE OR DELETE ON entities
        FOR EACH ROW EXECUTE FUNCTION capture_entity_revision();
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_entity_revision_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'entity revisions are append-only';
        END;
        $$;

        CREATE TRIGGER entity_revisions_reject_mutation
        BEFORE UPDATE OR DELETE ON entity_revisions
        FOR EACH ROW EXECUTE FUNCTION reject_entity_revision_mutation();

        CREATE TRIGGER entity_revisions_reject_truncate
        BEFORE TRUNCATE ON entity_revisions
        FOR EACH STATEMENT EXECUTE FUNCTION reject_entity_revision_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS entities_capture_revision ON entities")
    op.execute("DROP FUNCTION IF EXISTS capture_entity_revision()")
    op.execute("DROP TRIGGER IF EXISTS entity_revisions_reject_mutation ON entity_revisions")
    op.execute("DROP TRIGGER IF EXISTS entity_revisions_reject_truncate ON entity_revisions")
    op.execute("DROP FUNCTION IF EXISTS reject_entity_revision_mutation()")
    op.drop_table("entity_revisions")
