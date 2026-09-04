"""Generic MCP transport facade.

The existing Odoo client already implements the bounded Streamable HTTP JSON-RPC
lifecycle. Phase 13 exposes it through a provider-neutral name while preserving
the Odoo import for compatibility.
"""

from company_brain.integrations.odoo.client import OdooMCPClient

MCPClient = OdooMCPClient

__all__ = ["MCPClient"]
