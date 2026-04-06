"""Account pool with rotation, failure tracking, quota-aware selection, and client caching."""

import asyncio
import logging
import random
import time
from datetime import datetime, timezone

from anything_client import AnythingClient
from database.connection import execute, fetchall, fetchone
from services.runtime_state import delete_session_account, get_session_account, set_session_account

logger = logging.getLogger(__name__)

# Error keywords that indicate quota/rate limit exhaustion
QUOTA_ERROR_KEYWORDS = [
    "rate limit",
    "quota",
    "exceeded",
    "too many requests",
    "credit",
    "limit reached",
    "capacity",
    "overloaded",
    "generation limit reached",
]

# Account/project permissions that can be retried on another account,
# but should not be recorded as quota exhaustion.
RETRYABLE_ACCOUNT_ERROR_KEYWORDS = [
    "does not have access to the requested project group",
]

PERMISSION_BLOCKED_ERROR_KEYWORDS = [
    "user is not allowed to create a new chat message",
]


class AccountPool:
    """Manages multiple Anything AI accounts with rotation."""
    SESSION_TTL_SECONDS = 86400

    def __init__(self):
        self._clients: dict[int, AnythingClient] = {}
        self._in_use: set[int] = set()
        self._rr_index: int = 0
        self._fail_counts: dict[int, int] = {}
        self._session_bindings: dict[str, tuple[int, float]] = {}
        self._lock = asyncio.Lock()

    async def load(self):
        """Load accounts from DB and pre-create clients.
        Also reset quota-exhausted accounts from previous runs."""
        # Reset previously exhausted accounts so they get retried
        await execute(
            "UPDATE accounts SET credit_balance = NULL "
            "WHERE is_active = 1 AND credit_balance = 0"
        )
        # Reset error accounts that might have been temporary failures
        await execute(
            "UPDATE accounts SET status = 'active', last_error = NULL "
            "WHERE is_active = 1 AND status = 'error'"
        )

        accounts = await fetchall(
            "SELECT * FROM accounts WHERE is_active = 1 AND status = 'active'"
        )
        async with self._lock:
            self._clients.clear()
            self._in_use.clear()
            self._fail_counts.clear()
            self._session_bindings.clear()
            for acc in accounts:
                self._get_or_create_client(acc)
        logger.info(f"Account pool loaded: {len(accounts)} active accounts (reset stale statuses)")

    def _get_or_create_client(self, acc: dict) -> AnythingClient:
        aid = acc["id"]
        if aid not in self._clients:
            self._clients[aid] = AnythingClient(
                access_token=acc["access_token"],
                refresh_token=acc["refresh_token"],
                project_group_id=acc["project_group_id"],
                proxy_url=acc.get("proxy_url"),
                account_id=aid,
            )
        return self._clients[aid]

    def invalidate(self, account_id: int = -1):
        """Remove cached client so next pick creates a fresh one.
        If account_id is -1, clear all cached clients."""
        if account_id == -1:
            self._clients.clear()
            self._in_use.clear()
            self._fail_counts.clear()
            self._session_bindings.clear()
        else:
            self._clients.pop(account_id, None)
            self._in_use.discard(account_id)
            self._fail_counts.pop(account_id, None)
            self._drop_account_bindings(account_id)

    def _prune_session_bindings(self):
        now = time.monotonic()
        expired = [
            session_key
            for session_key, (_, ts) in self._session_bindings.items()
            if now - ts > self.SESSION_TTL_SECONDS
        ]
        for session_key in expired:
            self._session_bindings.pop(session_key, None)

    async def bind_session(self, session_key: str, account_id: int):
        if not session_key:
            return
        self._prune_session_bindings()
        self._session_bindings[session_key] = (account_id, time.monotonic())
        await set_session_account(session_key, account_id, self.SESSION_TTL_SECONDS)

    async def unbind_session(self, session_key: str):
        if session_key:
            self._session_bindings.pop(session_key, None)
            await delete_session_account(session_key)

    def _drop_account_bindings(self, account_id: int):
        stale_keys = [
            session_key
            for session_key, (bound_account_id, _) in self._session_bindings.items()
            if bound_account_id == account_id
        ]
        for session_key in stale_keys:
            self._session_bindings.pop(session_key, None)

    async def get_bound_account(self, session_key: str) -> int | None:
        if not session_key:
            return None
        self._prune_session_bindings()
        binding = self._session_bindings.get(session_key)
        if binding:
            account_id, _ = binding
        else:
            account_id = await get_session_account(session_key)
            if account_id is None:
                return None
        row = await fetchone(
            "SELECT id FROM accounts WHERE id = ? AND is_active = 1 AND status = 'active'",
            (account_id,),
        )
        if not row:
            await self.unbind_session(session_key)
            return None
        self._session_bindings[session_key] = (account_id, time.monotonic())
        return account_id

    def _get_client_state(self, account_id: int) -> tuple[str, str, str] | None:
        """Return the latest client state for DB sync, if the client is cached."""
        client = self._clients.get(account_id)
        if not client:
            return None
        return client._access_token, client._refresh_token, client._project_group_id

    async def pick_account(
        self,
        strategy: str = "lru",
        exclude_ids: set | None = None,
        preferred_account_id: int | None = None,
    ) -> tuple[int, AnythingClient]:
        """Select an account using the given strategy.
        Returns (account_id, client).
        Strategies: lru (least recently used), round_robin, random.

        Prefers accounts with credit_balance > 0 or NULL (unchecked).
        Excludes accounts in exclude_ids set.
        """
        # Prefer accounts with remaining balance (or unchecked)
        accounts = await fetchall(
            "SELECT * FROM accounts WHERE is_active = 1 AND status = 'active' "
            "AND (credit_balance IS NULL OR credit_balance > 0) "
            "ORDER BY last_used_at ASC NULLS FIRST"
        )

        # Filter out excluded IDs
        if exclude_ids:
            accounts = [a for a in accounts if a["id"] not in exclude_ids]

        # Fallback: if all have 0 balance, try any active account
        if not accounts:
            accounts = await fetchall(
                "SELECT * FROM accounts WHERE is_active = 1 AND status = 'active' "
                "ORDER BY last_used_at ASC NULLS FIRST"
            )
            if exclude_ids:
                accounts = [a for a in accounts if a["id"] not in exclude_ids]

        if not accounts:
            raise RuntimeError("没有可用的账号")

        async with self._lock:
            accounts = [a for a in accounts if a["id"] not in self._in_use]
            if not accounts:
                raise RuntimeError("No idle accounts available")
            selected = None
            if preferred_account_id is not None:
                selected = next((a for a in accounts if a["id"] == preferred_account_id), None)

            if selected is None:
                if strategy == "round_robin":
                    idx = self._rr_index % len(accounts)
                    self._rr_index += 1
                    selected = accounts[idx]
                elif strategy == "random":
                    selected = random.choice(accounts)
                else:  # lru
                    selected = accounts[0]
            self._in_use.add(selected["id"])

        aid = selected["id"]
        client = self._get_or_create_client(selected)

        # Update last_used_at
        now = datetime.now(timezone.utc).isoformat()
        await execute(
            "UPDATE accounts SET last_used_at = ?, updated_at = ? WHERE id = ?",
            (now, now, aid),
        )

        return aid, client

    async def release_account(self, account_id: int):
        """Release an in-use account so it can be selected again."""
        async with self._lock:
            self._in_use.discard(account_id)

    async def record_success(self, account_id: int):
        """Record a successful request and persist the latest client state."""
        self._fail_counts[account_id] = 0
        now = datetime.now(timezone.utc).isoformat()

        client_state = self._get_client_state(account_id)
        if client_state:
            access_token, refresh_token, project_group_id = client_state
            await execute(
                "UPDATE accounts SET total_requests = total_requests + 1, "
                "access_token = ?, refresh_token = ?, project_group_id = ?, "
                "last_error = NULL, updated_at = ? WHERE id = ?",
                (access_token, refresh_token, project_group_id, now, account_id),
            )
        else:
            await execute(
                "UPDATE accounts SET total_requests = total_requests + 1, "
                "last_error = NULL, updated_at = ? WHERE id = ?",
                (now, account_id),
            )

    async def record_failure(self, account_id: int, error: str):
        """Record a failed request. After 3 consecutive failures, mark account as error."""
        count = self._fail_counts.get(account_id, 0) + 1
        self._fail_counts[account_id] = count
        now = datetime.now(timezone.utc).isoformat()
        client_state = self._get_client_state(account_id)

        if count >= 3:
            if client_state:
                access_token, refresh_token, project_group_id = client_state
                await execute(
                    "UPDATE accounts SET status = 'error', access_token = ?, refresh_token = ?, "
                    "project_group_id = ?, last_error = ?, updated_at = ? WHERE id = ?",
                    (access_token, refresh_token, project_group_id, error, now, account_id),
                )
            else:
                await execute(
                    "UPDATE accounts SET status = 'error', last_error = ?, updated_at = ? WHERE id = ?",
                    (error, now, account_id),
                )
            self.invalidate(account_id)
            logger.warning(f"Account {account_id} marked as error after {count} failures: {error}")
        else:
            if client_state:
                access_token, refresh_token, project_group_id = client_state
                await execute(
                    "UPDATE accounts SET access_token = ?, refresh_token = ?, project_group_id = ?, "
                    "last_error = ?, updated_at = ? WHERE id = ?",
                    (access_token, refresh_token, project_group_id, error, now, account_id),
                )
            else:
                await execute(
                    "UPDATE accounts SET last_error = ?, updated_at = ? WHERE id = ?",
                    (error, now, account_id),
                )

    async def mark_quota_exhausted(self, account_id: int):
        """Mark an account as having exhausted its quota."""
        now = datetime.now(timezone.utc).isoformat()
        await execute(
            "UPDATE accounts SET credit_balance = 0, balance_checked_at = ?, updated_at = ? WHERE id = ?",
            (now, now, account_id),
        )
        self._drop_account_bindings(account_id)
        logger.warning(f"Account {account_id} marked as quota exhausted")

    async def mark_permission_blocked(self, account_id: int, error: str):
        """Quarantine an account that can log in but is forbidden to create chat messages."""
        now = datetime.now(timezone.utc).isoformat()
        client_state = self._get_client_state(account_id)
        if client_state:
            access_token, refresh_token, project_group_id = client_state
            await execute(
                "UPDATE accounts SET status = 'permission_blocked', access_token = ?, refresh_token = ?, "
                "project_group_id = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (access_token, refresh_token, project_group_id, error, now, account_id),
            )
        else:
            await execute(
                "UPDATE accounts SET status = 'permission_blocked', last_error = ?, updated_at = ? WHERE id = ?",
                (error, now, account_id),
            )
        self.invalidate(account_id)
        logger.warning(f"Account {account_id} marked as permission_blocked")

    @staticmethod
    def is_quota_error(error: str) -> bool:
        """Check if an error message indicates quota/rate limit exhaustion."""
        lower = error.lower()
        return any(kw in lower for kw in QUOTA_ERROR_KEYWORDS)

    @staticmethod
    def is_retryable_account_error(error: str) -> bool:
        """Check if an error should switch accounts without marking quota exhausted."""
        lower = error.lower()
        return any(kw in lower for kw in RETRYABLE_ACCOUNT_ERROR_KEYWORDS)

    @staticmethod
    def is_permission_blocked_error(error: str) -> bool:
        """Check if an account should be quarantined from future chat attempts."""
        lower = error.lower()
        return any(kw in lower for kw in PERMISSION_BLOCKED_ERROR_KEYWORDS)

    async def try_refresh_token(self, account_id: int, client: AnythingClient) -> bool:
        """Try to refresh an account's token and update DB."""
        ok = await client.refresh_access_token()
        now = datetime.now(timezone.utc).isoformat()
        if ok:
            await execute(
                "UPDATE accounts SET access_token = ?, refresh_token = ?, "
                "last_refresh_at = ?, status = 'active', last_error = NULL, updated_at = ? WHERE id = ?",
                (client._access_token, client._refresh_token, now, now, account_id),
            )
            self._fail_counts[account_id] = 0
            logger.info(f"Account {account_id} token refreshed successfully")
        else:
            await execute(
                "UPDATE accounts SET status = 'token_expired', "
                "last_error = 'Token refresh failed', updated_at = ? WHERE id = ?",
                (now, account_id),
            )
            self.invalidate(account_id)
            logger.warning(f"Account {account_id} token refresh failed")
        return ok


# Global singleton
account_pool = AccountPool()
