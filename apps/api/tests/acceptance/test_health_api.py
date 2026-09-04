from company_brain.main import app, health


def test_health_endpoint_reports_service_ready() -> None:
    assert health() == {"service": "company-second-brain-api", "status": "ok"}
    assert any(getattr(route, "path", None) == "/health" for route in app.routes)
