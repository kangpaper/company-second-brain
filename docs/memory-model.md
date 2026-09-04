# Memory Model

## Types

The persisted `memory_type` enum supports:

1. `semantic` — what the business knows;
2. `episodic` — what happened;
3. `decision` — what was decided and why;
4. `conversation` — what was discussed or committed;
5. `business` — customer, order, or project history.

## Current persisted schema

```typescript
interface Memory {
  id: string;
  organization_id: string;
  workspace_id: string;
  subject_entity_id?: string;
  memory_type: 'semantic' | 'episodic' | 'decision' | 'conversation' | 'business';
  text: string;
  structured_facts: Record<string, unknown>;
  confidence: number;
  review_status: string;
  created_at: string;
  updated_at: string;
}
```

`subject_entity_id` is optional and is protected by a tenant/workspace-composite
foreign key when present. Evidence provenance is not embedded as ID arrays on the
Memory row; separate `EvidenceLink` records attach Evidence to a Memory.

## Current and deferred behavior

The current tree persists tenant-scoped Memory records and can include them in
permission-filtered context/read projections. It does not implement a dedicated
memory-promotion workflow, semantic-similarity memory retrieval, sensitivity or
validity fields, or automatic memory decay.

Promotion based on repeated sources, explicit operator marking, impact,
decision/action relevance, and review policy remains product intent for a future
memory workflow. Semantic ranking and decay must not be treated as current
behavior until their storage, policy, API, and regression gates are implemented.
