"""Admin accounts API - CRUD, refresh, check, batch import, API keys, balance."""

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from anything_client import AnythingClient
from database.connection import execute, fetchall, fetchone
from routes.admin_auth import require_admin
from services.account_pool import account_pool
from services.pricing import estimate_usage_cost_usd

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])


# ─── Request Models ────────────────────────────────────────────────────


class AccountCreate(BaseModel):
    name: str = ""
    email: str = ""
    access_token: str
    refresh_token: str = ""
    project_group_id: str
    proxy_url: Optional[str] = None
    note: str = ""


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    project_group_id: Optional[str] = None
    proxy_url: Optional[str] = None
    note: Optional[str] = None
    status: Optional[str] = None


class BatchImportRequest(BaseModel):
    accounts: list[dict]


class BatchDeleteRequest(BaseModel):
    ids: list[int]


# ─── Endpoints ─────────────────────────────────────────────────────────


@router.get("/accounts")
async def list_accounts(status: Optional[str] = None, keyword: Optional[str] = None):
    """List all accounts with optional filters."""
    sql = "SELECT * FROM accounts WHERE 1=1"
    params = []

    if status:
        sql += " AND status = ?"
        params.append(status)

    if keyword:
        sql += " AND (name LIKE ? OR email LIKE ? OR note LIKE ?)"
        params.extend([f"%{keyword}%"] * 3)

    sql += " ORDER BY created_at DESC"
    accounts = await fetchall(sql, tuple(params))

    # Mask tokens for security
    for acc in accounts:
        if acc.get("access_token"):
            t = acc["access_token"]
            acc["access_token"] = t[:20] + "..." + t[-10:] if len(t) > 40 else "***"
        if acc.get("refresh_token"):
            t = acc["refresh_token"]
            acc["refresh_token"] = t[:20] + "..." + t[-10:] if len(t) > 40 else "***"

    # Stats
    total = len(accounts)
    active = sum(1 for a in accounts if a["status"] == "active" and a["is_active"])
    error_count = sum(1 for a in accounts if a["status"] in ("error", "token_expired", "banned"))

    return {
        "accounts": accounts,
        "stats": {"total": total, "active": active, "error": error_count},
    }


@router.post("/accounts")
async def create_account(req: AccountCreate):
    """Add a new account."""
    now = datetime.now(timezone.utc).isoformat()
    aid = await execute(
        "INSERT INTO accounts (name, email, access_token, refresh_token, "
        "project_group_id, proxy_url, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (req.name, req.email, req.access_token, req.refresh_token,
         req.project_group_id, req.proxy_url, req.note, now, now),
    )
    account_pool.invalidate(aid)
    await account_pool.load()
    return {"success": True, "id": aid}


