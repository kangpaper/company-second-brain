import pytest

from company_brain.ingestion.parsers import ParsedContent
from company_brain.ingestion.processing import (
    MAX_NORMALIZED_MARKDOWN_CHARS,
    NormalizationError,
    classify_document,
    normalize_to_markdown,
)
from company_brain.knowledge.markdown import parse_markdown


def test_invoice_is_classified_with_explainable_confidence() -> None:
    result = classify_document(
        filename="INV-2026-0042.pdf",
        text="Invoice number INV-2026-0042\nAmount due: 1,200 USD\nPayment due date: 2026-09-15",
    )

    assert result.document_type == "invoice"
    assert result.confidence >= 0.8
    assert result.method == "deterministic-rules.v1"
    assert "invoice" in result.reason.lower()


@pytest.mark.parametrize(
    ("filename", "text", "expected_type"),
    [
        (
            "customer-contract.docx",
            "Agreement between Acme and Example Ltd. Effective date",
            "contract",
        ),
        (
            "weekly-meeting-notes.txt",
            "Meeting notes\nAttendees: Alice, Bob\nAction items",
            "meeting_notes",
        ),
        (
            "customer-health-report.pdf",
            "Customer report\nAccount health and revenue trend",
            "customer_report",
        ),
        (
            "renewal-proposal.pdf",
            "Sales proposal\nPricing and commercial offer",
            "sales_proposal",
        ),
        (
            "ticket-export.csv",
            "Support ticket\nComplaint category: delivery",
            "support_document",
        ),
        ("security-policy.md", "Policy\nAccess control requirements", "policy"),
        (
            "project-charter.docx",
            "Project plan\nMilestones and delivery status",
            "project_document",
        ),
        (
            "product-guide.html",
            "Product documentation\nInstallation guide",
            "product_documentation",
        ),
    ],
)
def test_controlled_business_taxonomy(
    filename: str, text: str, expected_type: str
) -> None:
    result = classify_document(filename=filename, text=text)

    assert result.document_type == expected_type
    assert 0.0 <= result.confidence <= 1.0
    assert result.reason


def test_unknown_document_requires_review() -> None:
    result = classify_document(filename="notes.txt", text="A few disconnected observations")

    assert result.document_type == "unclassified"
    assert result.confidence < 0.6


def test_normalization_builds_parseable_markdown_with_classification() -> None:
    parsed = ParsedContent(
        text="Invoice INV-42\nAmount due: 1,200 USD",
        candidates=[],
        metadata={"format": "pdf", "pages": 1},
    )
    classification = classify_document(filename="INV-42.pdf", text=parsed.text)

    markdown = normalize_to_markdown(
        filename="INV-42.pdf",
        media_type="application/pdf",
        parsed=parsed,
        classification=classification,
        source_uri="upload://INV-42.pdf",
        content_hash="a" * 64,
    )
    normalized = parse_markdown(markdown)

    assert normalized.frontmatter["title"] == "INV-42"
    assert normalized.frontmatter["type"] == "invoice"
    assert normalized.frontmatter["classification"]["confidence"] >= 0.8
    assert normalized.frontmatter["source"]["content_hash"] == "a" * 64
    assert normalized.body.startswith("# INV-42\n")
    assert "Amount due" in normalized.body


def test_normalized_markdown_enforces_canonical_document_bound() -> None:
    classification = classify_document(filename="large.txt", text="x")
    parsed = ParsedContent(
        text="x" * MAX_NORMALIZED_MARKDOWN_CHARS,
        metadata={"format": "text"},
        candidates=[],
    )

    with pytest.raises(NormalizationError, match="Normalized Markdown exceeds"):
        normalize_to_markdown(
            filename="large.txt",
            media_type="text/plain",
            parsed=parsed,
            classification=classification,
            source_uri="upload://large.txt",
            content_hash="a" * 64,
        )
