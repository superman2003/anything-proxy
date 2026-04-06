from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment or .env file."""

    # Anything AI credentials (used for migration on first run)
    access_token: str = ""
    refresh_token: str = ""
    project_group_id: str = ""

    # Server settings
    host: str = "0.0.0.0"
    port: int = 3000

    # API key for authenticating incoming requests (optional)
    api_key: Optional[str] = None

    # Anything API settings
    anything_base_url: str = "https://www.anything.com/api/graphql"
    poll_interval: float = 2.0  # seconds
    poll_timeout: float = 120.0  # seconds

    # Proxy (optional)
    proxy_url: Optional[str] = None

    # Admin dashboard
    admin_password: str = "admin"

    # Database
    db_path: str = "data/anything_proxy.db"
    database_url: Optional[str] = None
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    auto_migrate_sqlite_to_postgres: bool = True
    db_schema: str = "anything_proxy"

    # Redis (optional, recommended for multi-process / server deployments)
    redis_url: Optional[str] = None
    redis_prefix: str = "anything_proxy"

    # Microsoft Graph API (Outlook email reading)
    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_tenant_id: str = "common"
    ms_redirect_uri: str = ""  # e.g. http://localhost:8080/admin/oauth/callback

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
