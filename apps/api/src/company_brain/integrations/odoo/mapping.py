from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast

from company_brain.domain.models import EntityType


class OdooMappingError(ValueError):
    pass


@dataclass(frozen=True)
class CanonicalEntityDTO:
    source_model: str
    external_id: str
    entity_type: EntityType
    name: str
    normalized_name: str
    lifecycle_status: str
    attributes: dict[str, Any]


def extract_mcp_record(result: dict[str, Any], expected_id: int) -> dict[str, Any]:
    candidate: Any = None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        candidate = structured.get("record")
    if candidate is None:
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                text = item.get("text")
                if not isinstance(text, str) or len(text.encode()) > 1024 * 1024:
                    continue
                try:
                    candidate = json.loads(text)
                except ValueError:
                    continue
                break
    if not isinstance(candidate, dict) or type(candidate.get("id")) is not int:
        raise OdooMappingError("Odoo MCP record payload is invalid")
    if candidate["id"] != expected_id:
        raise OdooMappingError("Odoo MCP record id does not match request")
    return candidate


def required_identity(record: dict[str, Any]) -> tuple[int, str]:
    record_id = record.get("id")
    name = record.get("name")
    if type(record_id) is not int or record_id <= 0:
        raise OdooMappingError("Odoo record id must be a positive integer")
    if not isinstance(name, str):
        raise OdooMappingError("Odoo record name is required")
    clean_name = " ".join(name.split())
    if not clean_name or len(clean_name) > 500:
        raise OdooMappingError("Odoo record name is invalid")
    return record_id, clean_name


def optional_text(record: dict[str, Any], field: str, max_length: int) -> str | None:
    value = record.get(field)
    if value in (None, False, ""):
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise OdooMappingError(f"Odoo field {field} is invalid")
    return value


def optional_nonnegative_int(record: dict[str, Any], field: str) -> int:
    value = record.get(field, 0)
    if type(value) is not int or value < 0:
        raise OdooMappingError(f"Odoo field {field} is invalid")
    return value


def optional_bool(record: dict[str, Any], field: str, default: bool) -> bool:
    value = record.get(field, default)
    if type(value) is not bool:
        raise OdooMappingError(f"Odoo field {field} is invalid")
    return value


def optional_datetime(record: dict[str, Any], field: str) -> str | None:
    value = optional_text(record, field, 64)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OdooMappingError(f"Odoo field {field} is invalid") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def optional_relation_id(record: dict[str, Any], field: str) -> int | None:
    value = record.get(field)
    if value in (None, False):
        return None
    if isinstance(value, list):
        if not value:
            return None
        value = value[0]
    if type(value) is not int or value <= 0:
        raise OdooMappingError(f"Odoo field {field} is invalid")
    return value


def optional_number(record: dict[str, Any], field: str) -> float | None:
    value = record.get(field)
    if value in (None, False):
        return None
    if type(value) not in (int, float):
        raise OdooMappingError(f"Odoo field {field} is invalid")
    result = float(cast(int | float, value))
    if not math.isfinite(result):
        raise OdooMappingError(f"Odoo field {field} is invalid")
    return result


FieldSpec = tuple[str, str, int | None]


def map_standard(
    source_model: str,
    entity_type: EntityType,
    specs: tuple[FieldSpec, ...],
) -> Callable[[dict[str, Any]], CanonicalEntityDTO]:
    def mapper(record: dict[str, Any]) -> CanonicalEntityDTO:
        record_id, name = required_identity(record)
        active = optional_bool(record, "active", True)
        attributes: dict[str, Any] = {}
        for field, kind, bound in specs:
            value: Any
            if kind == "relation":
                value = optional_relation_id(record, field)
            elif kind == "number":
                value = optional_number(record, field)
            elif kind == "datetime":
                value = optional_datetime(record, field)
            else:
                value = optional_text(record, field, bound or 500)
            if value is not None:
                attributes[field] = value
        return CanonicalEntityDTO(
            source_model=source_model,
            external_id=str(record_id),
            entity_type=entity_type,
            name=name,
            normalized_name=name.casefold(),
            lifecycle_status="active" if active else "inactive",
            attributes=attributes,
        )

    return mapper


def map_partner(record: dict[str, Any]) -> CanonicalEntityDTO:
    record_id, name = required_identity(record)
    is_company = optional_bool(record, "is_company", False)
    active = optional_bool(record, "active", True)
    customer_rank = optional_nonnegative_int(record, "customer_rank")
    supplier_rank = optional_nonnegative_int(record, "supplier_rank")
    attributes: dict[str, Any] = {
        "is_company": is_company,
        "customer_rank": customer_rank,
        "supplier_rank": supplier_rank,
    }
    for field, max_length in (("email", 320), ("phone", 100), ("vat", 100)):
        value = optional_text(record, field, max_length)
        if value is not None:
            attributes[field] = value
    write_date = optional_datetime(record, "write_date")
    if write_date is not None:
        attributes["write_date"] = write_date
    if not is_company:
        entity_type = EntityType.PERSON
    elif customer_rank > 0:
        entity_type = EntityType.CUSTOMER
    elif supplier_rank > 0:
        entity_type = EntityType.SUPPLIER
    else:
        entity_type = EntityType.ORGANIZATION
    return CanonicalEntityDTO(
        source_model="res.partner",
        external_id=str(record_id),
        entity_type=entity_type,
        name=name,
        normalized_name=name.casefold(),
        lifecycle_status="active" if active else "inactive",
        attributes=attributes,
    )


