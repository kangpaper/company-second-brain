"""guard integration audit truncate

Revision ID: 187025f68e30
Revises: 8f512bac77f2
Create Date: 2026-08-24 16:36:31.848531
"""

from collections.abc import Sequence

from alembic import op

revision: str = "187025f68e30"
down_revision: str | Sequence[str] | None = "8f512bac77f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TRIGGER integration_audits_reject_truncate
        BEFORE TRUNCATE ON integration_audits
        FOR EACH STATEMENT
        EXECUTE FUNCTION reject_integration_audit_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS integration_audits_reject_truncate "
        "ON integration_audits"
    )
