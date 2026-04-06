"""Anything AI reverse proxy - Anthropic Messages API compatible.

Management platform with multi-account support.
"""

import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import settings
from database.schema import init_db
from database.migrate_sqlite_to_postgres import migrate_sqlite_to_postgres_if_needed
from database.connection import close_db, execute, fetchone
from services.account_pool import account_pool
from services.runtime_state import close_runtime_state


def setup_logging():
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "anything-proxy.log")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=50 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Keep uvicorn logs using the same handlers.
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "websockets"):
        logger_obj = logging.getLogger(logger_name)
        logger_obj.handlers.clear()
        logger_obj.propagate = True


setup_logging()
logger = logging.getLogger(__name__)


async def migrate_from_env():
    """Migrate single account from .env to database on first run."""
    count = await fetchone("SELECT COUNT(*) as cnt FROM accounts")
    if count and count["cnt"] > 0:
        return

    if not settings.access_token:
        return

    await execute(
        "INSERT INTO accounts (name, access_token, refresh_token, project_group_id, proxy_url, status, is_active) "
        "VALUES (?, ?, ?, ?, ?, 'active', 1)",
        ("默认账号", settings.access_token, settings.refresh_token,
         settings.project_group_id, settings.proxy_url),
    )
    logger.info("已从 .env 迁移默认账号到数据库")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    await migrate_sqlite_to_postgres_if_needed()
    await migrate_from_env()
    await account_pool.load()
    logger.info(f"Anything Proxy 已启动 - http://{settings.host}:{settings.port}")
    logger.info(f"管理后台: http://{settings.host}:{settings.port}/admin/")
    yield
    # Shutdown
    await close_runtime_state()
    await close_db()


app = FastAPI(title="Anything AI Proxy", version="2.0.0", lifespan=lifespan)

# CORS - allow all origins for API compatibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Include all routes
from routes import api_router
app.include_router(api_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
