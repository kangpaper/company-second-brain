from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

_SIGNAL_WEIGHTS = {
    ("REVENUE_DECLINE", "medium"): 25,
    ("REVENUE_DECLINE", "high"): 35,
    ("PAYMENT_DELAY", "medium"): 20,
    ("PAYMENT_DELAY", "high"): 30,
    ("TICKET_INCREASE", "medium"): 15,
    ("TICKET_INCREASE", "high"): 25,
    ("DELIVERY_COMPLAINTS", "medium"): 20,
    ("DELIVERY_COMPLAINTS", "high"): 30,
}


@dataclass(frozen=True)
class RiskTicket:
    id: UUID
    attributes: dict[str, Any]
    evidence_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class RiskAssessment:
    calculation_version: str
    score: int
    severity: str
    signals: list[dict[str, Any]]
    data_gaps: list[str]


def _severity(score: int) -> str:
    if score >= 75:
        return "critical"
    if score >= 50:
        return "high"
    if score >= 25:
        return "moderate"
    return "low"


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        return None


def _ticket_increase_signal(
    tickets: list[RiskTicket], as_of: datetime, data_gaps: list[str]
) -> dict[str, Any] | None:
    recent_start = as_of - timedelta(days=30)
    prior_start = as_of - timedelta(days=60)
    recent: list[RiskTicket] = []
    prior: list[RiskTicket] = []
    for ticket in tickets:
        opened_at = _parse_timestamp(ticket.attributes.get("opened_at"))
        if opened_at is None:
            data_gaps.append(f"missing_ticket_opened_at:{ticket.id}")
            continue
        if opened_at >= as_of:
            data_gaps.append(f"future_ticket_excluded:{ticket.id}")
            continue
        if recent_start <= opened_at:
            recent.append(ticket)
        elif prior_start <= opened_at < recent_start:
            prior.append(ticket)
    if not prior:
        if recent:
            data_gaps.append("missing_ticket_increase_baseline")
        return None
    change = len(recent) - len(prior)
    ratio = len(recent) / len(prior)
    if change < 3 or ratio < 2:
        return None
    contributors = [*prior, *recent]
    if any(not ticket.evidence_ids for ticket in contributors):
        data_gaps.append("missing_risk_evidence:TICKET_INCREASE")
        return None
    evidence_ids = sorted(
        {evidence_id for ticket in contributors for evidence_id in ticket.evidence_ids}
    )
    return {
        "type": "TICKET_INCREASE",
        "severity": "high" if ratio >= 3 or change >= 5 else "medium",
        "window_days": 30,
        "recent_count": len(recent),
        "prior_count": len(prior),
        "change": change,
        "ratio": round(ratio, 10),
        "evidence_ids": [str(evidence_id) for evidence_id in evidence_ids],
    }


def _delivery_complaint_signal(
    tickets: list[RiskTicket], as_of: datetime, data_gaps: list[str]
) -> dict[str, Any] | None:
    window_start = as_of - timedelta(days=90)
    complaints: list[RiskTicket] = []
    for ticket in tickets:
        opened_at = _parse_timestamp(ticket.attributes.get("opened_at"))
        if opened_at is None:
            data_gaps.append(f"missing_ticket_opened_at:{ticket.id}")
            continue
        complaint_type = ticket.attributes.get("complaint_type")
        if (
            window_start <= opened_at < as_of
            and isinstance(complaint_type, str)
            and complaint_type.strip().casefold() in {"delivery", "delivery_complaint"}
        ):
            complaints.append(ticket)
    if not complaints:
        return None
    if any(not ticket.evidence_ids for ticket in complaints):
        data_gaps.append("missing_risk_evidence:DELIVERY_COMPLAINTS")
        return None
    evidence_ids = sorted(
        {evidence_id for ticket in complaints for evidence_id in ticket.evidence_ids}
    )
    return {
        "type": "DELIVERY_COMPLAINTS",
        "severity": "high" if len(complaints) >= 3 else "medium",
        "window_days": 90,
        "count": len(complaints),
        "evidence_ids": [str(evidence_id) for evidence_id in evidence_ids],
    }


def calculate_risk_assessment(
    *,
    base_signals: list[dict[str, Any]],
    tickets: list[RiskTicket],
    as_of: datetime,
) -> RiskAssessment:
    data_gaps: list[str] = []
    signals: list[dict[str, Any]] = []
    for signal in base_signals:
        normalized = dict(signal)
        if normalized.get("type") == "OVERDUE_PAYMENT":
            normalized["type"] = "PAYMENT_DELAY"
        if normalized.get("type") in {"PAYMENT_DELAY", "REVENUE_DECLINE"}:
            signals.append(normalized)
    ticket_signal = _ticket_increase_signal(tickets, as_of, data_gaps)
    if ticket_signal is not None:
        signals.append(ticket_signal)
    delivery_signal = _delivery_complaint_signal(tickets, as_of, data_gaps)
    if delivery_signal is not None:
        signals.append(delivery_signal)
    signals.sort(key=lambda item: (str(item["type"]), str(item.get("currency", ""))))
    component_scores: dict[str, int] = {}
    for signal in signals:
        signal_type = str(signal.get("type"))
        weight = _SIGNAL_WEIGHTS.get(
            (signal_type, str(signal.get("severity"))), 0
        )
        component_scores[signal_type] = max(component_scores.get(signal_type, 0), weight)
    score = min(100, sum(component_scores.values()))
    return RiskAssessment(
        calculation_version="customer-risk.v1",
        score=score,
        severity=_severity(score),
        signals=signals,
        data_gaps=sorted(set(data_gaps)),
    )
