"""add document intake promotion foundation

Revision ID: f1fa3b2c77ae
Revises: 187025f68e30
Create Date: 2026-08-25 10:06:42.331651
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1fa3b2c77ae"
down_revision: Union[str, Sequence[str], None] = "187025f68e30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_assets",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("byte_size >= 0", name="ck_source_asset_byte_size"),
        sa.CheckConstraint(
            "length(content) = byte_size", name="ck_source_asset_content_size"
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name="ck_source_asset_hash_length"
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
            "organization_id", "workspace_id", "source_id", "id"
        ),
    )
    op.create_index(
        op.f("ix_source_assets_content_hash"),
        "source_assets",
        ["content_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_source_assets_organization_id"),
        "source_assets",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_source_assets_source_id"),
        "source_assets",
        ["source_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_source_assets_workspace_id"),
        "source_assets",
        ["workspace_id"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION reject_source_asset_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'source_assets are immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_source_assets_immutable
        BEFORE UPDATE OR DELETE ON source_assets
        FOR EACH ROW EXECUTE FUNCTION reject_source_asset_mutation();
        """
    )

    op.add_column(
        "ingestion_runs", sa.Column("source_asset_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "ingestion_runs", sa.Column("document_type", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "ingestion_runs", sa.Column("classification_confidence", sa.Float(), nullable=True)
    )
    op.add_column(
        "ingestion_runs",
        sa.Column("classification_method", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "ingestion_runs", sa.Column("classification_reason", sa.Text(), nullable=True)
    )
    op.add_column(
        "ingestion_runs", sa.Column("normalized_markdown", sa.Text(), nullable=True)
    )
    op.add_column(
        "ingestion_runs",
        sa.Column(
            "review_status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column("review_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "ingestion_runs", sa.Column("document_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "ingestion_runs", sa.Column("document_version_id", sa.Uuid(), nullable=True)
    )
    op.create_index(
        op.f("ix_ingestion_runs_document_id"),
        "ingestion_runs",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ingestion_runs_document_type"),
        "ingestion_runs",
        ["document_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ingestion_runs_document_version_id"),
        "ingestion_runs",
        ["document_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ingestion_runs_review_status"),
        "ingestion_runs",
        ["review_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ingestion_runs_reviewed_by"),
        "ingestion_runs",
        ["reviewed_by"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ingestion_runs_source_asset_id"),
        "ingestion_runs",
        ["source_asset_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_ingestion_runs_reviewer",
        "ingestion_runs",
        "users",
        ["organization_id", "reviewed_by"],
        ["organization_id", "id"],
    )
    op.create_foreign_key(
        "fk_ingestion_runs_promoted_document",
        "ingestion_runs",
        "documents",
        ["organization_id", "workspace_id", "document_id"],
        ["organization_id", "workspace_id", "id"],
    )
    op.create_foreign_key(
        "fk_ingestion_runs_source_asset",
        "ingestion_runs",
        "source_assets",
        ["organization_id", "workspace_id", "source_id", "source_asset_id"],
        ["organization_id", "workspace_id", "source_id", "id"],
    )
    op.create_foreign_key(
        "fk_ingestion_runs_promoted_version",
        "ingestion_runs",
        "document_versions",
        [
            "organization_id",
            "workspace_id",
            "document_id",
            "document_version_id",
        ],
        ["organization_id", "workspace_id", "document_id", "id"],
    )
    op.create_check_constraint(
        "ck_ingestion_run_classification_confidence",
        "ingestion_runs",
        "classification_confidence IS NULL OR "
        "(classification_confidence >= 0 AND classification_confidence <= 1)",
    )
    op.create_check_constraint(
        "ck_ingestion_run_promotion_pair",
        "ingestion_runs",
        "(document_id IS NULL AND document_version_id IS NULL) OR "
        "(document_id IS NOT NULL AND document_version_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_ingestion_run_review_status",
        "ingestion_runs",
        "review_status IN ('pending', 'promoted', 'rejected')",
    )
    op.create_check_constraint(
        "ck_ingestion_run_review_audit",
        "ingestion_runs",
        "(review_status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
        "(review_status IN ('promoted', 'rejected') AND reviewed_by IS NOT NULL "
        "AND reviewed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_ingestion_run_rejection_reason",
        "ingestion_runs",
        "review_status != 'rejected' OR "
        "(review_reason IS NOT NULL AND length(review_reason) > 0)",
    )
    op.create_check_constraint(
        "ck_ingestion_run_review_document",
        "ingestion_runs",
        "(review_status = 'promoted' AND document_id IS NOT NULL) OR "
        "(review_status IN ('pending', 'rejected') AND document_id IS NULL)",
    )

    op.execute(
        """
        CREATE FUNCTION reject_terminal_ingestion_review_mutation() RETURNS trigger AS $$
        BEGIN
            IF OLD.review_status IN ('promoted', 'rejected') THEN
                RAISE EXCEPTION 'terminal ingestion reviews are immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_terminal_ingestion_reviews_immutable
        BEFORE UPDATE OR DELETE ON ingestion_runs
        FOR EACH ROW EXECUTE FUNCTION reject_terminal_ingestion_review_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_terminal_extraction_candidate_mutation() RETURNS trigger AS $$
        BEGIN
            IF OLD.status IN ('accepted', 'rejected') THEN
                RAISE EXCEPTION 'terminal extraction candidates are immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_terminal_extraction_candidates_immutable
        BEFORE UPDATE OR DELETE ON extraction_candidates
        FOR EACH ROW EXECUTE FUNCTION reject_terminal_extraction_candidate_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_document_chunk_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF EXISTS (
                    SELECT 1 FROM ingestion_runs
                    WHERE organization_id = NEW.organization_id
                      AND workspace_id = NEW.workspace_id
                      AND document_version_id = NEW.version_id
                      AND review_status = 'promoted'
                ) THEN
                    RAISE EXCEPTION 'promoted document chunk membership is sealed';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'document_chunks are immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_document_chunks_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON document_chunks
        FOR EACH ROW EXECUTE FUNCTION reject_document_chunk_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_promoted_ingestion_evidence_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.evidence_type = 'ingested_document' THEN
                    IF EXISTS (
                        SELECT 1 FROM ingestion_runs
                        WHERE id::text = NEW.pointer ->> 'ingestion_run_id'
                          AND organization_id = NEW.organization_id
                          AND workspace_id = NEW.workspace_id
                          AND source_id = NEW.source_id
                          AND review_status = 'promoted'
                    ) THEN
                        RAISE EXCEPTION 'promoted ingestion evidence membership is sealed';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM ingestion_runs
                        WHERE id::text = NEW.pointer ->> 'ingestion_run_id'
                          AND organization_id = NEW.organization_id
                          AND workspace_id = NEW.workspace_id
                          AND source_id = NEW.source_id
                          AND content_hash = NEW.pointer ->> 'content_hash'
                          AND status = 'succeeded'
                          AND review_status = 'pending'
                    ) THEN
                        RAISE EXCEPTION 'ingested document evidence requires a successful pending ingestion';
                    END IF;
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.evidence_type = 'ingested_document'
               OR (TG_OP = 'UPDATE' AND NEW.evidence_type = 'ingested_document') THEN
                RAISE EXCEPTION 'promoted ingestion evidence is immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_promoted_ingestion_evidence_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON evidence
        FOR EACH ROW EXECUTE FUNCTION reject_promoted_ingestion_evidence_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_promoted_ingestion_evidence_link_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' AND EXISTS (
                SELECT 1 FROM evidence
                WHERE id = NEW.evidence_id
                  AND organization_id = NEW.organization_id
                  AND workspace_id = NEW.workspace_id
                  AND evidence_type = 'ingested_document'
            ) THEN
                IF EXISTS (
                    SELECT 1
                    FROM evidence AS evidence_row
                    JOIN ingestion_runs AS run
                      ON run.id::text = evidence_row.pointer ->> 'ingestion_run_id'
                     AND run.organization_id = evidence_row.organization_id
                     AND run.workspace_id = evidence_row.workspace_id
                    WHERE evidence_row.id = NEW.evidence_id
                      AND evidence_row.organization_id = NEW.organization_id
                      AND evidence_row.workspace_id = NEW.workspace_id
                      AND run.review_status = 'promoted'
                ) THEN
                    RAISE EXCEPTION 'promoted ingestion evidence link membership is sealed';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM evidence AS evidence_row
                    JOIN ingestion_runs AS run
                      ON run.id::text = evidence_row.pointer ->> 'ingestion_run_id'
                     AND run.organization_id = evidence_row.organization_id
                     AND run.workspace_id = evidence_row.workspace_id
                    JOIN document_versions AS version
                      ON version.id::text = evidence_row.pointer ->> 'document_version_id'
                     AND version.organization_id = evidence_row.organization_id
                     AND version.workspace_id = evidence_row.workspace_id
                    WHERE evidence_row.id = NEW.evidence_id
                      AND evidence_row.organization_id = NEW.organization_id
                      AND evidence_row.workspace_id = NEW.workspace_id
                      AND run.status = 'succeeded'
                      AND run.review_status = 'pending'
                      AND NEW.document_id = version.document_id
                ) THEN
                    RAISE EXCEPTION 'ingested document evidence links require pending promotion scope';
                END IF;
                RETURN NEW;
            END IF;
            IF EXISTS (
                SELECT 1 FROM evidence
                WHERE id = OLD.evidence_id
                  AND organization_id = OLD.organization_id
                  AND workspace_id = OLD.workspace_id
                  AND evidence_type = 'ingested_document'
            ) OR (
                TG_OP = 'UPDATE' AND EXISTS (
                    SELECT 1 FROM evidence
                    WHERE id = NEW.evidence_id
                      AND organization_id = NEW.organization_id
                      AND workspace_id = NEW.workspace_id
                      AND evidence_type = 'ingested_document'
                )
            ) THEN
                RAISE EXCEPTION 'promoted ingestion evidence links are immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_promoted_ingestion_evidence_links_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON evidence_links
        FOR EACH ROW EXECUTE FUNCTION reject_promoted_ingestion_evidence_link_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_promoted_ingestion_evidence_links_immutable "
        "ON evidence_links"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_promoted_ingestion_evidence_link_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_promoted_ingestion_evidence_immutable ON evidence")
    op.execute("DROP FUNCTION IF EXISTS reject_promoted_ingestion_evidence_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_document_chunks_immutable ON document_chunks")
    op.execute("DROP FUNCTION IF EXISTS reject_document_chunk_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_terminal_extraction_candidates_immutable "
        "ON extraction_candidates"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_terminal_extraction_candidate_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_terminal_ingestion_reviews_immutable ON ingestion_runs"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_terminal_ingestion_review_mutation()")
    op.drop_constraint(
        "ck_ingestion_run_review_document", "ingestion_runs", type_="check"
    )
    op.drop_constraint(
        "ck_ingestion_run_rejection_reason", "ingestion_runs", type_="check"
    )
    op.drop_constraint(
        "ck_ingestion_run_review_audit", "ingestion_runs", type_="check"
    )
    op.drop_constraint(
        "ck_ingestion_run_review_status", "ingestion_runs", type_="check"
    )
    op.drop_constraint(
        "ck_ingestion_run_promotion_pair", "ingestion_runs", type_="check"
    )
    op.drop_constraint(
        "ck_ingestion_run_classification_confidence",
        "ingestion_runs",
        type_="check",
    )
    op.drop_constraint(
        "fk_ingestion_runs_promoted_version", "ingestion_runs", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_ingestion_runs_source_asset", "ingestion_runs", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_ingestion_runs_promoted_document", "ingestion_runs", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_ingestion_runs_reviewer", "ingestion_runs", type_="foreignkey"
    )
    op.drop_index(op.f("ix_ingestion_runs_source_asset_id"), table_name="ingestion_runs")
    op.drop_index(op.f("ix_ingestion_runs_reviewed_by"), table_name="ingestion_runs")
    op.drop_index(op.f("ix_ingestion_runs_review_status"), table_name="ingestion_runs")
    op.drop_index(
        op.f("ix_ingestion_runs_document_version_id"), table_name="ingestion_runs"
    )
    op.drop_index(op.f("ix_ingestion_runs_document_type"), table_name="ingestion_runs")
    op.drop_index(op.f("ix_ingestion_runs_document_id"), table_name="ingestion_runs")
    op.drop_column("ingestion_runs", "document_version_id")
    op.drop_column("ingestion_runs", "document_id")
    op.drop_column("ingestion_runs", "review_reason")
    op.drop_column("ingestion_runs", "reviewed_at")
    op.drop_column("ingestion_runs", "reviewed_by")
    op.drop_column("ingestion_runs", "review_status")
    op.drop_column("ingestion_runs", "normalized_markdown")
    op.drop_column("ingestion_runs", "classification_reason")
    op.drop_column("ingestion_runs", "classification_method")
    op.drop_column("ingestion_runs", "classification_confidence")
    op.drop_column("ingestion_runs", "document_type")
    op.drop_column("ingestion_runs", "source_asset_id")

    op.execute("DROP TRIGGER trg_source_assets_immutable ON source_assets")
    op.execute("DROP FUNCTION reject_source_asset_mutation()")
    op.drop_index(op.f("ix_source_assets_workspace_id"), table_name="source_assets")
    op.drop_index(op.f("ix_source_assets_source_id"), table_name="source_assets")
    op.drop_index(op.f("ix_source_assets_organization_id"), table_name="source_assets")
    op.drop_index(op.f("ix_source_assets_content_hash"), table_name="source_assets")
    op.drop_table("source_assets")
