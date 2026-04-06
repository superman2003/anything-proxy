"""Anything AI GraphQL client with WebSocket streaming support."""

import asyncio
import json
import logging
import re
import time
import uuid
from typing import AsyncGenerator, Optional, Tuple

import httpx

from config import settings
from services.pricing import DEFAULT_MODEL, PRIMARY_MODELS

logger = logging.getLogger(__name__)

# ─── Model Mapping ─────────────────────────────────────────────────────

MODEL_MAPPING = {
    "claude-opus-4-6": "ANTHROPIC_CLAUDE_OPUS_4_6",
    "claude-opus-4-6[1m]": "ANTHROPIC_CLAUDE_OPUS_4_6",
    "claude-opus-4-6-thinking": "ANTHROPIC_CLAUDE_OPUS_4_6",
    "claude-opus-4-5-thinking": "ANTHROPIC_CLAUDE_OPUS_4_6",
    "claude-opus-4-5-20251101": "ANTHROPIC_CLAUDE_OPUS_4_6",
    "claude-sonnet-4-6": "ANTHROPIC_CLAUDE_SONNET_4",
    "claude-sonnet-4-6[1m]": "ANTHROPIC_CLAUDE_SONNET_4",
    "claude-sonnet-4-6-thinking": "ANTHROPIC_CLAUDE_SONNET_4",
    "claude-sonnet-4-5": "ANTHROPIC_CLAUDE_SONNET_4_5",
    "claude-sonnet-4-5[1m]": "ANTHROPIC_CLAUDE_SONNET_4_5",
    "claude-sonnet-4-5-thinking": "ANTHROPIC_CLAUDE_SONNET_4_5",
    "claude-sonnet-4-5-20250929": "ANTHROPIC_CLAUDE_SONNET_4_5",
    "claude-haiku-4-5": "ANTHROPIC_CLAUDE_SONNET_4_5",
    "claude-haiku-4-5-20251001": "ANTHROPIC_CLAUDE_SONNET_4_5",
    "gpt-5.4": "CHAT_GPT",
    "gpt5.4": "CHAT_GPT",
}

SUPPORTED_MODELS = list(PRIMARY_MODELS)


def get_mapped_model(model: str) -> Optional[str]:
    return MODEL_MAPPING.get(model.lower())


# ─── Response Parser ───────────────────────────────────────────────────

THINKING_BLOCK_RE = re.compile(
    r'<file-based-block[^>]*uiType="thinking"[^>]*subtext="([^"]*)"[^>]*>[\s\S]*?</file-based-block>',
    re.DOTALL,
)
THINKING_BLOCK_FULL_RE = re.compile(
    r"<file-based-block[^>]*uiType=\"thinking\"[^>]*>[\s\S]*?</file-based-block>",
    re.DOTALL,
)


def parse_response(response: str) -> Tuple[Optional[str], str]:
    """Parse an Anything response into (thinking, text) parts."""
    thinking = None
    match = THINKING_BLOCK_RE.search(response)
    if match:
        raw = match.group(1)
        thinking = raw.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")

    text = THINKING_BLOCK_FULL_RE.sub("", response).strip()
    return thinking, text


# ─── GraphQL Queries ───────────────────────────────────────────────────

Q_ME = """query Me { me { id email username organizations { id } } }"""

Q_PROJECT_GROUPS = """query GetProjectGroups($input: ProjectGroupsInput) {
  projectGroups(input: $input) {
    edges { node { id name slug } }
  }
}"""

M_CREATE_PROJECT_GROUP = """mutation CreateProjectGroup($input: CreateProjectGroupInput!) {
  createProjectGroup(input: $input) {
    success
    projectGroup { id name }
    project { id }
  }
}"""

M_GENERATE = """mutation GenerateProjectGroupRevisionFromChat(
  $input: GenerateProjectGroupRevisionFromChatInput!
) {
  generateProjectGroupRevisionFromChat(input: $input) {
    success
    projectGroupRevision { id }
    errors { kind message }
  }
}"""

