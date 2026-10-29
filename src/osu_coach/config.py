from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings, read from environment variables or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+psycopg://osu:osu@localhost:5432/osu_coach"
    redis_url: str = "redis://localhost:6379/0"
    # Downloaded .osu files and replay frames; not in git. Relative paths start from the
    # working directory: the repo locally, /app in Docker (the appdata volume).
    data_dir: Path = Path("data")

    # osu! API v2 (client credentials)
    osu_client_id: str = ""
    osu_client_secret: SecretStr = SecretStr("")


@lru_cache
def get_settings() -> Settings:
    return Settings()
