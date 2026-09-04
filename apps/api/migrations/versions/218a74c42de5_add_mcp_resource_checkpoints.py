"""add mcp resource checkpoints

Revision ID: 218a74c42de5
Revises: 263a4ca7e76c
Create Date: 2026-08-25 15:52:28.654670
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "218a74c42de5"
down_revision: Union[str, Sequence[str], None] = "263a4ca7e76c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_ingestion_runs_mcp_checkpoint_target",
        "ingestion_runs",
        [
            "organization_id",
            "workspace_id",
            "source_id",
            "id",
            "content_hash",
            "status",
        ],
    )
    op.create_unique_constraint(
        "uq_mcp_connections_checkpoint_source",
        "mcp_connections",
        ["organization_id", "workspace_id", "id", "source_id"],
    )
    op.create_table(
        "mcp_resource_checkpoints",
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("resource_uri", sa.String(length=2048), nullable=False),
        sa.Column("resource_uri_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("ingestion_run_id", sa.Uuid(), nullable=True),
        sa.Column("ingestion_status", sa.String(length=32), nullable=True),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(content_hash IS NULL AND ingestion_run_id IS NULL "
            "AND ingestion_status IS NULL AND last_changed_at IS NULL) OR "
            "(content_hash IS NOT NULL AND ingestion_run_id IS NOT NULL "
            "AND ingestion_status = 'succeeded' AND last_changed_at IS NOT NULL)",
            name="ck_mcp_resource_checkpoint_target",
        ),
        sa.CheckConstraint(
            "resource_uri_hash ~ '^[0-9a-f]{64}$'",
            name="ck_mcp_resource_checkpoint_uri_hash",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "connection_id", "source_id"],
            [
                "mcp_connections.organization_id",
                "mcp_connections.workspace_id",
                "mcp_connections.id",
                "mcp_connections.source_id",
            ],
            name="fk_mcp_resource_checkpoint_connection_source",
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
            name="fk_mcp_resource_checkpoint_ingestion_target",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_mcp_resource_checkpoint_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_mcp_resource_checkpoint_organization",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "connection_id",
            "resource_uri_hash",
            name="uq_mcp_resource_checkpoints_identity",
        ),
        sa.UniqueConstraint("organization_id", "workspace_id", "id"),
    )
    op.create_index(
        op.f("ix_mcp_resource_checkpoints_connection_id"),
        "mcp_resource_checkpoints",
        ["connection_id"],
    )
    op.create_index(
        op.f("ix_mcp_resource_checkpoints_ingestion_run_id"),
        "mcp_resource_checkpoints",
        ["ingestion_run_id"],
    )
    op.create_index(
        op.f("ix_mcp_resource_checkpoints_organization_id"),
        "mcp_resource_checkpoints",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_mcp_resource_checkpoints_source_id"),
        "mcp_resource_checkpoints",
        ["source_id"],
    )
    op.create_index(
        op.f("ix_mcp_resource_checkpoints_workspace_id"),
        "mcp_resource_checkpoints",
        ["workspace_id"],
    )
    op.execute(
        """
        CREATE FUNCTION prevent_mcp_resource_checkpoint_identity_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF ROW(
                NEW.organization_id,
                NEW.workspace_id,
                NEW.connection_id,
                NEW.source_id,
                NEW.resource_uri,
                NEW.resource_uri_hash
            ) IS DISTINCT FROM ROW(
                OLD.organization_id,
                OLD.workspace_id,
                OLD.connection_id,
                OLD.source_id,
                OLD.resource_uri,
                OLD.resource_uri_hash
            ) THEN
                RAISE EXCEPTION 'MCP resource checkpoint identity is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_mcp_resource_checkpoint_identity_immutable
        BEFORE UPDATE OF
            organization_id,
            workspace_id,
            connection_id,
            source_id,
            resource_uri,
            resource_uri_hash
        ON mcp_resource_checkpoints
        FOR EACH ROW
        EXECUTE FUNCTION prevent_mcp_resource_checkpoint_identity_update()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_mcp_resource_checkpoint_identity_immutable "
        "ON mcp_resource_checkpoints"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS prevent_mcp_resource_checkpoint_identity_update()"
    )
    op.drop_index(
        op.f("ix_mcp_resource_checkpoints_workspace_id"),
        table_name="mcp_resource_checkpoints",
    )
    op.drop_index(
        op.f("ix_mcp_resource_checkpoints_source_id"),
        table_name="mcp_resource_checkpoints",
    )
    op.drop_index(
        op.f("ix_mcp_resource_checkpoints_organization_id"),
        table_name="mcp_resource_checkpoints",
    )
    op.drop_index(
        op.f("ix_mcp_resource_checkpoints_ingestion_run_id"),
        table_name="mcp_resource_checkpoints",
    )
    op.drop_index(
        op.f("ix_mcp_resource_checkpoints_connection_id"),
        table_name="mcp_resource_checkpoints",
    )
    op.drop_table("mcp_resource_checkpoints")
    op.drop_constraint(
        "uq_mcp_connections_checkpoint_source",
        "mcp_connections",
        type_="unique",
    )
    op.drop_constraint(
        "uq_ingestion_runs_mcp_checkpoint_target",
        "ingestion_runs",
        type_="unique",
    )
