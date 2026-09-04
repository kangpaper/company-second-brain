"""link external references to sources

Revision ID: 7450833f18d3
Revises: 59009aa896f9
Create Date: 2026-08-13 13:33:29.819637
"""

from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op


revision: str = "7450833f18d3"
down_revision: str | Sequence[str] | None = "59009aa896f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FK_NAME = "fk_external_references_tenant_source"
OLD_UNIQUE_NAME = (
    "external_references_organization_id_source_system_source_mo_key"
)
WORKSPACE_UNIQUE_NAME = "uq_external_reference_workspace_source"
ODOO_SOURCE_INDEX_NAME = "uq_sources_odoo_instance_uri"


def upgrade() -> None:
    op.drop_constraint(
        OLD_UNIQUE_NAME, "external_references", type_="unique"
    )
    op.add_column(
        "external_references", sa.Column("source_id", sa.Uuid(), nullable=True)
    )
    op.execute(
        """
        INSERT INTO sources (
            id,
            source_type,
            uri,
            metadata,
            created_at,
            updated_at,
            organization_id,
            workspace_id
        )
        SELECT
            (
                substr(md5(er.id::text || chr(58) || 'source'), 1, 8) || '-' ||
                substr(md5(er.id::text || chr(58) || 'source'), 9, 4) || '-' ||
                substr(md5(er.id::text || chr(58) || 'source'), 13, 4) || '-' ||
                substr(md5(er.id::text || chr(58) || 'source'), 17, 4) || '-' ||
                substr(md5(er.id::text || chr(58) || 'source'), 21, 12)
            )::uuid,
            er.source_system || '_record',
            er.source_system || '://' || er.source_model || '/' || er.external_id,
            json_build_object(
                'source_system', er.source_system,
                'source_model', er.source_model
            ),
            er.created_at,
            er.updated_at,
            er.organization_id,
            er.workspace_id
        FROM external_references er
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE external_references
        SET source_id = (
            substr(md5(id::text || chr(58) || 'source'), 1, 8) || '-' ||
            substr(md5(id::text || chr(58) || 'source'), 9, 4) || '-' ||
            substr(md5(id::text || chr(58) || 'source'), 13, 4) || '-' ||
            substr(md5(id::text || chr(58) || 'source'), 17, 4) || '-' ||
            substr(md5(id::text || chr(58) || 'source'), 21, 12)
        )::uuid
        """
    )
    op.alter_column("external_references", "source_id", nullable=False)
    op.create_unique_constraint(
        WORKSPACE_UNIQUE_NAME,
        "external_references",
        [
            "organization_id",
            "workspace_id",
            "source_id",
            "source_model",
            "external_id",
        ],
    )
    op.create_index(
        ODOO_SOURCE_INDEX_NAME,
        "sources",
        ["organization_id", "workspace_id", "source_type", "uri"],
        unique=True,
        postgresql_where=sa.text("source_type = 'odoo_instance'"),
    )
    op.create_index(
        op.f("ix_external_references_source_id"),
        "external_references",
        ["source_id"],
        unique=False,
    )
    op.create_foreign_key(
        FK_NAME,
        "external_references",
        "sources",
        ["organization_id", "workspace_id", "source_id"],
        ["organization_id", "workspace_id", "id"],
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM external_references
                GROUP BY organization_id, source_system, source_model, external_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: Phase 6 external identities conflict with Phase 5 uniqueness';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint(FK_NAME, "external_references", type_="foreignkey")
    op.drop_constraint(
        WORKSPACE_UNIQUE_NAME, "external_references", type_="unique"
    )
    op.drop_index(
        ODOO_SOURCE_INDEX_NAME, table_name="sources", if_exists=True
    )
    op.drop_index(
        op.f("ix_external_references_source_id"),
        table_name="external_references",
    )
    op.drop_column("external_references", "source_id")
    op.create_unique_constraint(
        OLD_UNIQUE_NAME,
        "external_references",
        ["organization_id", "source_system", "source_model", "external_id"],
    )
