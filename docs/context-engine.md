# Context Engine

## Purpose
Transform intent + entity into a structured, evidenced context object. This is the main differentiator from classic RAG.

## Target responsibilities
Intent-to-plan mapping, entity slot resolution, structured retrieval, semantic retrieval, relationship traversal, timeline retrieval, memory retrieval, deterministic metrics, business signals, evidence bundle assembly.

The current Phase 9 implementation uses deterministic structured, relationship,
timeline, memory, document, and Evidence retrieval. Persisted embeddings and
semantic-similarity retrieval are deferred; they are not part of the current
context contract.

## Customer 360 context contract

Phase 9 exposes `POST /api/v1/context/build`. The caller supplies a bounded
business question, an explicit canonical `customer_id`, and a timezone-aware
`as_of`. Requiring the canonical ID keeps ambiguous name resolution in the
Phase 7 review workflow instead of auto-selecting a fuzzy candidate. The
current deterministic intent grammar accepts only fully anchored, read-only
customer-status forms. Generic forms must explicitly identify `customer`/`khách
hàng`; named forms and the shorthand “Tình hình <customer label> thế nào?” must
match the exact normalized canonical customer name or a sanitized alias from the
authenticated historical projection. Prefixes/suffixes, write/action requests,
negations, and unrelated subjects therefore fail closed with `422`. Normalization
accepts ASCII letters/digits/punctuation/spacing plus Latin-script letters and an
explicit set of Vietnamese/Latin combining diacritics. Original code points are
validated before canonical NFD decomposition; compatibility folding is not used.
Controls, format characters, non-Latin or mixed-script letters, symbols, emoji,
variation selectors, enclosing/script-specific marks, and unsupported combining
marks are rejected rather than silently deleted. Questions are not sent to an
LLM.

The response envelope is:
```json
{
  "schema_version": "customer_360.v1",
  "intent": "CUSTOMER_360",
  "entity": {"id": "<uuid>", "type": "customer", "name": "ABC Ltd."},
  "as_of": "2026-08-14T00:00:00Z",
  "context_hash": "<sha256>",
  "context": {
    "customer": {},
    "metrics": {},
    "signals": [],
    "timeline": [],
    "memories": [],
    "documents": [],
    "relationships": [],
    "evidence": [],
    "data_gaps": []
  }
}
```

`context` reuses the exact bounded, tenant/workspace-scoped Phase 8 response
builder. `context_hash` is SHA-256 over canonical JSON containing schema
version, intent, canonical entity reference, normalized UTC `as_of`, and the
complete context. Question wording is intentionally excluded, so paraphrases
that map to the same intent and snapshot have the same hash; data or `as_of`
changes produce a different hash.

## Algorithm
1. Detect intent.
2. Map to required context slots.
3. Resolve entity.
4. Check freshness/permissions.
5. Retrieve profile/relationships/business records.
6. Retrieve documents/memories.
7. Build timeline.
8. Calculate metrics and signals.
9. Return schema-valid context with evidence and data gaps.

Acceptance: no LLM-only metric values; every metric has calculation metadata/evidence; context is deterministic for same data.
