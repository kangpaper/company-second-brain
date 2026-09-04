from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from company_brain.api.action_proposals import router as action_proposals_router
from company_brain.api.ai import router as ai_router
from company_brain.api.canvases import router as canvases_router
from company_brain.api.context import router as context_router
from company_brain.api.customer_360 import router as customer_360_router
from company_brain.api.documents import router as documents_router
from company_brain.api.entities import router as entities_router
from company_brain.api.entity_resolution import router as entity_resolution_router
from company_brain.api.evidence import router as evidence_router
from company_brain.api.generic_mcp_integrations import router as generic_mcp_integrations_router
from company_brain.api.graph import router as graph_router
from company_brain.api.ingestions import router as ingestions_router
from company_brain.api.integration_audits import router as integration_audits_router
from company_brain.api.odoo_integrations import router as odoo_integrations_router
from company_brain.api.relationships import router as relationships_router
from company_brain.api.risk import router as risk_router
from company_brain.api.search import router as search_router
from company_brain.api.timeline import router as timeline_router

app = FastAPI(title="Company Second Brain API", version="0.1.0")


@app.exception_handler(RequestValidationError)
async def sanitized_request_validation_error(
    _request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    detail = [
        {
            key: item[key]
            for key in ("type", "loc")
            if key in item
        }
        for item in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


app.include_router(action_proposals_router)
app.include_router(ai_router)
app.include_router(canvases_router)
app.include_router(context_router)
app.include_router(customer_360_router)
app.include_router(documents_router)
app.include_router(entities_router)
app.include_router(entity_resolution_router)
app.include_router(relationships_router)
app.include_router(risk_router)
app.include_router(evidence_router)
app.include_router(graph_router)
app.include_router(generic_mcp_integrations_router)
app.include_router(ingestions_router)
app.include_router(integration_audits_router)
app.include_router(odoo_integrations_router)
app.include_router(search_router)
app.include_router(timeline_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"service": "company-second-brain-api", "status": "ok"}
