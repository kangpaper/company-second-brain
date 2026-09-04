# Company Second Brain — Product Definition

**Delivery status:** Phases 0–16 are implemented and verified for the current
pre-publication product slice. Generic MCP resources enter the immutable-asset and
operator Review Queue pipeline; persistent discovery, checkpoints, bounded sync,
schedules, and recovery are implemented. See [`roadmap.md`](roadmap.md) for the
authoritative phase gates, evidence, and explicitly deferred product scope.

## Thesis
Company Second Brain is a business-context and organizational-memory system, not a generic RAG chatbot. RAG is only one retrieval mechanism inside a larger architecture that separates Knowledge, Business Data, Entity, Relationship, Event, Memory, Context, AI Reasoning, Action, and External Integration.

## MVP outcome
The first end-to-end use case is Customer 360 for “Tình hình khách hàng ABC hiện tại thế nào?” The answer must include deterministic metrics, risk signals, timeline, memories, documents, and evidence/source links to both documents and business records.

## Non-goals
- Do not fork/rebrand proprietary Obsidian core.
- Do not build a document-only chatbot.
- Do not hard-code Odoo models into core domain.
- Do not let LLMs calculate deterministic business metrics.
- Do not enable write actions before read-only flows, approvals, and audit pass.

## Obsidian-inspired but independent
Use open formats and concepts: Markdown vault-like knowledge workspace, metadata cache concepts, command palette, internal links/backlinks, properties/tags, JSON Canvas compatibility, importer/clipper patterns, and plugin-like extension points. The app remains an independent web product.

## Research Sources

- [S1] Obsidian API: MIT type definitions; concepts: App, Vault, Workspace, MetadataCache, commands/views/settings — https://github.com/obsidianmd/obsidian-api
- [S2] JSON Canvas: MIT open `.canvas` format; top-level `nodes`/`edges`; node types `text`, `file`, `link`, `group` — https://github.com/obsidianmd/jsoncanvas
- [S3] Obsidian Importer: MIT, converts many exports/file formats to durable Markdown; fixture-based tests — https://github.com/obsidianmd/obsidian-importer
- [S4] Obsidian Web Clipper: MIT source; captures/highlights web to durable Markdown; trademarks/marketing assets excluded — https://github.com/obsidianmd/obsidian-clipper
- [S5] Obsidian Maps: MIT, property-driven map view for notes/coordinates — https://github.com/obsidianmd/obsidian-maps
- [S6] Obsidian Sample Plugin: 0BSD TypeScript plugin template/build conventions — https://github.com/obsidianmd/obsidian-sample-plugin
- [S7] Odoo MCP Server app: third-party Odoo 19 module, OPL-1, technical name `mcp_server`, exposes `/mcp`, OAuth 2.1/API key, read-only consent, per-model permissions, audit log — https://apps.odoo.com/apps/modules/19.0/mcp_server
- [S8] `ivnvxd/mcp-server-odoo`: MPL-2.0 local stdio bridge/YOLO mode for Odoo access — https://github.com/ivnvxd/mcp-server-odoo
