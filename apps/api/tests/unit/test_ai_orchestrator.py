from uuid import uuid4

import pytest

from company_brain.ai.orchestrator import (
    GroundingError,
    ProviderDraft,
    validate_provider_draft,
)
from company_brain.api.ai import DeterministicGroundedProvider


def test_provider_draft_accepts_only_context_evidence_citations() -> None:
    allowed = uuid4()
    draft = ProviderDraft(
        answer="Revenue is supported by the cited order evidence.",
        citation_ids=[allowed],
        uncertainty="Activity history is incomplete.",
    )

    validated = validate_provider_draft(draft, evidence_ids={allowed})

    assert validated == draft


def test_provider_draft_rejects_unknown_citations_and_missing_uncertainty() -> None:
    allowed = uuid4()
    unknown = uuid4()

    with pytest.raises(GroundingError, match="citation"):
        validate_provider_draft(
            ProviderDraft(answer="Answer", citation_ids=[], uncertainty="Unknown."),
            evidence_ids={allowed},
        )
    with pytest.raises(GroundingError, match="unknown citation"):
        validate_provider_draft(
            ProviderDraft(answer="Unsupported", citation_ids=[unknown], uncertainty="Unknown."),
            evidence_ids={allowed},
        )
    with pytest.raises(GroundingError, match="uncertainty"):
        validate_provider_draft(
            ProviderDraft(answer="Answer", citation_ids=[allowed], uncertainty="   "),
            evidence_ids={allowed},
        )


def test_provider_draft_rejects_provider_text_with_nul() -> None:
    allowed = uuid4()

    for answer, uncertainty in (
        ("Grounded\x00but invalid", "Bounded."),
        ("Grounded.", "Bounded\x00but invalid"),
    ):
        with pytest.raises(GroundingError, match="unsupported control character"):
            validate_provider_draft(
                ProviderDraft(
                    answer=answer,
                    citation_ids=[allowed],
                    uncertainty=uncertainty,
                ),
                evidence_ids={allowed},
            )


def test_default_provider_caps_citations_deterministically() -> None:
    evidence_ids = [uuid4() for _ in range(101)]
    context = {
        "customer": {"name": "ABC"},
        "metrics": {"revenue_total": {"values": []}},
        "evidence": [{"id": str(item)} for item in evidence_ids],
        "data_gaps": [],
    }

    draft = DeterministicGroundedProvider().generate(
        question="Tình hình ABC thế nào?", context=context
    )

    assert draft.citation_ids == evidence_ids[:100]
    assert validate_provider_draft(draft, evidence_ids=set(evidence_ids)) == draft
