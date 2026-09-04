from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Literal

ContextIntent = Literal["CUSTOMER_360"]
MAX_QUESTION_LENGTH = 2_000

_GENERIC_CUSTOMER_STATUS = (
    re.compile(r"^tinh hinh khach hang (?:hien tai )?the nao$"),
    re.compile(r"^(?:cho toi xem )?(?:tong quan|trang thai) khach hang(?: hien tai)?$"),
    re.compile(
        r"^(?:give me |show me )?(?:the )?(?:current )?customer "
        r"(?:overview|status)$"
    ),
)
_NAMED_CUSTOMER_STATUS = (
    re.compile(
        r"^tinh hinh khach hang (?P<label>[a-z0-9][a-z0-9 .&-]{0,100}?) "
        r"(?:hien tai )?the nao$"
    ),
    re.compile(
        r"^(?:give me |show me )?(?:the )?(?:current )?customer "
        r"(?:overview|status) for (?P<label>[a-z0-9][a-z0-9 .&-]{0,100})$"
    ),
)
_SHORTHAND_CUSTOMER_STATUS = re.compile(
    r"^tinh hinh (?P<label>[a-z0-9][a-z0-9 .&-]{0,100}?) "
    r"(?:hien tai )?the nao$"
)


_ALLOWED_LATIN_MARKS = {
    "\u0300",  # grave
    "\u0301",  # acute
    "\u0302",  # circumflex
    "\u0303",  # tilde
    "\u0304",  # macron
    "\u0306",  # breve
    "\u0307",  # dot above
    "\u0308",  # diaeresis
    "\u0309",  # hook above
    "\u030a",  # ring above
    "\u030b",  # double acute
    "\u030c",  # caron
    "\u0323",  # dot below
    "\u0327",  # cedilla
    "\u0328",  # ogonek
    "\u031b",  # horn
}


def _is_allowed_original_character(character: str) -> bool:
    category = unicodedata.category(character)
    if character.isascii():
        return category[0] in {"L", "N", "P", "Z"}
    if category.startswith("L"):
        return "LATIN" in unicodedata.name(character, "")
    if category.startswith("M"):
        return character in _ALLOWED_LATIN_MARKS
    return category.startswith(("P", "Z"))


def _normalize_question(question: str) -> str | None:
    if any(not _is_allowed_original_character(character) for character in question):
        return None
    question = question.replace("Đ", "D").replace("đ", "d")
    decomposed = unicodedata.normalize("NFD", question)
    ascii_parts: list[str] = []
    follows_ascii_letter = False
    for character in decomposed:
        category = unicodedata.category(character)
        if character.isascii() and category[0] in {"L", "N"}:
            ascii_parts.append(character.casefold())
            follows_ascii_letter = category.startswith("L")
            continue
        if character in _ALLOWED_LATIN_MARKS and follows_ascii_letter:
            continue
        if category.startswith(("P", "Z")):
            ascii_parts.append(" ")
            follows_ascii_letter = False
            continue
        return None
    return re.sub(r"[^a-z0-9]+", " ", "".join(ascii_parts)).strip()


def _normalized_labels(customer_labels: Iterable[str]) -> set[str]:
    return {
        normalized
        for label in customer_labels
        if (normalized := _normalize_question(label))
    }


def is_potential_customer_status_question(question: str) -> bool:
    if not question.strip() or len(question) > MAX_QUESTION_LENGTH:
        return False
    normalized = _normalize_question(question)
    if normalized is None:
        return False
    patterns = (*_GENERIC_CUSTOMER_STATUS, *_NAMED_CUSTOMER_STATUS)
    return any(pattern.fullmatch(normalized) for pattern in patterns) or (
        _SHORTHAND_CUSTOMER_STATUS.fullmatch(normalized) is not None
    )


def detect_intent(
    question: str, *, customer_labels: Iterable[str] = ()
) -> ContextIntent | None:
    if not question.strip() or len(question) > MAX_QUESTION_LENGTH:
        return None
    normalized = _normalize_question(question)
    if normalized is None:
        return None
    if any(pattern.fullmatch(normalized) for pattern in _GENERIC_CUSTOMER_STATUS):
        return "CUSTOMER_360"
    labels = _normalized_labels(customer_labels)
    for pattern in (*_NAMED_CUSTOMER_STATUS, _SHORTHAND_CUSTOMER_STATUS):
        match = pattern.fullmatch(normalized)
        if match is not None and match.group("label") in labels:
            return "CUSTOMER_360"
    return None
