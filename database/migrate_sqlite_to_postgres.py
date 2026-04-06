"""Automatic migration from local SQLite database to PostgreSQL."""

from datetime import datetime
import logging
import os
import sqlite3

from config import settings
from database.connection import get_db, get_db_backend

logger = logging.getLogger(__name__)

TABLES = [
    "accounts",
    "outlook_accounts",
    "api_keys",
    "system_settings",
    "usage_logs",
]

TIMESTAMP_COLUMNS = {
    "accounts": {"last_used_at", "last_refresh_at", "balance_checked_at", "created_at", "updated_at"},
    "outlook_accounts": {"created_at", "updated_at"},
    "api_keys": {"created_at", "last_used_at"},
    "system_settings": {"updated_at"},
    "usage_logs": {"created_at"},
}


def _convert_value(table: str, column: str, value):
    if value is None:
        return None
    if column in TIMESTAMP_COLUMNS.get(table, set()) and isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


async def migrate_sqlite_to_postgres_if_needed():
    if get_db_backend() != "postgres":
        return False
    if not settings.auto_migrate_sqlite_to_postgres:
        return False
    if not settings.db_path or not os.path.exists(settings.db_path):
        return False

    sqlite_conn = sqlite3.connect(settings.db_path)
    sqlite_conn.row_factory = sqlite3.Row
    try:
        sqlite_cur = sqlite_conn.cursor()
        has_sqlite_data = any(
            sqlite_cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
            for table in TABLES
        )
        if not has_sqlite_data:
            return False

        pool = await get_db()
        async with pool.acquire() as conn:
            has_pg_data = False
            for table in TABLES:
                if (await conn.fetchval(f"SELECT COUNT(*) FROM {table}")) > 0:
                    has_pg_data = True
                    break
            if has_pg_data:
                logger.info("PostgreSQL already contains data, skipping SQLite auto-migration")
                return False

            async with conn.transaction():
                for table in TABLES:
                    rows = sqlite_cur.execute(f"SELECT * FROM {table}").fetchall()
                    if not rows:
                        continue
                    columns = list(rows[0].keys())
                    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
                    query = (
                        f"INSERT INTO {table} ({', '.join(columns)}) "
                        f"VALUES ({placeholders})"
                    )
                    values = [
                        tuple(_convert_value(table, col, row[col]) for col in columns)
                        for row in rows
                    ]
                    await conn.executemany(query, values)

                for table in ("accounts", "outlook_accounts", "api_keys", "usage_logs"):
                    await conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{settings.db_schema}.{table}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM \"{settings.db_schema}\".{table}), 1), true)"
                    )

        logger.info("SQLite -> PostgreSQL auto-migration completed")
        return True
    finally:
        sqlite_conn.close()
