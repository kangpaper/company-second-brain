"""add saved mcp connections

Revision ID: 263a4ca7e76c
Revises: f1fa3b2c77ae
Create Date: 2026-08-25 14:09:33.384369
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "263a4ca7e76c"
down_revision: Union[str, Sequence[str], None] = "f1fa3b2c77ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_connections",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("credential_key", sa.String(length=64), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(credential_key) BETWEEN 1 AND 64",
            name="ck_mcp_connection_credential_key_length",
        ),
        sa.CheckConstraint(
            "credential_key ~ '^[a-z][a-z0-9-]{0,63}$'",
            name="ck_mcp_connection_credential_key_format",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            ["users.organization_id", "users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "source_id"],
            ["sources.organization_id", "sources.workspace_id", "sources.id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "workspace_id", "id"),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "name",
            name="uq_mcp_connections_tenant_name",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "source_id",
            name="uq_mcp_connections_tenant_source",
        ),
    )
    op.create_index(
        op.f("ix_mcp_connections_created_by_user_id"),
        "mcp_connections",
        ["created_by_user_id"],
    )
    op.create_index(
        op.f("ix_mcp_connections_organization_id"),
        "mcp_connections",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_mcp_connections_source_id"),
        "mcp_connections",
        ["source_id"],
    )
    op.create_index(
        op.f("ix_mcp_connections_workspace_id"),
        "mcp_connections",
        ["workspace_id"],
    )
    op.execute(
        """
        CREATE FUNCTION validate_mcp_connection_source() RETURNS trigger AS $$
        DECLARE
            locked_source_type text;
        BEGIN
            SELECT source_type
            INTO locked_source_type
            FROM sources
            WHERE organization_id = NEW.organization_id
              AND workspace_id = NEW.workspace_id
              AND id = NEW.source_id
            FOR SHARE;

            IF locked_source_type IS DISTINCT FROM 'mcp_instance' THEN
                RAISE EXCEPTION 'MCP connection source must be an MCP instance'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_mcp_connections_validate_source
        BEFORE INSERT OR UPDATE OF organization_id, workspace_id, source_id
        ON mcp_connections
        FOR EACH ROW EXECUTE FUNCTION validate_mcp_connection_source();
        """
    )
    op.execute(
        """
        CREATE FUNCTION protect_mcp_connection_source_type() RETURNS trigger AS $$
        BEGIN
            IF NEW.source_type <> 'mcp_instance' AND EXISTS (
                SELECT 1
                FROM mcp_connections
                WHERE organization_id = OLD.organization_id
                  AND workspace_id = OLD.workspace_id
                  AND source_id = OLD.id
            ) THEN
                RAISE EXCEPTION 'MCP connection source type is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_sources_protect_mcp_connection_type
        BEFORE UPDATE OF source_type
        ON sources
        FOR EACH ROW EXECUTE FUNCTION protect_mcp_connection_source_type();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_sources_protect_mcp_connection_type ON sources"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_mcp_connection_source_type()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_mcp_connections_validate_source ON mcp_connections"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_mcp_connection_source()")
    op.drop_index(
        op.f("ix_mcp_connections_workspace_id"), table_name="mcp_connections"
    )
    op.drop_index(op.f("ix_mcp_connections_source_id"), table_name="mcp_connections")
    op.drop_index(
        op.f("ix_mcp_connections_organization_id"), table_name="mcp_connections"
    )
    op.drop_index(
        op.f("ix_mcp_connections_created_by_user_id"),
        table_name="mcp_connections",
    )
    op.drop_table("mcp_connections")
