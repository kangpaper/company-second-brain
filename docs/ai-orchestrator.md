# AI Orchestrator

## Phase 10 scope

Phase 10 exposes a grounded, read-only reasoning boundary over the exact Phase 9
Context Engine response. It does not recalculate metrics, modify deterministic
signals, select a customer heuristically, or perform tool/action writes.

## API

### Ask

`POST /api/v1/ai/ask` is member-readable and requires the normal bearer token,
`X-Organization-ID`, and `X-Workspace-ID` headers. The strict request body is:

```json
{
  "question": "Tình hình khách hàng ABC hiện tại thế nào?",
  "customer_id": "00000000-0000-0000-0000-000000000000",
  "as_of": "2026-08-14T00:00:00Z"
}
```

Extra fields are rejected. `customer_id`, intent grammar, tenant/workspace scope,
canonical lifecycle rules, timezone-aware `as_of`, historical filtering,
provenance bounds, and context hashing are inherited unchanged from Phase 9.

A successful response contains:

- `reasoning_run_id` and the canonical SHA-256 `context_hash`;
- provider-authored `answer`, bounded `citation_ids`, and required `uncertainty`;
- deterministic `metrics` and `signals` copied directly from trusted context.

The provider output type has no metric or signal fields. The provider therefore
cannot override deterministic values in the response.

### Audit readback

`GET /api/v1/reasoning-runs/{run_id}` returns a run only inside the authenticated
organization/workspace. A cross-scope or unknown ID returns `404`.

## Provider contract

`AIProvider` exposes bounded `provider_name` and `model_name` metadata and one
`generate(question, context)` operation returning a `ProviderDraft`:

- nonblank answer, at most 20,000 characters, with no NUL character;
- 1–100 unique citation UUIDs, all present in supplied context evidence;
- nonblank uncertainty, at most 2,000 characters, with no NUL character.

The provider is injected as a factory and constructed inside the endpoint's
sanitized audit boundary. Construction, metadata, generation, and grounding
failures therefore all create a failed run without exposing raw exception text.
The built-in deterministic local provider is a testable default and selects the
first 100 context evidence records in deterministic Context Engine order.

## Insufficient evidence and failures

A context with no evidence fails closed before provider invocation:

- HTTP `422`, `Insufficient evidence for grounded answer`;
- failed run with `error_code=insufficient_evidence` and provider/model
  `not-invoked`.

Unknown, duplicate, missing, or excessive provider citations produce a sanitized
HTTP `502` with `error_code=invalid_grounding`. Other provider-boundary failures
produce a sanitized HTTP `502` with `error_code=provider_failure`. Raw provider
exceptions are neither returned nor persisted.

## Durable ReasoningRun audit

Every provider attempt after successful context construction persists one
organization/workspace-scoped `reasoning_runs` row. Successful rows include
answer, uncertainty, and citation IDs; failed rows include only sanitized error
code/message. The row records actor, customer, context hash, provider, model, and
prompt version.

PostgreSQL enforces:

- actor has a Membership in the exact organization/workspace at insert time;
- the target entity is an active customer in that scope at insert time;
- canonical 64-character lowercase hexadecimal context hash;
- nonblank bounded provider/model/prompt and state-dependent fields;
- successful citation JSON as an array of 1–100 unique UUID strings;
- each cited Evidence row is materialized in an append-only
  `reasoning_run_citations` association with tenant-composite foreign keys;
- the Evidence foreign key prevents citations from becoming dangling;
- failed citations are exactly an empty array with no association rows;
- both audit rows and citation associations reject mutation and truncation.

Application validation remains defense in depth; direct database writes cannot
bypass the critical audit shape and tenant-evidence rules.

## Verification

Run the final Phase 10 PostgreSQL and TCP gate with:

```bash
TEST_DATABASE_URL='postgresql+psycopg://…' \
  .venv/Scripts/python scripts/verify_phase10_runtime.py
```

The verifier creates a temporary database, migrates zero-to-head, checks Alembic
drift, runs PostgreSQL audit tests, and starts a real TCP Uvicorn server with a
test-only provider harness. It exercises grounded success plus a provider failure,
asserts sanitized `502`, failed-audit persistence/readback and no secret leakage,
checks cross-tenant denial, then stops the server and drops the database.
