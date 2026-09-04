from dataclasses import dataclass
from pathlib import Path

import yaml

from company_brain.ingestion.parsers import ParsedContent

CLASSIFIER_VERSION = "deterministic-rules.v1"
MAX_NORMALIZED_MARKDOWN_CHARS = 2_000_000


class NormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class ClassificationResult:
    document_type: str
    confidence: float
    method: str
    reason: str


_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("invoice", ("invoice", "amount due", "payment due", "invoice number")),
    ("contract", ("contract", "agreement", "effective date", "party")),
    ("meeting_notes", ("meeting notes", "attendees", "action items", "minutes")),
    ("customer_report", ("customer report", "account health", "revenue trend")),
    ("sales_proposal", ("sales proposal", "commercial offer", "pricing", "proposal")),
    ("support_document", ("support ticket", "complaint category", "incident", "resolution")),
    ("policy", ("policy", "requirements", "procedure", "governance")),
    ("project_document", ("project plan", "milestones", "delivery status", "project charter")),
    (
        "product_documentation",
        ("product documentation", "installation guide", "user guide", "release notes"),
    ),
)


def classify_document(*, filename: str, text: str) -> ClassificationResult:
    searchable = f"{Path(filename).stem.replace('-', ' ')} {text[:20_000]}".casefold()
    matches: list[tuple[str, list[str]]] = []
    for document_type, markers in _RULES:
        matched = [marker for marker in markers if marker in searchable]
        if matched:
            matches.append((document_type, matched))
    if matches:
        document_type, matched = max(matches, key=lambda item: len(item[1]))
        confidence = min(0.98, 0.72 + 0.08 * len(matched))
        return ClassificationResult(
            document_type=document_type,
            confidence=confidence,
            method=CLASSIFIER_VERSION,
            reason=f"Matched {document_type.replace('_', ' ')} indicators: {', '.join(matched)}.",
        )
    return ClassificationResult(
        document_type="unclassified",
        confidence=0.0,
        method=CLASSIFIER_VERSION,
        reason="No controlled business-document rule matched.",
    )


def normalize_to_markdown(
    *,
    filename: str,
    media_type: str,
    parsed: ParsedContent,
    classification: ClassificationResult,
    source_uri: str,
    content_hash: str,
) -> str:
    title = Path(filename).stem.strip() or "Untitled document"
    frontmatter = {
        "title": title,
        "type": classification.document_type,
        "status": "needs_review",
        "classification": {
            "confidence": classification.confidence,
            "method": classification.method,
            "reason": classification.reason,
        },
        "source": {
            "uri": source_uri,
            "filename": filename,
            "media_type": media_type,
            "content_hash": content_hash,
        },
        "parser": parsed.metadata,
        "tags": [classification.document_type],
    }
    serialized = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).rstrip()
    body = parsed.text.strip()
    markdown = f"---\n{serialized}\n---\n# {title}\n\n{body}\n"
    if len(markdown) > MAX_NORMALIZED_MARKDOWN_CHARS:
        raise NormalizationError(
            f"Normalized Markdown exceeds {MAX_NORMALIZED_MARKDOWN_CHARS} characters"
        )
    return markdown
