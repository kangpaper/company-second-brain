from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy.orm import Session

from company_brain.domain.models import (
    ExtractionCandidate,
    IngestionRun,
    Source,
    SourceAsset,
)
from company_brain.domain.repositories import TenantScope
from company_brain.ingestion.parsers import ParseError, parse_content
from company_brain.ingestion.processing import (
    NormalizationError,
    classify_document,
    normalize_to_markdown,
)


@dataclass(frozen=True)
class IntakeInput:
    source_type: str
    uri: str
    filename: str
    media_type: str


class IntakeProcessingError(Exception):
    def __init__(self, run: IngestionRun, code: str, message: str) -> None:
        super().__init__(message)
        self.run = run
        self.code = code
        self.message = message


def _source_for_intake(
    session: Session,
    scope: TenantScope,
    intake: IntakeInput,
    source: Source | None,
) -> Source:
    if source is not None:
        if (
            source.organization_id != scope.organization_id
            or source.workspace_id != scope.workspace_id
        ):
            raise ValueError("Intake source scope mismatch")
        return source
    source = Source(
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        source_type=intake.source_type,
        uri=intake.uri,
        metadata_={"filename": intake.filename, "media_type": intake.media_type},
    )
    session.add(source)
    session.flush()
    return source


def _failed_run(
    session: Session,
    scope: TenantScope,
    source: Source,
    asset: SourceAsset,
    intake: IntakeInput,
    digest: str,
    code: str,
    message: str,
) -> IntakeProcessingError:
    run = IngestionRun(
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        source_id=source.id,
        source_asset_id=asset.id,
        status="failed",
        filename=intake.filename,
        media_type=intake.media_type,
        content_hash=digest,
        byte_size=asset.byte_size,
        candidate_count=0,
        error_code=code,
        error_message=message,
    )
    session.add(run)
    session.flush()
    return IntakeProcessingError(run, code, message)


def stage_intake(
    session: Session,
    scope: TenantScope,
    intake: IntakeInput,
    content: bytes,
    *,
    source: Source | None = None,
) -> IngestionRun:
    digest = sha256(content).hexdigest()
    source = _source_for_intake(session, scope, intake, source)
    asset = SourceAsset(
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        source_id=source.id,
        filename=intake.filename,
        media_type=intake.media_type,
        content_hash=digest,
        byte_size=len(content),
        content=content,
    )
    session.add(asset)
    session.flush()

    try:
        parsed = parse_content(intake.media_type, intake.filename, content)
    except ParseError as error:
        raise _failed_run(
            session, scope, source, asset, intake, digest, "parse_error", str(error)
        ) from error
    except Exception as error:
        message = "Parser failed unexpectedly"
        raise _failed_run(
            session, scope, source, asset, intake, digest, "parser_error", message
        ) from error

    classification = classify_document(filename=intake.filename, text=parsed.text)
    try:
        normalized_markdown = normalize_to_markdown(
            filename=intake.filename,
            media_type=intake.media_type,
            parsed=parsed,
            classification=classification,
            source_uri=intake.uri,
            content_hash=digest,
        )
    except NormalizationError as error:
        raise _failed_run(
            session,
            scope,
            source,
            asset,
            intake,
            digest,
            "normalization_error",
            str(error),
        ) from error

    run = IngestionRun(
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        source_id=source.id,
        source_asset_id=asset.id,
        status="succeeded",
        filename=intake.filename,
        media_type=intake.media_type,
        content_hash=digest,
        byte_size=len(content),
        extracted_text=parsed.text,
        parser_metadata=parsed.metadata,
        candidate_count=len(parsed.candidates),
        document_type=classification.document_type,
        classification_confidence=classification.confidence,
        classification_method=classification.method,
        classification_reason=classification.reason,
        normalized_markdown=normalized_markdown,
        review_status="pending",
    )
    session.add(run)
    session.flush()
    session.add_all(
        [
            ExtractionCandidate(
                organization_id=scope.organization_id,
                workspace_id=scope.workspace_id,
                ingestion_run_id=run.id,
                source_id=source.id,
                candidate_index=index,
                candidate_type=candidate.kind,
                locator=candidate.locator,
                data=candidate.data,
                text=candidate.text,
                status="pending",
            )
            for index, candidate in enumerate(parsed.candidates)
        ]
    )
    session.flush()
    return run
