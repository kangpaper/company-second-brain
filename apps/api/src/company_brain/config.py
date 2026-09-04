import os
import re
import unicodedata

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://brain:brain@localhost:5432/company_brain"
    odoo_allowed_hosts: str = ""
    mcp_allowed_hosts: str = ""
    mcp_credential_keys: str = ""


def load_mcp_credential_registry() -> dict[str, SecretStr]:
    raw_keys = Settings().mcp_credential_keys
    if len(raw_keys) > 2079:
        raise RuntimeError("MCP credential registry configuration is invalid")
    keys = [key.strip() for key in raw_keys.split(",") if key.strip()]
    if (
        len(keys) > 32
        or len(set(keys)) != len(keys)
        or any(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", key) is None for key in keys)
    ):
        raise RuntimeError("MCP credential registry configuration is invalid")
    registry: dict[str, SecretStr] = {}
    for key in keys:
        value = os.environ.get(f"MCP_CREDENTIAL_{key.replace('-', '_').upper()}")
        if (
            value is not None
            and 8 <= len(value) <= 4096
            and not any(unicodedata.category(character).startswith("C") for character in value)
        ):
            registry[key] = SecretStr(value)
    return registry
