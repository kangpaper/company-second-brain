from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

MAX_ANSWER_LENGTH = 20_000
MAX_UNCERTAINTY_LENGTH = 2_000
MAX_CITATIONS = 100


class GroundingError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderDraft:
    answer: str
    citation_ids: list[UUID]
    uncertainty: str


class AIProvider(Protocol):
    provider_name: str
    model_name: str

    def generate(self, *, question: str, context: dict[str, Any]) -> ProviderDraft: ...


def validate_provider_draft(
    draft: ProviderDraft, *, evidence_ids: set[UUID]
) -> ProviderDraft:
    if not draft.answer.strip() or len(draft.answer) > MAX_ANSWER_LENGTH:
        raise GroundingError("answer is missing or too large")
    if "\x00" in draft.answer:
        raise GroundingError("answer contains an unsupported control character")
    if not draft.uncertainty.strip() or len(draft.uncertainty) > MAX_UNCERTAINTY_LENGTH:
        raise GroundingError("uncertainty is required and bounded")
    if "\x00" in draft.uncertainty:
        raise GroundingError("uncertainty contains an unsupported control character")
    if not draft.citation_ids:
        raise GroundingError("at least one citation is required")
    if len(draft.citation_ids) > MAX_CITATIONS:
        raise GroundingError("too many citations")
    if len(set(draft.citation_ids)) != len(draft.citation_ids):
        raise GroundingError("duplicate citation")
    if any(citation_id not in evidence_ids for citation_id in draft.citation_ids):
        raise GroundingError("unknown citation")
    return draft
