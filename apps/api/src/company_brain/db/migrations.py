from company_brain.config import Settings


def configure_alembic_url() -> str:
    return Settings().database_url


def escape_alembic_url(database_url: str) -> str:
    return database_url.replace("%", "%%")
