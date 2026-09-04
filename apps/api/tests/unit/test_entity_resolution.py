from uuid import uuid4

from sqlalchemy.dialects import postgresql

from company_brain.api.entity_resolution import _candidate_for_decision_statement
from company_brain.domain.models import Entity, EntityType
from company_brain.domain.repositories import TenantScope
from company_brain.entity_resolution.service import (
    ResolutionInput,
    find_resolution_candidates,
)


def entity(scope: TenantScope, name: str, **metadata: str) -> Entity:
    return Entity(
        id=uuid4(),
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        entity_type=EntityType.CUSTOMER,
        name=name,
        normalized_name=name.casefold(),
        metadata_=metadata,
        lifecycle_status="active",
    )


def test_candidate_decision_locks_entity_before_eligibility_check() -> None:
    scope = TenantScope(uuid4(), uuid4())
    statement = _candidate_for_decision_statement(scope, uuid4(), EntityType.CUSTOMER)

    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql


def test_exact_identifier_outranks_fuzzy_name_and_is_deterministic() -> None:
    scope = TenantScope(uuid4(), uuid4())
    exact = entity(scope, "Acme Holdings", vat="VN-123")
    fuzzy = entity(scope, "Acme Corporation", vat="VN-999")

    candidates = find_resolution_candidates(
        ResolutionInput(
            entity_type=EntityType.CUSTOMER,
            name="Acme Corp",
            identifiers={"vat": " vn-123 "},
        ),
        [fuzzy, exact],
    )

    assert [candidate.entity_id for candidate in candidates] == [exact.id, fuzzy.id]
    assert candidates[0].score == 1.0
    assert candidates[0].reasons == ["exact:vat"]
    assert 0.0 < candidates[1].score < 1.0


def test_exact_normalized_name_has_explicit_reason() -> None:
    scope = TenantScope(uuid4(), uuid4())
    exact = entity(scope, "Àcme Corporation")

    candidates = find_resolution_candidates(
        ResolutionInput(entity_type=EntityType.CUSTOMER, name="acme corporation"),
        [exact],
    )

    assert candidates[0].score == 1.0
    assert candidates[0].reasons == ["exact:name"]


def test_arbitrary_identifier_key_is_rejected() -> None:
    try:
        ResolutionInput(
            entity_type=EntityType.CUSTOMER,
            name="Acme",
            identifiers={"private_internal_note": "secret"},
        )
    except ValueError as error:
        assert str(error) == "unsupported identifier key"
    else:
        raise AssertionError("unsupported identifier key was accepted")


def test_candidates_are_type_scoped_bounded_and_exclude_merged_entities() -> None:
    scope = TenantScope(uuid4(), uuid4())
    candidates = [entity(scope, f"Acme {index}") for index in range(30)]
    candidates[0].lifecycle_status = "merged"
    wrong_type = entity(scope, "Acme Corp")
    wrong_type.entity_type = EntityType.SUPPLIER

    ranked = find_resolution_candidates(
        ResolutionInput(entity_type=EntityType.CUSTOMER, name="Acme Corp"),
        [*candidates, wrong_type],
    )

    assert len(ranked) == 10
    assert candidates[0].id not in {item.entity_id for item in ranked}
    assert wrong_type.id not in {item.entity_id for item in ranked}
    assert ranked == sorted(ranked, key=lambda item: (-item.score, str(item.entity_id)))


def test_low_similarity_names_are_not_candidates() -> None:
    scope = TenantScope(uuid4(), uuid4())

    ranked = find_resolution_candidates(
        ResolutionInput(entity_type=EntityType.CUSTOMER, name="Acme Corporation"),
        [entity(scope, "Completely Different")],
    )

    assert ranked == []
