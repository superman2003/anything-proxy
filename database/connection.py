"""Database connection management with SQLite/PostgreSQL compatibility."""

import asyncio
from datetime import datetime
import logging
import os
import re

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)

_sqlite_db: aiosqlite.Connection | None = None
_pg_pool = None
_write_lock = asyncio.Lock()
_PG_TABLES = ("accounts", "outlook_accounts", "api_keys", "system_settings", "usage_logs")


def get_db_backend() -> str:
    database_url = (settings.database_url or "").lower()
    if database_url.startswith("postgresql://") or database_url.startswith("postgres://"):
        return "postgres"
    return "sqlite"


def _convert_placeholders(sql: str) -> str:
    parts = []
    index = 1
    for ch in sql:
        if ch == "?":
            parts.append(f"${index}")
            index += 1
        else:
            parts.append(ch)
    return "".join(parts)


def _qualify_postgres_sql(sql: str) -> str:
    schema = settings.db_schema
    qualified = sql
    for table in _PG_TABLES:
        qualified_name = f'"{schema}".{table}'
        patterns = [
            rf"(?i)\bFROM\s+{table}\b",
            rf"(?i)\bJOIN\s+{table}\b",
            rf"(?i)\bINTO\s+{table}\b",
            rf"(?i)\bUPDATE\s+{table}\b",
            rf"(?i)\bDELETE\s+FROM\s+{table}\b",
            rf"(?i)\bALTER\s+TABLE\s+{table}\b",
            rf"(?i)\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{table}\b",
            rf"(?i)\bREFERENCES\s+{table}\b",
            rf"(?i)\bON\s+{table}\b",
        ]
        replacements = [
            f"FROM {qualified_name}",
            f"JOIN {qualified_name}",
            f"INTO {qualified_name}",
            f"UPDATE {qualified_name}",
            f"DELETE FROM {qualified_name}",
            f"ALTER TABLE {qualified_name}",
            f"CREATE TABLE IF NOT EXISTS {qualified_name}",
            f"REFERENCES {qualified_name}",
            f"ON {qualified_name}",
        ]
        for pattern, replacement in zip(patterns, replacements):
            qualified = re.sub(pattern, replacement, qualified)
    return qualified


def _coerce_param(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def _coerce_params(params: tuple) -> tuple:
    return tuple(_coerce_param(value) for value in params)


async def get_db():
    backend = get_db_backend()
    if backend == "sqlite":
        global _sqlite_db
        if _sqlite_db is None:
            os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
            _sqlite_db = await aiosqlite.connect(settings.db_path)
            _sqlite_db.row_factory = aiosqlite.Row
            await _sqlite_db.execute("PRAGMA journal_mode=WAL")
            await _sqlite_db.execute("PRAGMA foreign_keys=ON")
        return _sqlite_db

    global _pg_pool
    if _pg_pool is None:
        import asyncpg

        admin_conn = await asyncpg.connect(
            dsn=settings.database_url,
            command_timeout=60,
        )
        try:
            await admin_conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{settings.db_schema}"')
        finally:
            await admin_conn.close()

        async def _init_pg_conn(conn):
            await conn.execute(f'SET search_path TO "{settings.db_schema}"')

        _pg_pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            command_timeout=60,
            init=_init_pg_conn,
        )
    return _pg_pool


async def execute(sql: str, params: tuple = ()) -> int:
    """Execute a write query with lock. Returns inserted id for INSERT statements when possible."""
    backend = get_db_backend()
    if backend == "sqlite":
        db = await get_db()
        async with _write_lock:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.lastrowid

    pool = await get_db()
    query = _convert_placeholders(_qualify_postgres_sql(sql))
    params = _coerce_params(params)
    async with _write_lock:
        async with pool.acquire() as conn:
            if query.lstrip().upper().startswith("INSERT") and "RETURNING" not in query.upper():
                row = await conn.fetchrow(query + " RETURNING id", *params)
                return row["id"] if row and "id" in row else 0
            await conn.execute(query, *params)
            return 0


async def fetchone(sql: str, params: tuple = ()) -> dict | None:
    """Fetch a single row as dict."""
    backend = get_db_backend()
    if backend == "sqlite":
        db = await get_db()
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    pool = await get_db()
    query = _convert_placeholders(_qualify_postgres_sql(sql))
    params = _coerce_params(params)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, *params)
        return dict(row) if row is not None else None


async def fetchall(sql: str, params: tuple = ()) -> list[dict]:
    """Fetch all rows as list of dict."""
    backend = get_db_backend()
    if backend == "sqlite":
        db = await get_db()
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    pool = await get_db()
    query = _convert_placeholders(_qualify_postgres_sql(sql))
    params = _coerce_params(params)
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]


async def execute_script(sql_script: str):
    """Execute a multi-statement SQL script."""
    backend = get_db_backend()
    if backend == "sqlite":
        db = await get_db()
        await db.executescript(sql_script)
        await db.commit()
        return

    pool = await get_db()
    statements = [stmt.strip() for stmt in sql_script.split(";") if stmt.strip()]
    async with _write_lock:
        async with pool.acquire() as conn:
            for stmt in statements:
                await conn.execute(_qualify_postgres_sql(stmt))


async def close_db():
    """Close the active database connection/pool."""
    global _sqlite_db, _pg_pool
    if _sqlite_db is not None:
        await _sqlite_db.close()
        _sqlite_db = None
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None
