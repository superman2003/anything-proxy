"""Database schema and initialization for SQLite/PostgreSQL."""

import logging

from .connection import execute_script, get_db_backend

logger = logging.getLogger(__name__)

SQLITE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL DEFAULT '',
    email            TEXT NOT NULL DEFAULT '',
    access_token     TEXT NOT NULL,
    refresh_token    TEXT NOT NULL DEFAULT '',
    project_group_id TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    is_active        INTEGER NOT NULL DEFAULT 1,
    total_requests   INTEGER NOT NULL DEFAULT 0,
    last_used_at     TEXT,
    last_refresh_at  TEXT,
    last_error       TEXT,
    note             TEXT NOT NULL DEFAULT '',
    proxy_url        TEXT,
    credit_balance   REAL,
    plan             TEXT NOT NULL DEFAULT '',
    organization_id  TEXT NOT NULL DEFAULT '',
    balance_checked_at TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_accounts_is_active ON accounts(is_active);

CREATE TABLE IF NOT EXISTS outlook_accounts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    email            TEXT NOT NULL,
    password         TEXT NOT NULL DEFAULT '',
    client_id        TEXT NOT NULL DEFAULT '',
    ms_refresh_token TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    linked_account_id INTEGER,
    last_error       TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (linked_account_id) REFERENCES accounts(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL DEFAULT '',
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS system_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS usage_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id       TEXT NOT NULL,
    account_id       INTEGER,
    api_key_id       TEXT NOT NULL DEFAULT '',
    model            TEXT NOT NULL,
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    is_stream        INTEGER NOT NULL DEFAULT 0,
    has_thinking     INTEGER NOT NULL DEFAULT 0,
    has_tool_use     INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'success',
    error_message    TEXT,
    duration_ms      INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_usage_logs_created_at ON usage_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_logs_account_id ON usage_logs(account_id);
CREATE INDEX IF NOT EXISTS idx_usage_logs_model ON usage_logs(model);
"""

POSTGRES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id               BIGSERIAL PRIMARY KEY,
    name             TEXT NOT NULL DEFAULT '',
    email            TEXT NOT NULL DEFAULT '',
    access_token     TEXT NOT NULL,
    refresh_token    TEXT NOT NULL DEFAULT '',
    project_group_id TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    is_active        INTEGER NOT NULL DEFAULT 1,
    total_requests   INTEGER NOT NULL DEFAULT 0,
    last_used_at     TIMESTAMPTZ,
    last_refresh_at  TIMESTAMPTZ,
    last_error       TEXT,
    note             TEXT NOT NULL DEFAULT '',
    proxy_url        TEXT,
    credit_balance   DOUBLE PRECISION,
    plan             TEXT NOT NULL DEFAULT '',
    organization_id  TEXT NOT NULL DEFAULT '',
    balance_checked_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS email TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS access_token TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS refresh_token TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS project_group_id TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS is_active INTEGER NOT NULL DEFAULT 1;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS total_requests INTEGER NOT NULL DEFAULT 0;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_refresh_at TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS proxy_url TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS credit_balance DOUBLE PRECISION;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS balance_checked_at TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

CREATE TABLE IF NOT EXISTS outlook_accounts (
    id               BIGSERIAL PRIMARY KEY,
    email            TEXT NOT NULL,
    password         TEXT NOT NULL DEFAULT '',
    client_id        TEXT NOT NULL DEFAULT '',
    ms_refresh_token TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    linked_account_id BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE outlook_accounts ADD COLUMN IF NOT EXISTS email TEXT NOT NULL DEFAULT '';
ALTER TABLE outlook_accounts ADD COLUMN IF NOT EXISTS password TEXT NOT NULL DEFAULT '';
ALTER TABLE outlook_accounts ADD COLUMN IF NOT EXISTS client_id TEXT NOT NULL DEFAULT '';
ALTER TABLE outlook_accounts ADD COLUMN IF NOT EXISTS ms_refresh_token TEXT NOT NULL DEFAULT '';
ALTER TABLE outlook_accounts ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE outlook_accounts ADD COLUMN IF NOT EXISTS linked_account_id BIGINT;
ALTER TABLE outlook_accounts ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE outlook_accounts ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE outlook_accounts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

CREATE TABLE IF NOT EXISTS api_keys (
    id         BIGSERIAL PRIMARY KEY,
    key        TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL DEFAULT '',
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMPTZ
);

ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key TEXT NOT NULL DEFAULT '';
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT '';
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS is_active INTEGER NOT NULL DEFAULT 1;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS system_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS value TEXT NOT NULL DEFAULT '';
ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

CREATE TABLE IF NOT EXISTS usage_logs (
    id               BIGSERIAL PRIMARY KEY,
    request_id       TEXT NOT NULL,
    account_id       BIGINT,
    api_key_id       TEXT NOT NULL DEFAULT '',
    model            TEXT NOT NULL,
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    is_stream        INTEGER NOT NULL DEFAULT 0,
    has_thinking     INTEGER NOT NULL DEFAULT 0,
    has_tool_use     INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'success',
    error_message    TEXT,
    duration_ms      INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS request_id TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS account_id BIGINT;
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS api_key_id TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS output_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS total_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS is_stream INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS has_thinking INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS has_tool_use INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'success';
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS duration_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_logs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_accounts_is_active ON accounts(is_active);
CREATE INDEX IF NOT EXISTS idx_usage_logs_created_at ON usage_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_logs_account_id ON usage_logs(account_id);
CREATE INDEX IF NOT EXISTS idx_usage_logs_model ON usage_logs(model);
"""

SQLITE_MIGRATION_SQL = """
ALTER TABLE accounts ADD COLUMN credit_balance REAL;
ALTER TABLE accounts ADD COLUMN plan TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN organization_id TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN balance_checked_at TEXT;
"""


async def init_db():
    """Create tables if they don't exist, run migrations for existing DBs."""
    backend = get_db_backend()
    if backend == "postgres":
        await execute_script(POSTGRES_SCHEMA_SQL)
        logger.info("PostgreSQL schema initialized")
        return

    await execute_script(SQLITE_SCHEMA_SQL)
    for stmt in SQLITE_MIGRATION_SQL.strip().splitlines():
        stmt = stmt.strip()
        if stmt:
            try:
                await execute_script(stmt)
            except Exception:
                pass
    logger.info("SQLite schema initialized")
