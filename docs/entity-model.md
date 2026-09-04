# Entity and Relationship Model

## Current Entity schema

```typescript
type EntityType =
  | 'organization' | 'person' | 'employee' | 'customer' | 'supplier'
  | 'product' | 'order' | 'invoice' | 'opportunity' | 'project'
  | 'ticket' | 'meeting' | 'email' | 'document' | 'decision'
  | 'event' | 'task';

interface Entity {
  id: string;
  organization_id: string;
  workspace_id: string;
  entity_type: EntityType;
  name: string;
  normalized_name: string;
  aliases: string[];
  metadata: Record<string, unknown>;
  lifecycle_status: string;
}
```

`lifecycle_status` defaults to `active`, but the current ORM/API/database expose
it as a string rather than a database-enforced closed enum. Merge policy uses a
managed tombstone status and prevents generic patch requests from performing or
reversing merge lifecycle transitions.

## Current Relationship schema

```typescript
interface Relationship {
  id: string;
  organization_id: string;
  workspace_id: string;
  from_entity_id: string;
  to_entity_id: string;
  relationship_type: string;
  confidence: number;
  valid_from?: string;
  valid_to?: string;
  metadata: Record<string, unknown>;
}
```

Evidence IDs are not embedded on a Relationship row or its CRUD request schema.
Separate tenant-safe `EvidenceLink` records attach Evidence to Relationships.

## Relationship vocabulary

Current APIs accept bounded relationship-type strings. The following values are
product vocabulary rather than a database-enforced enum:

- Customer edges: `CUSTOMER_HAS_CONTACT`, `CUSTOMER_HAS_ORDER`,
  `CUSTOMER_HAS_INVOICE`, `CUSTOMER_HAS_OPPORTUNITY`, `CUSTOMER_HAS_TICKET`,
  `CUSTOMER_ATTENDED_MEETING`, `CUSTOMER_HAS_PROJECT`,
  `CUSTOMER_RELATED_DOCUMENT`, `CUSTOMER_HAS_DECISION`, `CUSTOMER_HAS_EVENT`.
- Operational edges: `ORDER_HAS_INVOICE`, `OPPORTUNITY_OWNED_BY_EMPLOYEE`,
  `TICKET_ASSIGNED_TO_EMPLOYEE`, `PROJECT_FOR_CUSTOMER`,
  `MEETING_MENTIONS_ENTITY`, `DOCUMENT_MENTIONS_ENTITY`,
  `DECISION_IMPACTS_ENTITY`.
- Knowledge edges: `DOCUMENT_LINKS_TO_DOCUMENT`, `DOCUMENT_TAGGED_WITH`,
  `DOCUMENT_HAS_PROPERTY`, `MEMORY_DERIVED_FROM_SOURCE`.

## Entity resolution pipeline

The current resolver applies external-reference matches, exact identifiers,
normalized name/type matching, and bounded fuzzy candidate ranking. Fuzzy or
ambiguous matches enter a human review queue and never auto-merge. No embedding
backend participates in current entity resolution.

Merges preserve the source as a tombstone, transfer guarded references, and are
reversible through an audited merge journal while drift checks continue to pass.
