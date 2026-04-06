"""Admin routes for Outlook account import and auto-login."""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database.connection import execute, fetchall, fetchone
from routes.admin_auth import require_admin
from services.account_pool import account_pool
from services.anything_login import login_and_add_account
from services.outlook_client import parse_outlook_line

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])


class OutlookImportRequest(BaseModel):
    """Import Outlook accounts from text lines.
    Format per line: email----password----client_id----ms_refresh_token
    """
    text: str


class OutlookUpdateRequest(BaseModel):
    """Update an Outlook account's credentials."""
    email: str | None = None
    password: str | None = None
    client_id: str | None = None
    ms_refresh_token: str | None = None


class AutoLoginRequest(BaseModel):
    outlook_id: int


# ─── Outlook account CRUD ──────────────────────────────────────────────


async def _repair_broken_outlook_links() -> int:
    """Reset stale 'linked' rows whose linked account no longer exists."""
    broken = await fetchall(
        "SELECT o.id "
        "FROM outlook_accounts o "
        "LEFT JOIN accounts a ON o.linked_account_id = a.id "
        "WHERE o.status = 'linked' AND (o.linked_account_id IS NULL OR a.id IS NULL)"
    )
    if not broken:
        return 0

    ids = [row["id"] for row in broken]
    placeholders = ", ".join("?" for _ in ids)
    now = datetime.now(timezone.utc).isoformat()
    await execute(
        f"UPDATE outlook_accounts SET status = 'pending', linked_account_id = NULL, "
        f"updated_at = ? WHERE id IN ({placeholders})",
        (now, *ids),
    )
    logger.warning(f"已自动修正 {len(ids)} 条 Outlook 脏关联状态: {ids}")
    return len(ids)


@router.get("/outlook-accounts")
async def list_outlook_accounts():
    await _repair_broken_outlook_links()
    accounts = await fetchall(
        "SELECT id, email, status, linked_account_id, last_error, created_at "
        "FROM outlook_accounts ORDER BY created_at DESC"
    )
    total = len(accounts)
    pending = sum(1 for a in accounts if a["status"] == "pending")
    linked = sum(1 for a in accounts if a["status"] == "linked")
    error = sum(1 for a in accounts if a["status"] == "error")
    return {
        "accounts": accounts,
        "stats": {"total": total, "pending": pending, "linked": linked, "error": error},
    }


@router.post("/outlook-accounts/import")
async def import_outlook_accounts(req: OutlookImportRequest):
    """Batch import Outlook accounts from text.
    One account per line: email----password----client_id----ms_refresh_token
    """
    lines = [l.strip() for l in req.text.strip().splitlines() if l.strip()]
    imported = 0
    errors = []
    now = datetime.now(timezone.utc).isoformat()

    for i, line in enumerate(lines):
        try:
            cred = parse_outlook_line(line)

            # Check if email already exists
            existing = await fetchone(
                "SELECT id FROM outlook_accounts WHERE email = ?", (cred["email"],)
            )
            if existing:
                errors.append(f"第{i+1}行: {cred['email']} 已存在")
                continue

            await execute(
                "INSERT INTO outlook_accounts (email, password, client_id, ms_refresh_token, "
                "status, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (cred["email"], cred["password"], cred["client_id"],
                 cred["ms_refresh_token"], now, now),
            )
            imported += 1
        except Exception as e:
            errors.append(f"第{i+1}行: {str(e)}")

    return {"imported": imported, "errors": errors}


@router.delete("/outlook-accounts/{outlook_id}")
async def delete_outlook_account(outlook_id: int):
    acc = await fetchone("SELECT id FROM outlook_accounts WHERE id = ?", (outlook_id,))
    if not acc:
        raise HTTPException(404, "Outlook 账号不存在")
    await execute("DELETE FROM outlook_accounts WHERE id = ?", (outlook_id,))
    return {"success": True}