Q_REVISION_CONTENT = """query GetHistoricalRevision($id: ID!) {
  projectGroupRevisionById(id: $id) {
    id
    status
    response
  }
}"""

Q_BILLING = """query GetOrganizationBilling($organizationId: ID!) {
  organizationById(id: $organizationId) {
    id
    creditBalance
    plan
  }
}"""

M_SELECT_INTEGRATION = """mutation SelectIntegrationFromChat(
  $input: SelectIntegrationFromChatInput!
) {
  selectIntegrationFromChat(input: $input) {
    success
  }
}"""

SUB_REVISION_UPDATE = """subscription GetProjectGroupRevisionContentUpdate($id: ID!) {
  projectGroupRevisionContentUpdate(id: $id) {
    id
    response
    status
    autoSelectedAction
  }
}"""


# ─── Client ────────────────────────────────────────────────────────────


class AnythingClient:
    """GraphQL client for Anything AI with auto token refresh."""

    GRAPHQL_HTTP = "https://www.anything.com/api/graphql"
    GRAPHQL_WS = "wss://api.createanything.com/subscriptions"
    REFRESH_URL = "https://www.anything.com/api/refresh_token"

    def __init__(
        self,
        access_token: str = "",
        refresh_token: str = "",
        project_group_id: str = "",
        proxy_url: str | None = None,
        account_id: int | None = None,
    ):
        self._access_token = access_token or settings.access_token
        self._refresh_token = refresh_token or settings.refresh_token
        self._project_group_id = project_group_id or settings.project_group_id
        self._proxy_url = proxy_url if proxy_url is not None else settings.proxy_url
        self._account_id = account_id
        self._refreshing = False

    def update_tokens(self, access_token: str, refresh_token: str = ""):
        """Update tokens after refresh."""
        self._access_token = access_token
        if refresh_token:
            self._refresh_token = refresh_token

    def _log_prefix(self) -> str:
        if self._account_id is None:
            return ""
        return f"[account {self._account_id}] "

    # ── HTTP helpers ──

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": self._access_token,
            "Origin": "https://www.anything.com",
            "Referer": "https://www.anything.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

    def _cookie_header(self) -> str:
        """Build the cookie header shape used by Anything's web client."""
        parts = []
        if self._access_token:
            parts.append(f"lS_authToken={self._access_token}")
        if self._refresh_token:
            parts.append(f"qid={self._refresh_token}")
            parts.append(f"refresh_token={self._refresh_token}")
        return "; ".join(parts)

    def _http_client(self) -> httpx.AsyncClient:
        kwargs = {"timeout": 30.0, "follow_redirects": True}
        if self._proxy_url:
            kwargs["proxy"] = self._proxy_url
        return httpx.AsyncClient(**kwargs)

    # ── Token Refresh ──

    async def refresh_access_token(self) -> bool:
        """Refresh access token via POST /api/refresh_token.
        Needs the qid cookie. Returns { ok: true, accessToken: "eyJ..." }"""
        if self._refreshing:
            return False
        self._refreshing = True
        try:
            async with self._http_client() as client:
                resp = await client.post(
                    self.REFRESH_URL,
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://www.anything.com",
                        "Referer": "https://www.anything.com/",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                        "Cookie": self._cookie_header(),
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    new_token = data.get("accessToken") or data.get("access_token")
                    if new_token:
                        self._access_token = new_token
                        logger.info(f"{self._log_prefix()}Access token refreshed successfully")
                        # Update refresh_token from Set-Cookie if present
                        for cookie in resp.cookies.jar:
                            if cookie.name in ("refresh_token", "qid") and cookie.value:
                                self._refresh_token = cookie.value
                        return True
                    logger.warning(f"{self._log_prefix()}Refresh response missing token: {data}")
                else:
                    logger.error(f"{self._log_prefix()}Token refresh failed: HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"{self._log_prefix()}Token refresh error: {e}")
            return False
        finally:
            self._refreshing = False

    # ── Core GraphQL Request ──

    async def _gql(self, query: str, variables: dict = None, retry_on_401: bool = True) -> dict:
        """Execute a GraphQL request with auto 401 retry."""
        body = {"query": query}
        if variables:
            body["variables"] = variables

        async with self._http_client() as client:
            resp = await client.post(
                self.GRAPHQL_HTTP,
                json=body,
                headers={**self._headers(), "Cookie": self._cookie_header()},
            )

            if resp.status_code == 401 and retry_on_401:
                logger.info("Got 401, refreshing token...")
                if await self.refresh_access_token():
                    return await self._gql(query, variables, retry_on_401=False)

            # GraphQL may return 400 with error details - parse them
            result = resp.json()

            if "errors" in result and result["errors"]:
                err_msg = result["errors"][0].get("message", str(result["errors"]))
                err_code = result["errors"][0].get("extensions", {}).get("code", "")
                logger.error(f"{self._log_prefix()}GraphQL error: {err_msg} | code={err_code} | full={result['errors']}")
                raise Exception(f"GraphQL error: {err_msg}")

            if resp.status_code >= 400:
                resp.raise_for_status()

            return result

    # ── Public API ──

    async def get_me(self) -> dict:
        """Verify token and get current user info."""
        result = await self._gql(Q_ME)
        return result.get("data", {}).get("me", {})

    async def get_billing_info(self) -> dict:
        """Get account credit balance and plan via organization query."""
        # First get organization ID from user info
        me = await self.get_me()
        orgs = me.get("organizations") or []
        if not orgs:
            return {"creditBalance": None, "plan": "", "organization_id": ""}

        org_id = orgs[0].get("id", "")
        if not org_id:
            return {"creditBalance": None, "plan": "", "organization_id": ""}

        result = await self._gql(Q_BILLING, {"organizationId": org_id})
        org = result.get("data", {}).get("organizationById") or {}
        return {
            "creditBalance": org.get("creditBalance"),
            "plan": org.get("plan") or "",
            "organization_id": org_id,
        }

    async def get_project_groups(self) -> list:
        """Get user's project list."""
        result = await self._gql(Q_PROJECT_GROUPS, {"input": {}})
        edges = result.get("data", {}).get("projectGroups", {}).get("edges", [])
        return [e["node"] for e in edges]

    async def create_project_group(self, name: str = "New Project") -> dict:
        """Create a new project group. Returns {"id": ..., "name": ...}."""
        # Need organizationId from user info
        me = await self.get_me()
        orgs = me.get("organizations") or []
        if not orgs:
            raise Exception("No organization found for this account")
        org_id = orgs[0]["id"]

        result = await self._gql(M_CREATE_PROJECT_GROUP, {
            "input": {"name": name, "organizationId": org_id}
        })
        data = result.get("data", {}).get("createProjectGroup", {})
        if not data.get("success"):
            raise Exception(f"Failed to create project group: {result}")
        return data.get("projectGroup", {})

    async def select_model(self, model: str):
        """Select AI model for the project via SelectIntegrationFromChat."""
        await self._gql(M_SELECT_INTEGRATION, {
            "input": {
                "projectGroupId": self._project_group_id,
                "integration": model,
            }
        })
        logger.info(f"{self._log_prefix()}Model selected: {model}")

    async def send_message(self, content: str, model: str = None, use_thinking: bool = True) -> str:
        """Send a chat message. Returns the revision ID.
        Auto-refreshes project_group_id and token on specific errors.
        Raises directly on quota/limit errors for account pool to handle."""
        last_error = None
        for attempt in range(3):
            try:
                return await self._do_send(content, model, use_thinking)
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                # Quota/limit errors — don't retry, let account pool switch accounts
                if "generation limit" in error_str:
                    raise
                # Project group access error — refresh and retry once
                if "does not have access" in error_str or "project group" in error_str:
                    logger.warning(f"{self._log_prefix()}Project group access denied (attempt {attempt+1}), refreshing...")
                    if not await self._refresh_project_group():
                        raise
                    continue
                if "not allowed" in error_str:
                    logger.warning(
                        f"{self._log_prefix()}Chat creation forbidden (attempt {attempt+1}), leaving token unchanged and letting caller switch accounts"
                    )
                    raise
                raise
        raise last_error

    async def _do_send(self, content: str, model: str = None, use_thinking: bool = True) -> str:
        """Internal: select model + send message."""
        if model:
            await self.select_model(model)

        input_data = {
            "projectGroupId": self._project_group_id,
            "content": content,
        }

        # Log the request for debugging
        logger.info(f"{self._log_prefix()}Sending generate request: projectGroupId={self._project_group_id}, content_len={len(content)}")

        result = await self._gql(M_GENERATE, {"input": input_data})

        # Check for top-level errors (already handled by _gql raising Exception)
        gen = result.get("data", {}).get("generateProjectGroupRevisionFromChat")
        if not gen:
            logger.error(f"{self._log_prefix()}Generate returned unexpected result: {result}")
            raise Exception(f"Generate returned no data: {result}")

        if not gen["success"]:
            errors = gen.get("errors", [])
            logger.error(f"{self._log_prefix()}Generate failed: errors={errors}")
            raise Exception(f"Send message failed: {errors}")

        revision_id = gen["projectGroupRevision"]["id"]
        logger.info(f"{self._log_prefix()}Generate success: revision_id={revision_id}")
        return revision_id

    async def _refresh_project_group(self) -> bool:
        """Create a new project group to replace the current one."""
        try:
            import uuid
            name = f"proxy-{uuid.uuid4().hex[:8]}"
            new_group = await self.create_project_group(name)
            new_id = new_group.get("id")
            if new_id:
                logger.info(f"{self._log_prefix()}New project group created: {self._project_group_id} -> {new_id} ({name})")
                self._project_group_id = new_id
                return True
            logger.warning(f"{self._log_prefix()}Create project group returned no ID, falling back to existing groups")
        except Exception as e:
            logger.warning(f"{self._log_prefix()}Failed to create project group: {e}, falling back to existing groups")

        # Fallback: try existing groups
        try:
            groups = await self.get_project_groups()
            if groups:
                new_id = groups[0]["id"]
                if new_id != self._project_group_id:
                    logger.info(f"{self._log_prefix()}Project group refreshed: {self._project_group_id} -> {new_id}")
                    self._project_group_id = new_id
                    return True
            logger.warning(f"{self._log_prefix()}No project groups available")
            return False
        except Exception as e:
            logger.error(f"{self._log_prefix()}Failed to refresh project group: {e}")
            return False

    async def get_revision_content(self, revision_id: str) -> Optional[dict]:
        """Get the content of a specific revision by ID."""
        result = await self._gql(Q_REVISION_CONTENT, {"id": revision_id})
        return result.get("data", {}).get("projectGroupRevisionById")

    async def poll_result(self, revision_id: str) -> dict:
        """Poll for revision completion. Returns the completed revision data."""
        deadline = time.time() + settings.poll_timeout

        while True:
            if time.time() > deadline:
                raise TimeoutError(f"Polling timed out after {settings.poll_timeout}s")

            rev = await self.get_revision_content(revision_id)
            if rev:
                status = rev.get("status", "")
                if status in ("COMPLETED", "VALID"):
                    return rev
                if status in ("FAILED", "CANCELLED"):
                    raise Exception(f"Generation {status}")

            await asyncio.sleep(settings.poll_interval)

    async def chat(self, content: str, model: str, use_thinking: bool = True) -> Tuple[Optional[str], str, dict]:
        """High-level: send message, poll for result, parse response.
        Returns (thinking, text, metadata)."""
        mapped_model = get_mapped_model(model or DEFAULT_MODEL)
        if not mapped_model:
            raise ValueError(f"Unsupported model: {model}. Supported: {list(MODEL_MAPPING.keys())}")

        # Step 1: Send
        revision_id = await self.send_message(content, mapped_model, use_thinking)
        logger.info(f"Message sent, revision_id={revision_id}")

        # Step 2: Poll
        rev = await self.poll_result(revision_id)
        logger.info(f"Generation complete, status={rev.get('status')}")

        # Step 3: Extract response directly from revision
        full_response = rev.get("response", "") or ""

        # Step 4: Parse
        thinking, text = parse_response(full_response)

        metadata = {"revision_id": revision_id}
        return thinking, text, metadata

    async def chat_stream(self, content: str, model: str, use_thinking: bool = True, revision_id: str = None) -> AsyncGenerator[dict, None]:
        """Stream chat response via WebSocket subscription.
        If revision_id is provided, skip send_message and subscribe directly.
        Yields dicts with keys: type ('thinking'|'text'|'done'|'error'), content."""
        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed, falling back to polling")
            thinking, text, meta = await self.chat(content, model, use_thinking)
            if thinking:
                yield {"type": "thinking", "content": thinking}
            yield {"type": "text", "content": text}
            yield {"type": "done", "content": ""}
            return

        mapped_model = get_mapped_model(model or DEFAULT_MODEL)
        if not mapped_model:
            raise ValueError(f"Unsupported model: {model}")

        # Step 1: Send message to get revision ID (skip if already provided)
        if not revision_id:
            revision_id = await self.send_message(content, mapped_model, use_thinking)
        logger.info(f"Message sent, revision_id={revision_id}, subscribing via WS...")

        # Step 2: Subscribe via WebSocket
        prev_response = ""
        try:
            extra_headers = {}
            if settings.proxy_url:
                # websockets doesn't support proxy directly, log warning
                logger.warning("WebSocket proxy not supported, connecting directly")

            async with websockets.connect(
                self.GRAPHQL_WS,
                subprotocols=["graphql-transport-ws"],
                additional_headers=extra_headers,
            ) as ws:
                # Init connection with auth
                await ws.send(json.dumps({
                    "type": "connection_init",
                    "payload": {"authorization": self._access_token},
                }))

                # Wait for connection_ack
                msg = json.loads(await ws.recv())
                if msg.get("type") != "connection_ack":
                    raise Exception(f"WS connection failed: {msg}")

                # Subscribe to revision updates
                await ws.send(json.dumps({
                    "id": "1",
                    "type": "subscribe",
                    "payload": {
                        "query": SUB_REVISION_UPDATE,
                        "variables": {"id": revision_id},
                    },
                }))

                # Process updates
                async for raw in ws:
                    msg = json.loads(raw)

                    if msg.get("type") == "next":
                        data = msg.get("payload", {}).get("data", {}).get("projectGroupRevisionContentUpdate", {})
                        if data:
                            response = data.get("response", "")
                            status = data.get("status", "")

                            # Calculate the delta (new content since last update)
                            if response and len(response) > len(prev_response):
                                delta = response[len(prev_response):]
                                prev_response = response

                                # Try to parse thinking vs text
                                # During streaming, just yield raw text deltas
                                yield {"type": "text", "content": delta}

                            if status in ("COMPLETED", "VALID", "FAILED", "CANCELLED"):
                                if status in ("FAILED", "CANCELLED"):
                                    yield {"type": "error", "content": f"Generation {status}"}
                                else:
                                    yield {"type": "done", "content": ""}
                                break

                    elif msg.get("type") == "error":
                        yield {"type": "error", "content": str(msg.get("payload", "Unknown error"))}
                        break

                    elif msg.get("type") == "complete":
                        yield {"type": "done", "content": ""}
                        break

        except Exception as e:
            logger.error(f"WebSocket streaming error: {e}, falling back to polling")
            # Fallback: poll for the result
            try:
                rev = await self.poll_result(revision_id)
                full_response = rev.get("response", "") or ""
                thinking, text = parse_response(full_response)
                if thinking:
                    yield {"type": "thinking", "content": thinking}
                yield {"type": "text", "content": text}
            except Exception as poll_err:
                yield {"type": "error", "content": str(poll_err)}
            yield {"type": "done", "content": ""}
