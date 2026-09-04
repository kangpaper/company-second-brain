import pytest

from company_brain.domain.models import EntityType
from company_brain.integrations.odoo.mapping import (
    OdooMappingError,
    map_odoo_record,
)


def test_company_partner_maps_to_bounded_customer_dto() -> None:
    dto = map_odoo_record(
        "res.partner",
        {
            "id": 42,
            "name": "  Acme Corporation  ",
            "is_company": True,
            "customer_rank": 3,
            "supplier_rank": 0,
            "email": "sales@example.com",
            "phone": "+84 123",
            "vat": "VN-123",
            "active": True,
            "write_date": "2026-08-12 14:30:00",
            "secret_remote_field": "must-not-cross-boundary",
        },
    )

    assert dto.source_model == "res.partner"
    assert dto.external_id == "42"
    assert dto.entity_type is EntityType.CUSTOMER
    assert dto.name == "Acme Corporation"
    assert dto.normalized_name == "acme corporation"
    assert dto.lifecycle_status == "active"
    assert dto.attributes == {
        "email": "sales@example.com",
        "phone": "+84 123",
        "vat": "VN-123",
        "is_company": True,
        "customer_rank": 3,
        "supplier_rank": 0,
        "write_date": "2026-08-12T14:30:00+00:00",
    }
    assert "secret_remote_field" not in dto.attributes


def test_individual_partner_maps_to_person() -> None:
    dto = map_odoo_record(
        "res.partner",
        {"id": 7, "name": "Jane Doe", "is_company": False, "active": False},
    )

    assert dto.entity_type is EntityType.PERSON
    assert dto.lifecycle_status == "inactive"


def test_supplier_only_company_partner_maps_to_supplier() -> None:
    dto = map_odoo_record(
        "res.partner",
        {
            "id": 8,
            "name": "Supply Co",
            "is_company": True,
            "customer_rank": 0,
            "supplier_rank": 2,
        },
    )

    assert dto.entity_type is EntityType.SUPPLIER


def test_neutral_company_partner_maps_to_organization() -> None:
    dto = map_odoo_record(
        "res.partner",
        {"id": 9, "name": "Holding Co", "is_company": True},
    )

    assert dto.entity_type is EntityType.ORGANIZATION


def test_activity_uses_odoo_summary_as_canonical_name() -> None:
    dto = map_odoo_record(
        "mail.activity",
        {
            "id": 17,
            "summary": "Follow up",
            "res_model": "crm.lead",
            "res_id": 12,
        },
    )

    assert dto.name == "Follow up"


@pytest.mark.parametrize(
    "record",
    [
        {"id": True, "name": "Bad Bool ID"},
        {"id": "42", "name": "Coerced ID"},
        {"id": 42, "name": "   "},
        {"id": 42, "name": "x" * 501},
    ],
)
def test_partner_mapper_rejects_invalid_identity(record: dict[str, object]) -> None:
    with pytest.raises(OdooMappingError):
        map_odoo_record("res.partner", record)


def test_unknown_odoo_model_is_rejected() -> None:
    with pytest.raises(OdooMappingError, match="unsupported Odoo model"):
        map_odoo_record("x.custom", {"id": 1, "name": "Unknown"})


def test_mcp_structured_record_is_extracted_with_expected_id() -> None:
    from company_brain.integrations.odoo.mapping import extract_mcp_record

    record = extract_mcp_record(
        {"structuredContent": {"record": {"id": 42, "name": "Acme"}}}, 42
    )

    assert record == {"id": 42, "name": "Acme"}


def test_mcp_json_text_record_is_extracted() -> None:
    from company_brain.integrations.odoo.mapping import extract_mcp_record

    record = extract_mcp_record(
        {"content": [{"type": "text", "text": '{"id":42,"name":"Acme"}'}]},
        42,
    )

    assert record["name"] == "Acme"


@pytest.mark.parametrize(
    "result",
    [
        {"structuredContent": {"record": {"id": 41, "name": "Wrong"}}},
        {"structuredContent": {"record": [{"id": 42, "name": "List"}]}},
        {"content": [{"type": "text", "text": "not-json"}]},
        {"content": []},
    ],
)
def test_mcp_record_extraction_rejects_invalid_or_mismatched_payload(
    result: dict[str, object],
) -> None:
    from company_brain.integrations.odoo.mapping import extract_mcp_record

    with pytest.raises(OdooMappingError):
        extract_mcp_record(result, 42)


