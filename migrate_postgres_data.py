import argparse
import asyncio

import asyncpg

from database.schema import POSTGRES_SCHEMA_SQL

TABLES = [
    "accounts",
    "outlook_accounts",
    "api_keys",
    "system_settings",
    "usage_logs",
]


def parse_args():
    parser = argparse.ArgumentParser(description="Migrate Anything Proxy data from one PostgreSQL database/schema to another.")
    parser.add_argument("--source-url", required=True, help="Source PostgreSQL DSN")
    parser.add_argument("--target-url", required=True, help="Target PostgreSQL DSN")
    parser.add_argument("--source-schema", default="public", help="Source schema name")
    parser.add_argument("--target-schema", default="anything_proxy", help="Target schema name")
    parser.add_argument("--truncate-target", action="store_true", help="Truncate target tables before importing")
    return parser.parse_args()


def qname(schema: str, table: str) -> str:
    return f'"{schema}"."{table}"'


async def ensure_target_schema(conn, schema: str):
    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')


async def ensure_target_tables(conn, schema: str):
    await conn.execute(f'SET search_path TO "{schema}"')
    statements = [stmt.strip() for stmt in POSTGRES_SCHEMA_SQL.split(";") if stmt.strip()]
    for stmt in statements:
        await conn.execute(stmt)


async def table_exists(conn, schema: str, table: str) -> bool:
    sql = """
    SELECT EXISTS (
      SELECT 1
      FROM information_schema.tables
      WHERE table_schema = $1 AND table_name = $2
    )
    """
    return bool(await conn.fetchval(sql, schema, table))


async def table_count(conn, schema: str, table: str) -> int:
    if not await table_exists(conn, schema, table):
        return 0
    return int(await conn.fetchval(f"SELECT COUNT(*) FROM {qname(schema, table)}"))


async def truncate_target(conn, schema: str):
    for table in reversed(TABLES):
        if await table_exists(conn, schema, table):
            await conn.execute(f"TRUNCATE TABLE {qname(schema, table)} RESTART IDENTITY CASCADE")


async def copy_table(source_conn, target_conn, source_schema: str, target_schema: str, table: str):
    if not await table_exists(source_conn, source_schema, table):
        print(f"[SKIP] source table missing: {source_schema}.{table}")
        return 0
    if not await table_exists(target_conn, target_schema, table):
        raise RuntimeError(f"target table missing: {target_schema}.{table}")

    rows = await source_conn.fetch(f"SELECT * FROM {qname(source_schema, table)} ORDER BY 1")
    if not rows:
        print(f"[OK] {table}: 0 rows")
        return 0

    columns = list(rows[0].keys())
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    query = f"INSERT INTO {qname(target_schema, table)} ({', '.join(columns)}) VALUES ({placeholders})"
    values = [tuple(row[col] for col in columns) for row in rows]
    await target_conn.executemany(query, values)
    print(f"[OK] {table}: {len(values)} rows")
    return len(values)


async def sync_sequence(conn, schema: str, table: str):
    if not await table_exists(conn, schema, table):
        return
    await conn.execute(
        f"SELECT setval(pg_get_serial_sequence('{schema}.{table}', 'id'), "
        f"COALESCE((SELECT MAX(id) FROM {qname(schema, table)}), 1), true)"
    )


async def main():
    args = parse_args()
    source = await asyncpg.connect(dsn=args.source_url, command_timeout=60)
    target = await asyncpg.connect(dsn=args.target_url, command_timeout=60)
    try:
        await ensure_target_schema(target, args.target_schema)
        await ensure_target_tables(target, args.target_schema)

        target_has_data = False
        for table in TABLES:
            if await table_count(target, args.target_schema, table) > 0:
                target_has_data = True
                break
        if target_has_data and not args.truncate_target:
            raise RuntimeError(
                f"Target schema {args.target_schema} already contains data. "
                f"Use --truncate-target if you want to overwrite it."
            )
        if args.truncate_target:
            await truncate_target(target, args.target_schema)

        async with target.transaction():
            total_rows = 0
            for table in TABLES:
                total_rows += await copy_table(source, target, args.source_schema, args.target_schema, table)
            for table in ("accounts", "outlook_accounts", "api_keys", "usage_logs"):
                await sync_sequence(target, args.target_schema, table)

        print(f"[DONE] migrated {total_rows} rows into schema {args.target_schema}")
    finally:
        await source.close()
        await target.close()


if __name__ == "__main__":
    asyncio.run(main())
