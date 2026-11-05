from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "fileora"
    redis_url: str = "redis://localhost:6379/0"
    storage_dir: str = "./storage"
    storage_backend: str = "local"
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = ""
    r2_endpoint_url: str = ""
    r2_prefix: str = ""
    max_upload_bytes: int = 25 * 1024 * 1024
    file_ttl_hours: int = 24
    session_cookie_secure: bool = False
    admin_email: str = ""
    admin_password: str = ""
    jwt_secret_key: str = ""
    otp_hash_secret: str = ""
    email_provider: str = "resend"
    email_from: str = ""
    resend_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    otp_expiry_seconds: int = 300
    otp_resend_cooldown_seconds: int = 60
    otp_max_attempts: int = 5
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[2] / ".env", extra="ignore")

settings = Settings()
