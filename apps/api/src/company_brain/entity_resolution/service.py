from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from uuid import UUID

from company_brain.domain.models import Entity, EntityType

MAX_CANDIDATES = 10
MIN_FUZZY_SCORE = 0.45
MAX_NAME_LENGTH = 500
MAX_IDENTIFIERS = 16
MAX_IDENTIFIER_LENGTH = 320
ALLOWED_IDENTIFIER_KEYS = frozenset({"vat", "tax_id", "email", "domain", "phone"})


@dataclass(frozen=True)
class ResolutionInput:
    entity_type: EntityType
    name: str
    identifiers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.name) > MAX_NAME_LENGTH:
            raise ValueError("name must be non-empty and bounded")
        if len(self.identifiers) > MAX_IDENTIFIERS:
            raise ValueError("too many identifiers")
        for key, value in self.identifiers.items():
            if key not in ALLOWED_IDENTIFIER_KEYS:
                raise ValueError("unsupported identifier key")
            if not key.strip() or not value.strip():
                raise ValueError("identifier keys and values must be non-empty")
            if len(key) > 100 or len(value) > MAX_IDENTIFIER_LENGTH:
                raise ValueError("identifier keys and values must be bounded")


@dataclass(frozen=True)
class ResolutionCandidate:
    entity_id: UUID
    score: float
    reasons: list[str]


def normalize_value(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).strip()


def _exact_identifier_reasons(identifiers: dict[str, str], entity: Entity) -> list[str]:
    reasons: list[str] = []
    metadata = entity.metadata_ if isinstance(entity.metadata_, dict) else {}
    for key in sorted(identifiers):
        candidate_value = metadata.get(key)
        if not isinstance(candidate_value, str):
            continue
        if normalize_value(candidate_value) == normalize_value(identifiers[key]):
            reasons.append(f"exact:{key}")
    return reasons


def find_resolution_candidates(
    payload: ResolutionInput,
    entities: Iterable[Entity],
) -> list[ResolutionCandidate]:
    query_name = normalize_value(payload.name)
    ranked: list[ResolutionCandidate] = []
    for entity in entities:
        if entity.entity_type != payload.entity_type or entity.lifecycle_status != "active":
            continue
        exact_reasons = _exact_identifier_reasons(payload.identifiers, entity)
        if exact_reasons:
            ranked.append(ResolutionCandidate(entity.id, 1.0, exact_reasons))
            continue
        candidate_name = normalize_value(entity.name)
        score = SequenceMatcher(None, query_name, candidate_name, autojunk=False).ratio()
        if score >= MIN_FUZZY_SCORE:
            reason = "exact:name" if score == 1.0 else "fuzzy:name"
            ranked.append(ResolutionCandidate(entity.id, round(score, 6), [reason]))
    ranked.sort(key=lambda candidate: (-candidate.score, str(candidate.entity_id)))
    return ranked[:MAX_CANDIDATES]