@router.get("/outlook-accounts/{outlook_id}")
async def get_outlook_account(outlook_id: int):
    """Get full Outlook account details (for editing)."""
    acc = await fetchone("SELECT * FROM outlook_accounts WHERE id = ?", (outlook_id,))
    if not acc:
        raise HTTPException(404, "Outlook 账号不存在")
    return dict(acc)


@router.put("/outlook-accounts/{outlook_id}")
async def update_outlook_account(outlook_id: int, req: OutlookUpdateRequest):
    """Update an Outlook account's credentials."""
    acc = await fetchone("SELECT id FROM outlook_accounts WHERE id = ?", (outlook_id,))
    if not acc:
        raise HTTPException(404, "Outlook 账号不存在")

    updates = []
    params = []
    for field, value in req.model_dump(exclude_none=True).items():
        updates.append(f"{field} = ?")
        params.append(value)

    if not updates:
        return {"success": True}

    updates.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(outlook_id)

    await execute(
        f"UPDATE outlook_accounts SET {', '.join(updates)} WHERE id = ?",
        tuple(params),
    )
    return {"success": True}


# ─── Auto-login ─────────────────────────────────────────────────────────


@router.post("/outlook-accounts/{outlook_id}/login")
async def auto_login_single(outlook_id: int):
    """Auto-login a single Outlook account to Anything."""
    await _repair_broken_outlook_links()
    acc = await fetchone("SELECT * FROM outlook_accounts WHERE id = ?", (outlook_id,))
    if not acc:
        raise HTTPException(404, "Outlook 账号不存在")
    if acc["status"] == "linked":
        raise HTTPException(400, "该账号已关联 Anything 账号")

    result = await login_and_add_account(
        outlook_id=acc["id"],
        email=acc["email"],
        ms_refresh_token=acc["ms_refresh_token"],
        client_id=acc["client_id"],
    )
    return result


@router.post("/outlook-accounts/{outlook_id}/relogin")
async def relogin_single(outlook_id: int):
    """Re-login an Outlook account (e.g. when linked account's token expired).
    Resets the linked account and performs a fresh login."""
    acc = await fetchone("SELECT * FROM outlook_accounts WHERE id = ?", (outlook_id,))
    if not acc:
        raise HTTPException(404, "Outlook 账号不存在")

    now = datetime.now(timezone.utc).isoformat()

    # If there's a linked account, remove or reset it
    if acc["linked_account_id"]:
        await execute(
            "DELETE FROM accounts WHERE id = ?",
            (acc["linked_account_id"],),
        )

    # Reset Outlook account status to pending
    await execute(
        "UPDATE outlook_accounts SET status = 'pending', linked_account_id = NULL, "
        "last_error = NULL, updated_at = ? WHERE id = ?",
        (now, outlook_id),
    )

    # Perform fresh login
    result = await login_and_add_account(
        outlook_id=acc["id"],
        email=acc["email"],
        ms_refresh_token=acc["ms_refresh_token"],
        client_id=acc["client_id"],
    )
    return result


@router.post("/outlook-accounts/login-all")
async def auto_login_all():
    """Auto-login all pending Outlook accounts."""
    await _repair_broken_outlook_links()
    accounts = await fetchall(
        "SELECT * FROM outlook_accounts WHERE status IN ('pending', 'error')"
    )
    results = {"total": len(accounts), "success": 0, "failed": 0, "details": []}
    if not accounts:
        return results

    semaphore = asyncio.Semaphore(3)

    async def _worker(acc: dict) -> tuple[dict, dict]:
        async with semaphore:
            result = await login_and_add_account(
                outlook_id=acc["id"],
                email=acc["email"],
                ms_refresh_token=acc["ms_refresh_token"],
                client_id=acc["client_id"],
                reload_account_pool=False,
            )
            return acc, result

    worker_results = await asyncio.gather(*[_worker(acc) for acc in accounts])

    for acc, result in worker_results:
        if result.get("success"):
            results["success"] += 1
        else:
            results["failed"] += 1
        results["details"].append({
            "email": acc["email"],
            "success": result.get("success", False),
            "error": result.get("error"),
        })

    if results["success"] > 0:
        await account_pool.load()

    return results
