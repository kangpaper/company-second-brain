from datetime import UTC, datetime
from uuid import uuid4

from company_brain.risk_engine.service import RiskTicket, calculate_risk_assessment


def test_composite_risk_maps_trusted_phase8_signals_deterministically() -> None:
    revenue_evidence = uuid4()
    payment_evidence = uuid4()

    assessment = calculate_risk_assessment(
        base_signals=[
            {
                "type": "REVENUE_DECLINE",
                "severity": "high",
                "currency": "USD",
                "value": -0.4,
                "evidence_ids": [str(revenue_evidence)],
            },
            {
                "type": "OVERDUE_PAYMENT",
                "severity": "medium",
                "count": 1,
                "evidence_ids": [str(payment_evidence)],
            },
        ],
        tickets=[],
        as_of=datetime(2026, 8, 14, tzinfo=UTC),
    )

    assert assessment.calculation_version == "customer-risk.v1"
    assert assessment.score == 55
    assert assessment.severity == "high"
    assert assessment.signals == [
        {
            "type": "PAYMENT_DELAY",
            "severity": "medium",
            "count": 1,
            "evidence_ids": [str(payment_evidence)],
        },
        {
            "type": "REVENUE_DECLINE",
            "severity": "high",
            "currency": "USD",
            "value": -0.4,
            "evidence_ids": [str(revenue_evidence)],
        },
    ]
    assert assessment.data_gaps == []


def test_ticket_increase_compares_two_anchored_windows_with_complete_evidence() -> None:
    as_of = datetime(2026, 8, 14, tzinfo=UTC)
    ticket_specs = [
        ("2026-06-20T00:00:00Z", uuid4()),
        ("2026-07-20T00:00:00Z", uuid4()),
        ("2026-07-25T00:00:00Z", uuid4()),
        ("2026-08-01T00:00:00Z", uuid4()),
        ("2026-08-10T00:00:00Z", uuid4()),
    ]
    tickets = [
        RiskTicket(
            id=uuid4(),
            attributes={"opened_at": opened_at},
            evidence_ids=(evidence_id,),
        )
        for opened_at, evidence_id in ticket_specs
    ]

    assessment = calculate_risk_assessment(
        base_signals=[], tickets=tickets, as_of=as_of
    )

    assert assessment.score == 25
    assert assessment.severity == "moderate"
    assert assessment.signals == [
        {
            "type": "TICKET_INCREASE",
            "severity": "high",
            "window_days": 30,
            "recent_count": 4,
            "prior_count": 1,
            "change": 3,
            "ratio": 4.0,
            "evidence_ids": [
                str(evidence_id)
                for evidence_id in sorted(item[1] for item in ticket_specs)
            ],
        }
    ]
    assert assessment.data_gaps == []


def test_delivery_complaints_use_explicit_canonical_type_and_ninety_day_window() -> None:
    complaint_evidence = [uuid4(), uuid4()]
    tickets = [
        RiskTicket(
            id=uuid4(),
            attributes={"opened_at": "2026-06-20T00:00:00Z"},
            evidence_ids=(uuid4(),),
        ),
        RiskTicket(
            id=uuid4(),
            attributes={
                "opened_at": "2026-07-20T00:00:00Z",
                "complaint_type": "delivery",
            },
            evidence_ids=(complaint_evidence[0],),
        ),
        RiskTicket(
            id=uuid4(),
            attributes={
                "opened_at": "2026-08-01T00:00:00Z",
                "complaint_type": "delivery_complaint",
            },
            evidence_ids=(complaint_evidence[1],),
        ),
    ]

    assessment = calculate_risk_assessment(
        base_signals=[],
        tickets=tickets,
        as_of=datetime(2026, 8, 14, tzinfo=UTC),
    )

    assert assessment.score == 20
    assert assessment.severity == "low"
    assert assessment.signals == [
        {
            "type": "DELIVERY_COMPLAINTS",
            "severity": "medium",
            "window_days": 90,
            "count": 2,
            "evidence_ids": [str(item) for item in sorted(complaint_evidence)],
        }
    ]
    assert assessment.data_gaps == []


