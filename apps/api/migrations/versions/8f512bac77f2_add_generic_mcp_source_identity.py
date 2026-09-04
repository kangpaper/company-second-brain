"""add generic mcp source identity

Revision ID: 8f512bac77f2
Revises: 545c74fcdca1
Create Date: 2026-08-21 13:37:16.952277
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8f512bac77f2"
down_revision: str | Sequence[str] | None = "545c74fcdca1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_sources_mcp_instance_uri",
        "sources",
        ["organization_id", "workspace_id", "source_type", "uri"],
        unique=True,
        postgresql_where=sa.text("source_type = 'mcp_instance'"),
    )


def downgrade() -> None:
    op.drop_index("uq_sources_mcp_instance_uri", table_name="sources")