@pytest.mark.parametrize(
    ("model", "record", "entity_type", "expected_attributes"),
    [
        (
            "sale.order",
            {
                "id": 10,
                "name": "SO0010",
                "partner_id": [42, "Acme"],
                "amount_total": 1250.5,
                "currency_id": [2, "USD"],
                "state": "sale",
                "date_order": "2026-08-01 10:00:00",
            },
            EntityType.ORDER,
            {
                "partner_id": 42,
                "amount_total": 1250.5,
                "currency_id": 2,
                "state": "sale",
                "date_order": "2026-08-01T10:00:00+00:00",
            },
        ),
        (
            "account.move",
            {
                "id": 11,
                "name": "INV/0011",
                "partner_id": 42,
                "amount_total": 99,
                "payment_state": "paid",
                "invoice_date": "2026-08-02",
                "invoice_date_due": "2026-08-31",
            },
            EntityType.INVOICE,
            {
                "partner_id": 42,
                "amount_total": 99.0,
                "payment_state": "paid",
                "invoice_date": "2026-08-02T00:00:00+00:00",
                "due_date": "2026-08-31T00:00:00+00:00",
            },
        ),
        (
            "crm.lead",
            {
                "id": 12,
                "name": "Expansion",
                "partner_id": [42, "Acme"],
                "user_id": [8, "Owner"],
                "expected_revenue": 5000,
                "probability": 60,
                "active": True,
            },
            EntityType.OPPORTUNITY,
            {"partner_id": 42, "user_id": 8, "expected_revenue": 5000.0, "probability": 60.0},
        ),
        (
            "project.project",
            {"id": 13, "name": "ERP Rollout", "partner_id": 42, "user_id": 8, "active": True},
            EntityType.PROJECT,
            {"partner_id": 42, "user_id": 8},
        ),
        (
            "helpdesk.ticket",
            {
                "id": 14,
                "name": "Delivery issue",
                "partner_id": 42,
                "user_id": 8,
                "priority": "3",
                "stage_id": [4, "Open"],
                "create_date": "2026-08-03 09:00:00",
            },
            EntityType.TICKET,
            {
                "partner_id": 42,
                "user_id": 8,
                "priority": "3",
                "stage_id": 4,
                "opened_at": "2026-08-03T09:00:00+00:00",
            },
        ),
        (
            "hr.employee",
            {
                "id": 15,
                "name": "Jane Employee",
                "work_email": "jane@example.com",
                "job_title": "Engineer",
                "user_id": 8,
                "active": True,
            },
            EntityType.EMPLOYEE,
            {"work_email": "jane@example.com", "job_title": "Engineer", "user_id": 8},
        ),
        (
            "product.product",
            {
                "id": 16,
                "name": "Service Plan",
                "default_code": "PLAN",
                "list_price": 200,
                "active": True,
            },
            EntityType.PRODUCT,
            {"default_code": "PLAN", "list_price": 200.0},
        ),
        (
            "mail.activity",
            {
                "id": 17,
                "summary": "Follow up",
                "res_model": "crm.lead",
                "res_id": 12,
                "user_id": 8,
                "date_deadline": "2026-08-20",
            },
            EntityType.TASK,
            {
                "res_model": "crm.lead",
                "res_id": 12,
                "user_id": 8,
                "date_deadline": "2026-08-20T00:00:00+00:00",
            },
        ),
    ],
)
def test_supported_odoo_models_map_to_canonical_dtos(
    model: str,
    record: dict[str, object],
    entity_type: EntityType,
    expected_attributes: dict[str, object],
) -> None:
    dto = map_odoo_record(model, {**record, "remote_secret": "drop-me"})

    assert dto.source_model == model
    assert dto.external_id == str(record["id"])
    assert dto.entity_type is entity_type
    assert dto.attributes == expected_attributes
    assert "remote_secret" not in dto.attributes