@router.get("/accounts/{account_id}")
async def get_account(account_id: int):
    """Get account details (full tokens for editing)."""
    acc = await fetchone("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    return acc


@router.put("/accounts/{account_id}")
async def update_account(account_id: int, req: AccountUpdate):
    """Update an account."""
    acc = await fetchone("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    updates = []
    params = []
    for field, value in req.model_dump(exclude_none=True).items():
        updates.append(f"{field} = ?")
        params.append(value)

    if not updates:
        return {"success": True}

    updates.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(account_id)

    await execute(
        f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?",
        tuple(params),
    )
    account_pool.invalidate(account_id)
    await account_pool.load()
    return {"success": True}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int):
    """Delete an account."""
    acc = await fetchone("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    await execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    account_pool.invalidate(account_id)
    return {"success": True}


@router.post("/accounts/batch-delete")
async def batch_delete_accounts(req: BatchDeleteRequest):
    """Delete multiple accounts at once."""
    ids = sorted({account_id for account_id in req.ids if account_id > 0})
    if not ids:
        raise HTTPException(status_code=400, detail="请选择要删除的账号")

    placeholders = ", ".join("?" for _ in ids)
    existing = await fetchall(
        f"SELECT id FROM accounts WHERE id IN ({placeholders})",
        tuple(ids),
    )
    existing_ids = sorted(row["id"] for row in existing)
    missing_ids = [account_id for account_id in ids if account_id not in set(existing_ids)]

    if not existing_ids:
        return {"success": True, "deleted": 0, "ids": [], "missing_ids": missing_ids}

    delete_placeholders = ", ".join("?" for _ in existing_ids)
    await execute(
        f"DELETE FROM accounts WHERE id IN ({delete_placeholders})",
        tuple(existing_ids),
    )
    for account_id in existing_ids:
        account_pool.invalidate(account_id)
    await account_pool.load()
    return {
        "success": True,
        "deleted": len(existing_ids),
        "ids": existing_ids,
        "missing_ids": missing_ids,
    }


@router.post("/accounts/{account_id}/refresh")
async def refresh_account(account_id: int):
    """Refresh token for a specific account."""
    acc = await fetchone("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    client = AnythingClient(
        access_token=acc["access_token"],
        refresh_token=acc["refresh_token"],
        project_group_id=acc["project_group_id"],
        proxy_url=acc.get("proxy_url"),
    )
    ok = await account_pool.try_refresh_token(account_id, client)
    return {"success": ok, "message": "Token刷新成功" if ok else "Token刷新失败"}


@router.post("/accounts/{account_id}/check")
async def check_account(account_id: int):
    """Check account status by calling get_me()."""
    acc = await fetchone("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    client = AnythingClient(
        access_token=acc["access_token"],
        refresh_token=acc["refresh_token"],
        project_group_id=acc["project_group_id"],
        proxy_url=acc.get("proxy_url"),
    )

    now = datetime.now(timezone.utc).isoformat()
    try:
        me = await client.get_me()
        await execute(
            "UPDATE accounts SET status = 'active', email = COALESCE(NULLIF(?, ''), email), "
            "last_error = NULL, updated_at = ? WHERE id = ?",
            (me.get("email", ""), now, account_id),
        )
        account_pool.invalidate(account_id)
        await account_pool.load()
        return {"success": True, "status": "active", "user": me}
    except Exception as e:
        error_msg = str(e)
        status = "token_expired" if "401" in error_msg else "error"
        await execute(
            "UPDATE accounts SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (status, error_msg, now, account_id),
        )
        account_pool.invalidate(account_id)
        return {"success": False, "status": status, "error": error_msg}


@router.post("/accounts/{account_id}/toggle")
async def toggle_account(account_id: int):
    """Enable/disable an account."""
    acc = await fetchone("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    new_state = 0 if acc["is_active"] else 1
    now = datetime.now(timezone.utc).isoformat()
    await execute(
        "UPDATE accounts SET is_active = ?, updated_at = ? WHERE id = ?",
        (new_state, now, account_id),
    )
    account_pool.invalidate(account_id)
    if new_state:
        await account_pool.load()
    return {"success": True, "is_active": bool(new_state)}


@router.post("/accounts/batch-import")
async def batch_import(req: BatchImportRequest):
    """Batch import accounts from JSON array."""
    imported = 0
    errors = []
    now = datetime.now(timezone.utc).isoformat()

    for i, acc_data in enumerate(req.accounts):
        try:
            access_token = acc_data.get("access_token", "").strip()
            project_group_id = acc_data.get("project_group_id", "").strip()
            if not access_token or not project_group_id:
                errors.append(f"第{i+1}条: 缺少 access_token 或 project_group_id")
                continue

            await execute(
                "INSERT INTO accounts (name, email, access_token, refresh_token, "
                "project_group_id, proxy_url, note, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    acc_data.get("name", f"导入账号 #{i+1}"),
                    acc_data.get("email", ""),
                    access_token,
                    acc_data.get("refresh_token", ""),
                    project_group_id,
                    acc_data.get("proxy_url"),
                    acc_data.get("note", ""),
                    now, now,
                ),
            )
            imported += 1
        except Exception as e:
            errors.append(f"第{i+1}条: {str(e)}")

    await account_pool.load()
    return {"imported": imported, "errors": errors}


@router.post("/accounts/refresh-all")
async def refresh_all():
    """Refresh tokens for all active accounts."""
    accounts = await fetchall("SELECT * FROM accounts WHERE is_active = 1")
    results = {"total": len(accounts), "success": 0, "failed": 0}

    for acc in accounts:
        client = AnythingClient(
            access_token=acc["access_token"],
            refresh_token=acc["refresh_token"],
            project_group_id=acc["project_group_id"],
            proxy_url=acc.get("proxy_url"),
        )
        ok = await account_pool.try_refresh_token(acc["id"], client)
        if ok:
            results["success"] += 1
        else:
            results["failed"] += 1

    await account_pool.load()
    return results


@router.post("/accounts/check-all")
async def check_all():
    """Check status of all accounts."""
    accounts = await fetchall("SELECT * FROM accounts")
    results = []
    now = datetime.now(timezone.utc).isoformat()

    for acc in accounts:
        client = AnythingClient(
            access_token=acc["access_token"],
            refresh_token=acc["refresh_token"],
            project_group_id=acc["project_group_id"],
            proxy_url=acc.get("proxy_url"),
        )
        try:
            me = await client.get_me()
            await execute(
                "UPDATE accounts SET status = 'active', email = COALESCE(NULLIF(?, ''), email), "
                "last_error = NULL, updated_at = ? WHERE id = ?",
                (me.get("email", ""), now, acc["id"]),
            )
            results.append({"id": acc["id"], "name": acc["name"], "status": "active"})
        except Exception as e:
            error_msg = str(e)
            status = "token_expired" if "401" in error_msg else "error"
            await execute(
                "UPDATE accounts SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, error_msg, now, acc["id"]),
            )
            results.append({"id": acc["id"], "name": acc["name"], "status": status, "error": error_msg})

    account_pool.invalidate(-1)  # Clear all cached clients
    await account_pool.load()
    return {"results": results}


@router.get("/stats/overview")
async def stats_overview():
    """Basic stats overview."""
    total = await fetchone("SELECT COUNT(*) as cnt FROM accounts")
    active = await fetchone(
        "SELECT COUNT(*) as cnt FROM accounts WHERE is_active = 1 AND status = 'active'"
    )
    error = await fetchone(
        "SELECT COUNT(*) as cnt FROM accounts WHERE status IN ('error', 'token_expired', 'banned')"
    )
    total_requests = await fetchone("SELECT COALESCE(SUM(total_requests), 0) as cnt FROM accounts")

    return {
        "total_accounts": total["cnt"],
        "active_accounts": active["cnt"],
        "error_accounts": error["cnt"],
        "total_requests": total_requests["cnt"],
    }


# ─── API Keys Management ──────────────────────────────────────────────


@router.post("/keys")
async def create_api_key(request: Request):
    """Generate a new API key."""
    body = await request.json() if await request.body() else {}
    name = body.get("name", "")
    key = f"sk-anything-{secrets.token_hex(24)}"
    now = datetime.now(timezone.utc).isoformat()
    await execute(
        "INSERT INTO api_keys (key, name, is_active, created_at) VALUES (?, ?, 1, ?)",
        (key, name, now),
    )
    return {"key": key, "name": name}


@router.get("/keys")
async def list_api_keys():
    """List all API keys."""
    keys = await fetchall(
        "SELECT id, key, name, is_active, created_at, last_used_at FROM api_keys ORDER BY created_at DESC"
    )
    usage_rows = await fetchall(
        "SELECT api_key_id, model, COUNT(*) as requests, "
        "COALESCE(SUM(input_tokens), 0) as input_tokens, "
        "COALESCE(SUM(output_tokens), 0) as output_tokens, "
        "COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens, "
        "COALESCE(SUM(cache_write_tokens), 0) as cache_write_tokens, "
        "COALESCE(SUM(total_tokens), 0) as total_tokens "
        "FROM usage_logs WHERE api_key_id <> '' "
        "GROUP BY api_key_id, model"
    )

    usage_by_key = {}
    for row in usage_rows:
        key = row["api_key_id"]
        summary = usage_by_key.setdefault(
            key,
            {
                "total_requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
            },
        )
        summary["total_requests"] += row["requests"]
        summary["input_tokens"] += row["input_tokens"]
        summary["output_tokens"] += row["output_tokens"]
        summary["cache_read_tokens"] += row["cache_read_tokens"]
        summary["cache_write_tokens"] += row["cache_write_tokens"]
        summary["total_tokens"] += row["total_tokens"]
        summary["total_cost_usd"] += estimate_usage_cost_usd(
            row["model"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_write_tokens=row["cache_write_tokens"],
        )

    key_items = []
    for key in keys:
        item = dict(key)
        usage = usage_by_key.get(item["key"], {})
        item.update({
            "total_requests": usage.get("total_requests", 0),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_tokens", 0),
            "cache_write_tokens": usage.get("cache_write_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "total_cost_usd": round(usage.get("total_cost_usd", 0.0), 6),
        })
        key_items.append(item)

    return {"keys": key_items}


@router.delete("/keys/{key_id}")
async def delete_api_key(key_id: int):
    """Delete an API key."""
    await execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    return {"success": True}


# ─── Account Balance / Quota ──────────────────────────────────────────


@router.post("/accounts/{account_id}/balance")
async def check_account_balance(account_id: int):
    """Query account credit balance from Anything API."""
    acc = await fetchone("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not acc:
        raise HTTPException(404, "账号不存在")

    client = AnythingClient(
        access_token=acc["access_token"],
        refresh_token=acc["refresh_token"],
        project_group_id=acc["project_group_id"],
        proxy_url=acc["proxy_url"],
    )

    now = datetime.now(timezone.utc).isoformat()
    try:
        billing = await client.get_billing_info()
        credit_balance = billing.get("creditBalance")
        plan = billing.get("plan") or ""
        org_id = billing.get("organization_id") or ""

        await execute(
            "UPDATE accounts SET credit_balance = ?, plan = ?, organization_id = ?, "
            "balance_checked_at = ?, updated_at = ? WHERE id = ?",
            (credit_balance, plan, org_id, now, now, account_id),
        )
        return {
            "success": True,
            "credit_balance": credit_balance,
            "plan": plan,
            "organization_id": org_id,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/accounts/check-balance-all")
async def check_all_balances():
    """Check credit balance for all active accounts."""
    accounts = await fetchall("SELECT * FROM accounts WHERE is_active = 1")
    results = []
    now = datetime.now(timezone.utc).isoformat()

    for acc in accounts:
        try:
            client = AnythingClient(
                access_token=acc["access_token"],
                refresh_token=acc["refresh_token"],
                project_group_id=acc["project_group_id"],
                proxy_url=acc["proxy_url"],
            )
            billing = await client.get_billing_info()
            credit_balance = billing.get("creditBalance")
            plan = billing.get("plan") or ""
            org_id = billing.get("organization_id") or ""

            await execute(
                "UPDATE accounts SET credit_balance = ?, plan = ?, organization_id = ?, "
                "balance_checked_at = ?, updated_at = ? WHERE id = ?",
                (credit_balance, plan, org_id, now, now, acc["id"]),
            )
            results.append({"id": acc["id"], "email": acc["email"],
                          "credit_balance": credit_balance, "plan": plan})
        except Exception as e:
            results.append({"id": acc["id"], "email": acc["email"],
                          "error": str(e)})

    return {"results": results}
