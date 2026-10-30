from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "fileora"
    redis_url: str = "redis://localhost:6379/0"
    storage_dir: str = "./storage"
    max_upload_bytes: int = 25 * 1024 * 1024
    file_ttl_hours: int = 24
    session_cookie_secure: bool = False
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[2] / ".env", extra="ignore")

settings = Settings()
