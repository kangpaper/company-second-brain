# Domain Model

## Aggregate roots
- Organization: tenant boundary.
- Workspace: isolated knowledge/business context inside an organization.
- User/Membership/Role: auth and authorization.
- Entity: canonical business object.
- Relationship: typed edge between two canonical Entities.
- Event: time-indexed occurrence/state transition.
- Source: origin of a fact (Odoo record, document, email, meeting note, upload, web clip, API response).
- SourceAsset: immutable exact original bytes and hash attached to one tenant-scoped Source.
- IngestionRun: audited parser/classification/normalized-Markdown and operator-review lifecycle.
- Evidence: pointer to a field/span/calculation supporting a fact.
- EvidenceLink: tenant-safe attachment from Evidence to exactly one Entity, Relationship, Event, Document, or Memory.
- Document: Markdown/content artifact with properties/tags/links/backlinks.
- Memory: durable organizational fact with type, confidence, review status.
- ReasoningRun: auditable AI execution record.

Deterministic metrics and business signals are computed read-model concepts; the
current schema does not persist `business_metrics` or `business_signals` tables.

## Entity base fields
`id`, `organization_id`, `workspace_id`, `entity_type`, `name`, `normalized_name`, `aliases`, `metadata`, `lifecycle_status`, `created_at`, `updated_at`.

## Required entity types
Organization, Person, Employee, Customer, Supplier, Product, Order, Invoice, Opportunity, Project, Ticket, Meeting, Email, Document, Decision, Event, Task.

## External references
Unique per `(organization_id, workspace_id, source_id, source_model, external_id)` and mapped to canonical entities. `source_id` identifies the exact connector/source instance, so the same Odoo model and record ID can coexist across workspaces or Odoo instances. Example: Customer ABC ⇢ Odoo `res.partner/123`.

## Entity resolution
- `EntityResolutionCase` stores a tenant-scoped, bounded candidate snapshot for
  ambiguous resolution. A pending case may transition once to matched or
  dismissed; fuzzy similarity alone never mutates canonical identity.
- `EntityMerge` is a reversible journal from a source entity to a surviving
  target entity. Only active, same-type entities in one workspace may merge.
  The source becomes a tombstone while external references, relationships,
  events, memories, and evidence links are reassigned atomically.
- Split is allowed only while the journal remains active and every journal-owned
  record still has its expected post-merge owner. It fails closed on drift rather
  than overwriting subsequent business changes. Alias history is preserved
  non-destructively because array values cannot prove whether an alias was later
  removed and independently re-added.
- Merge fails closed if rewiring would create a duplicate typed graph edge.
  Active relationships cannot be self-loops and typed edges are unique per
  tenant/workspace and ordered endpoints.
- PostgreSQL protects immutable merge-journal identity and snapshots; the only
  permitted state transition is `active` to `split` with a recorded actor.
- Database owner checks reject new or reassigned references, graph edges, events,
  memories, and entity evidence links that point at a merged tombstone.
- `EntityResolutionAudit` records match, dismiss, merge, and split operations and
  is append-only at the PostgreSQL layer.
- Entity lifecycle is domain-managed; generic entity patch requests cannot mark
  or revive merge tombstones.

## Reviewed document intake

- Original source bytes remain immutable and separate from normalized Markdown.
- Technical parsing and `deterministic-rules.v1` business classification are
  bounded, reproducible, and explainable; low confidence never implies promotion.
- Every successful run starts `pending`. A writer explicitly promotes or rejects it
  once; reviewer/time are required for terminal states and rejection requires a
  reason.
- Promotion creates canonical Document/version/chunks/links and Evidence in one
  transaction. The Evidence pointer binds ingestion run, exact original asset,
  resulting version, and content hash.
- Rejection preserves the original and audit but creates no canonical document.
- Every current tenant-owned and linked row remains organization/workspace scoped;
  database constraints reject cross-source assets, cross-scope reviewers, and
  mismatched canonical document/version links.

## Domain rules
- One real-world object equals one canonical Entity.
- Every important fact has Evidence.
- Every tenant-owned row must reference a workspace belonging to the same organization; linked records cannot cross workspace boundaries.
- Current Relationships are explicit tenant-scoped graph writes with confidence;
  Evidence provenance is attached through separate `EvidenceLink` rows.
  Relationship inference remains future product intent.
- Metrics are reproducible from stored inputs.
- A future memory-promotion workflow must be explicit and reviewable for sensitive
  facts; no promotion workflow is implemented in the current tree.
