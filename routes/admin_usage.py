"""Admin usage logs API - view usage statistics and logs."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends

from database.connection import get_db_backend
from database.connection import fetchall, fetchone, execute
from routes.admin_auth import require_admin
from services.pricing import estimate_usage_cost_usd, get_model_pricing, get_pricing_catalog

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])


def _cutoff(days: int):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    if get_db_backend() == "postgres":
        return cutoff
    return cutoff.isoformat()


@router.get("/usage/stats")
async def usage_stats(days: int = 7):
    """Get aggregated usage statistics."""
    cutoff = _cutoff(days)
    # Overall totals
    totals = await fetchone(
        "SELECT COUNT(*) as total_requests, "
        "COALESCE(SUM(input_tokens), 0) as total_input_tokens, "
        "COALESCE(SUM(output_tokens), 0) as total_output_tokens, "
        "COALESCE(SUM(cache_read_tokens), 0) as total_cache_read, "
        "COALESCE(SUM(cache_write_tokens), 0) as total_cache_write, "
        "COALESCE(SUM(total_tokens), 0) as total_tokens, "
        "COALESCE(AVG(duration_ms), 0) as avg_duration_ms, "
        "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count "
        "FROM usage_logs WHERE created_at >= ?",
        (cutoff,),
    )

    # Per-model breakdown
    by_model = await fetchall(
        "SELECT model, COUNT(*) as requests, "
        "COALESCE(SUM(input_tokens), 0) as input_tokens, "
        "COALESCE(SUM(output_tokens), 0) as output_tokens, "
        "COALESCE(SUM(cache_read_tokens), 0) as cache_read, "
        "COALESCE(SUM(cache_write_tokens), 0) as cache_write, "
        "COALESCE(SUM(total_tokens), 0) as total_tokens "
        "FROM usage_logs WHERE created_at >= ? "
        "GROUP BY model ORDER BY total_tokens DESC",
        (cutoff,),
    )

    # Per-status breakdown
    by_status = await fetchall(
        "SELECT status, COUNT(*) as requests, "
        "COALESCE(SUM(total_tokens), 0) as total_tokens "
        "FROM usage_logs WHERE created_at >= ? "
        "GROUP BY status ORDER BY requests DESC, total_tokens DESC",
        (cutoff,),
    )

    # Daily breakdown
    daily = await fetchall(
        "SELECT DATE(created_at) as date, COUNT(*) as requests, "
        "COALESCE(SUM(input_tokens), 0) as input_tokens, "
        "COALESCE(SUM(output_tokens), 0) as output_tokens, "
        "COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens, "
        "COALESCE(SUM(cache_write_tokens), 0) as cache_write_tokens, "
        "COALESCE(SUM(total_tokens), 0) as total_tokens "
        "FROM usage_logs WHERE created_at >= ? "
        "GROUP BY DATE(created_at) ORDER BY date DESC",
        (cutoff,),
    )

    by_key_rows = await fetchall(
        "SELECT u.api_key_id, COALESCE(k.id, 0) as key_id, COALESCE(k.name, '') as key_name, "
        "u.model, COUNT(*) as requests, "
        "COALESCE(SUM(u.input_tokens), 0) as input_tokens, "
        "COALESCE(SUM(u.output_tokens), 0) as output_tokens, "
        "COALESCE(SUM(u.cache_read_tokens), 0) as cache_read_tokens, "
        "COALESCE(SUM(u.cache_write_tokens), 0) as cache_write_tokens, "
        "COALESCE(SUM(u.total_tokens), 0) as total_tokens "
        "FROM usage_logs u "
        "LEFT JOIN api_keys k ON u.api_key_id = k.key "
        "WHERE u.created_at >= ? AND u.api_key_id <> '' "
        "GROUP BY u.api_key_id, k.id, k.name, u.model "
        "ORDER BY total_tokens DESC",
        (cutoff,),
    )

    total_cost_usd = 0.0
    enriched_models = []
    for row in by_model:
        item = dict(row)
        item["pricing"] = get_model_pricing(item["model"])
        item["cost_usd"] = estimate_usage_cost_usd(
            item["model"],
            input_tokens=item["input_tokens"],
            output_tokens=item["output_tokens"],
            cache_read_tokens=item["cache_read"],
            cache_write_tokens=item["cache_write"],
        )
        total_cost_usd += item["cost_usd"]
        enriched_models.append(item)

    enriched_daily = []
    for row in daily:
        item = dict(row)
        item["cost_usd"] = 0.0
        enriched_daily.append(item)

    key_summaries = {}
    for row in by_key_rows:
        key_id = row["api_key_id"]
        item = key_summaries.setdefault(
            key_id,
            {
                "api_key_id": key_id,
                "key_id": row["key_id"],
                "key_name": row["key_name"],
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        item["requests"] += row["requests"]
        item["input_tokens"] += row["input_tokens"]
        item["output_tokens"] += row["output_tokens"]
        item["cache_read_tokens"] += row["cache_read_tokens"]
        item["cache_write_tokens"] += row["cache_write_tokens"]
        item["total_tokens"] += row["total_tokens"]
        item["cost_usd"] += estimate_usage_cost_usd(
            row["model"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_write_tokens=row["cache_write_tokens"],
        )

    totals_dict = dict(totals) if totals else {}
    totals_dict["total_cost_usd"] = round(total_cost_usd, 6)

    return {
        "totals": totals_dict,
        "by_model": enriched_models,
        "by_status": [dict(r) for r in by_status],
        "daily": enriched_daily,
        "by_key": [dict(item, cost_usd=round(item["cost_usd"], 6)) for item in key_summaries.values()],
        "pricing_catalog": get_pricing_catalog(),
    }


@router.get("/usage/logs")
async def usage_logs(
    page: int = 1,
    page_size: int = 50,
    model: Optional[str] = None,
    status: Optional[str] = None,
    account_id: Optional[int] = None,
):
    """Get paginated usage logs."""
    conditions = []
    params = []

    if model:
        conditions.append("u.model = ?")
        params.append(model)
    if status:
        conditions.append("u.status = ?")
        params.append(status)
    if account_id:
        conditions.append("u.account_id = ?")
        params.append(account_id)

    where = " AND ".join(conditions) if conditions else "1=1"
    offset = (page - 1) * page_size

    # Get total count
    count = await fetchone(
        f"SELECT COUNT(*) as cnt FROM usage_logs u WHERE {where}",
        tuple(params),
    )

    # Get logs
    logs = await fetchall(
        f"SELECT u.* FROM usage_logs u "
        f"WHERE {where} ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (page_size, offset),
    )

    return {
        "logs": [dict(r) for r in logs],
        "total": count["cnt"] if count else 0,
        "page": page,
        "page_size": page_size,
        "total_pages": ((count["cnt"] if count else 0) + page_size - 1) // page_size,
    }


@router.delete("/usage/logs")
async def clear_usage_logs(days: Optional[int] = None):
    """Clear usage logs. If days specified, only clear logs older than N days."""
    if days:
        cutoff = _cutoff(days)
        await execute(
            "DELETE FROM usage_logs WHERE created_at < ?",
            (cutoff,),
        )
    else:
        await execute("DELETE FROM usage_logs")
    return {"success": True}