def test_ticket_increase_is_suppressed_when_any_contributor_lacks_evidence() -> None:
    tickets = [
        RiskTicket(
            id=uuid4(),
            attributes={"opened_at": opened_at},
            evidence_ids=(uuid4(),) if index != 3 else (),
        )
        for index, opened_at in enumerate(
            [
                "2026-06-20T00:00:00Z",
                "2026-07-20T00:00:00Z",
                "2026-07-25T00:00:00Z",
                "2026-08-01T00:00:00Z",
                "2026-08-10T00:00:00Z",
            ]
        )
    ]

    assessment = calculate_risk_assessment(
        base_signals=[],
        tickets=tickets,
        as_of=datetime(2026, 8, 14, tzinfo=UTC),
    )

    assert assessment.signals == []
    assert assessment.score == 0
    assert assessment.data_gaps == ["missing_risk_evidence:TICKET_INCREASE"]


def test_delivery_complaints_are_suppressed_when_evidence_is_incomplete() -> None:
    tickets = [
        RiskTicket(
            id=uuid4(),
            attributes={"opened_at": "2026-06-20T00:00:00Z"},
            evidence_ids=(uuid4(),),
        ),
        RiskTicket(
            id=uuid4(),
            attributes={
                "opened_at": "2026-08-01T00:00:00Z",
                "complaint_type": "delivery",
            },
            evidence_ids=(),
        ),
    ]

    assessment = calculate_risk_assessment(
        base_signals=[],
        tickets=tickets,
        as_of=datetime(2026, 8, 14, tzinfo=UTC),
    )

    assert assessment.signals == []
    assert assessment.score == 0
    assert assessment.data_gaps == ["missing_risk_evidence:DELIVERY_COMPLAINTS"]


def test_composite_score_caps_each_signal_type_at_its_highest_severity() -> None:
    assessment = calculate_risk_assessment(
        base_signals=[
            {
                "type": "REVENUE_DECLINE",
                "severity": "medium",
                "currency": "EUR",
                "value": -0.25,
                "evidence_ids": [str(uuid4())],
            },
            {
                "type": "REVENUE_DECLINE",
                "severity": "high",
                "currency": "USD",
                "value": -0.5,
                "evidence_ids": [str(uuid4())],
            },
        ],
        tickets=[],
        as_of=datetime(2026, 8, 14, tzinfo=UTC),
    )

    assert len(assessment.signals) == 2
    assert assessment.score == 35
    assert assessment.severity == "moderate"


def test_all_four_risk_components_cap_at_one_hundred() -> None:
    tickets = [
        RiskTicket(
            id=uuid4(),
            attributes={
                "opened_at": opened_at,
                **({"complaint_type": "delivery"} if index in {1, 2, 3} else {}),
            },
            evidence_ids=(uuid4(),),
        )
        for index, opened_at in enumerate(
            [
                "2026-06-20T00:00:00Z",
                "2026-07-16T00:00:00Z",
                "2026-07-20T00:00:00Z",
                "2026-07-25T00:00:00Z",
                "2026-08-01T00:00:00Z",
                "2026-08-10T00:00:00Z",
            ]
        )
    ]
    assessment = calculate_risk_assessment(
        base_signals=[
            {
                "type": "REVENUE_DECLINE",
                "severity": "high",
                "currency": "USD",
                "value": -0.5,
                "evidence_ids": [str(uuid4())],
            },
            {
                "type": "OVERDUE_PAYMENT",
                "severity": "high",
                "count": 4,
                "evidence_ids": [str(uuid4())],
            },
        ],
        tickets=tickets,
        as_of=datetime(2026, 8, 14, tzinfo=UTC),
    )

    assert {signal["type"] for signal in assessment.signals} == {
        "REVENUE_DECLINE",
        "PAYMENT_DELAY",
        "TICKET_INCREASE",
        "DELIVERY_COMPLAINTS",
    }
    assert assessment.score == 100
    assert assessment.severity == "critical"
