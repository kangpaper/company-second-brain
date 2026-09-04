import pytest
from alembic.config import Config

from company_brain.config import Settings, load_mcp_credential_registry
from company_brain.db.migrations import configure_alembic_url, escape_alembic_url


def test_database_url_can_be_overridden_by_environment(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://custom@db/custom")

    settings = Settings()

    assert settings.database_url == "postgresql+psycopg://custom@db/custom"


def test_legacy_json_mcp_credentials_cannot_leak_through_settings_validation(
    monkeypatch,
) -> None:
    canary = "registry-secret-canary"
    monkeypatch.setenv("MCP_CREDENTIALS", f'{{"prod":{{"value":"{canary}"}}}}')

    try:
        Settings()
    except Exception as error:
        assert canary not in repr(error)
        raise AssertionError("legacy MCP_CREDENTIALS must be ignored") from error


def test_mcp_credential_registry_loads_secret_from_per_key_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")

    registry = load_mcp_credential_registry()

    assert list(registry) == ["knowledge-prod"]
    assert registry["knowledge-prod"].get_secret_value() == "server-owned-secret"
    assert "server-owned-secret" not in repr(registry)


def test_mcp_credential_registry_rejects_unbounded_or_ambiguous_key_lists(
    monkeypatch,
) -> None:
    keys = [f"key-{index}" for index in range(33)]
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", ",".join(keys))
    monkeypatch.setenv("MCP_CREDENTIAL_KEY_0", "registry-secret-canary")

    with pytest.raises(RuntimeError) as captured:
        load_mcp_credential_registry()

    assert str(captured.value) == "MCP credential registry configuration is invalid"
    assert "registry-secret-canary" not in repr(captured.value)

    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "ambiguous_key")
    with pytest.raises(RuntimeError, match="^MCP credential registry configuration is invalid$"):
        load_mcp_credential_registry()


@pytest.mark.parametrize(
    "invalid_secret",
    ["", "short", "valid-prefix\nheader-injection", "x" * 4097],
)
def test_mcp_credential_registry_omits_invalid_secret_values(
    monkeypatch,
    invalid_secret: str,
) -> None:
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", invalid_secret)

    assert load_mcp_credential_registry() == {}


def test_alembic_uses_application_database_url(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://deploy@db/production")

    assert configure_alembic_url() == "postgresql+psycopg://deploy@db/production"


def test_alembic_escapes_percent_encoded_credentials_for_config_parser() -> None:
    url = "postgresql+psycopg://user:p%25ss@localhost:5432/company_brain"

    assert escape_alembic_url(url) == (
        "postgresql+psycopg://user:p%%25ss@localhost:5432/company_brain"
    )
    config = Config()
    config.set_main_option("sqlalchemy.url", escape_alembic_url(url))

    assert config.get_main_option("sqlalchemy.url") == url