def map_activity(record: dict[str, Any]) -> CanonicalEntityDTO:
    mapped_record = {**record, "name": record.get("summary")}
    mapper = map_standard(
        "mail.activity",
        EntityType.TASK,
        (
            ("res_model", "text", 255),
            ("res_id", "relation", None),
            ("user_id", "relation", None),
            ("date_deadline", "datetime", None),
        ),
    )
    return mapper(mapped_record)


def _map_with_canonical_timestamp(
    record: dict[str, Any],
    *,
    source_model: str,
    entity_type: EntityType,
    specs: tuple[FieldSpec, ...],
    source_field: str,
    canonical_field: str,
) -> CanonicalEntityDTO:
    dto = map_standard(source_model, entity_type, specs)(record)
    attributes = dict(dto.attributes)
    value = attributes.pop(source_field, None)
    if value is not None:
        attributes[canonical_field] = value
    return replace(dto, attributes=attributes)


def map_invoice(record: dict[str, Any]) -> CanonicalEntityDTO:
    return _map_with_canonical_timestamp(
        record,
        source_model="account.move",
        entity_type=EntityType.INVOICE,
        specs=(
            ("partner_id", "relation", None),
            ("amount_total", "number", None),
            ("payment_state", "text", 50),
            ("invoice_date", "datetime", None),
            ("invoice_date_due", "datetime", None),
        ),
        source_field="invoice_date_due",
        canonical_field="due_date",
    )


def map_helpdesk_ticket(record: dict[str, Any]) -> CanonicalEntityDTO:
    return _map_with_canonical_timestamp(
        record,
        source_model="helpdesk.ticket",
        entity_type=EntityType.TICKET,
        specs=(
            ("partner_id", "relation", None),
            ("user_id", "relation", None),
            ("priority", "text", 10),
            ("stage_id", "relation", None),
            ("create_date", "datetime", None),
        ),
        source_field="create_date",
        canonical_field="opened_at",
    )


MAPPERS: dict[str, Callable[[dict[str, Any]], CanonicalEntityDTO]] = {
    "res.partner": map_partner,
    "sale.order": map_standard(
        "sale.order",
        EntityType.ORDER,
        (
            ("partner_id", "relation", None),
            ("amount_total", "number", None),
            ("currency_id", "relation", None),
            ("state", "text", 50),
            ("date_order", "datetime", None),
        ),
    ),
    "account.move": map_invoice,
    "crm.lead": map_standard(
        "crm.lead",
        EntityType.OPPORTUNITY,
        (
            ("partner_id", "relation", None),
            ("user_id", "relation", None),
            ("expected_revenue", "number", None),
            ("probability", "number", None),
        ),
    ),
    "project.project": map_standard(
        "project.project",
        EntityType.PROJECT,
        (("partner_id", "relation", None), ("user_id", "relation", None)),
    ),
    "helpdesk.ticket": map_helpdesk_ticket,
    "hr.employee": map_standard(
        "hr.employee",
        EntityType.EMPLOYEE,
        (
            ("work_email", "text", 320),
            ("job_title", "text", 255),
            ("user_id", "relation", None),
        ),
    ),
    "product.product": map_standard(
        "product.product",
        EntityType.PRODUCT,
        (("default_code", "text", 100), ("list_price", "number", None)),
    ),
    "mail.activity": map_activity,
}


MAPPING_FIELDS: dict[str, list[str]] = {
    "res.partner": [
        "id",
        "name",
        "is_company",
        "customer_rank",
        "supplier_rank",
        "email",
        "phone",
        "vat",
        "active",
        "write_date",
    ],
    "sale.order": [
        "id",
        "name",
        "partner_id",
        "amount_total",
        "currency_id",
        "state",
        "date_order",
        "active",
    ],
    "account.move": [
        "id",
        "name",
        "partner_id",
        "amount_total",
        "payment_state",
        "invoice_date",
        "invoice_date_due",
        "active",
    ],
    "crm.lead": [
        "id",
        "name",
        "partner_id",
        "user_id",
        "expected_revenue",
        "probability",
        "active",
    ],
    "project.project": ["id", "name", "partner_id", "user_id", "active"],
    "helpdesk.ticket": [
        "id",
        "name",
        "partner_id",
        "user_id",
        "priority",
        "stage_id",
        "create_date",
        "active",
    ],
    "hr.employee": [
        "id",
        "name",
        "work_email",
        "job_title",
        "user_id",
        "active",
    ],
    "product.product": ["id", "name", "default_code", "list_price", "active"],
    "mail.activity": [
        "id",
        "summary",
        "res_model",
        "res_id",
        "user_id",
        "date_deadline",
        "active",
    ],
}


def map_odoo_record(source_model: str, record: dict[str, Any]) -> CanonicalEntityDTO:
    mapper = MAPPERS.get(source_model)
    if mapper is None:
        raise OdooMappingError(f"unsupported Odoo model: {source_model}")
    if not isinstance(record, dict):
        raise OdooMappingError("Odoo record must be an object")
    return mapper(record)
