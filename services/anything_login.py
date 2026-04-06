"""Anything AI auto-login via Magic Link.

Flow (borrowed from anything_register.py and adapted for Outlook):
1. Request Anything to send a Magic Login Link email (GraphQL SignUp mutation)
2. Poll Outlook inbox for the email (via Microsoft Graph API)
3. Open the Magic Link (follow redirects step-by-step, capture cookies)
4. Extract refresh_token from cookies (qid / refresh_token)
5. Call /api/refresh_token to get access_token
6. Call get_me() + get_project_groups() to get account info
7. Save to database
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

from anything_client import AnythingClient
from config import settings
from database.connection import execute, fetchone
from services.account_pool import account_pool
from services.outlook_client import OutlookClient

logger = logging.getLogger(__name__)

ANYTHING_BASE = "https://www.anything.com"
GRAPHQL_URL = f"{ANYTHING_BASE}/api/graphql"
LANGUAGE = "zh-CN"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# GraphQL mutation - same as anything_register.py uses for signup/login
LOGIN_MUTATION = """
mutation SignUpWithAppPrompt($input: SignUpWithAppPromptInput!) {
  signUpAndStartAgent(input: $input) {
    ... on SignUpWithoutAppPromptPayload {
      success
      accessToken
      project { id projectGroup { id __typename } __typename }
      projectGroup { id __typename }
      user {
        id
        email
        username
        profile { firstName lastName __typename }
        __typename
      }
      organization { id __typename }
      __typename
    }
    ... on SignUpAndStartAgentErrorResult {
      success
      errors { kind message __typename }
      __typename
    }
    __typename
  }
}
""".strip()

def _http(proxy_url: str | None = None) -> httpx.AsyncClient:
    """Create HTTP client with optional proxy (mirrors anything_register.py)."""
    kw = {"timeout": 60.0, "follow_redirects": False}
    effective_proxy = proxy_url or settings.proxy_url
    if effective_proxy:
        kw["proxy"] = effective_proxy
    return httpx.AsyncClient(**kw)


def _build_anything_cookie_header(
    refresh_token: str = "",
    access_token: str = "",
    qid: str = "",
) -> str:
    """Build the cookie header shape used by Anything's web client."""
    effective_qid = qid or refresh_token
    effective_refresh = refresh_token or qid
    parts = []
    if access_token:
        parts.append(f"lS_authToken={access_token}")
    if effective_qid:
        parts.append(f"qid={effective_qid}")
    if effective_refresh:
        parts.append(f"refresh_token={effective_refresh}")
    return "; ".join(parts)


def _extract_cookie_state_from_jar(cookie_jar) -> dict:
    state = {"qid": "", "refresh_token": "", "access_token": ""}
    if not cookie_jar:
        return state

    for cookie in cookie_jar:
        value = getattr(cookie, "value", "")
        if not value:
            continue
        if cookie.name == "qid":
            state["qid"] = value
            state["refresh_token"] = state["refresh_token"] or value
        elif cookie.name == "refresh_token":
            state["refresh_token"] = value
            state["qid"] = state["qid"] or value
        elif cookie.name == "lS_authToken":
            state["access_token"] = value

    return state


def _extract_cookie_state_from_headers(headers: httpx.Headers) -> dict:
    state = {"qid": "", "refresh_token": "", "access_token": ""}
    set_cookie_headers = []
    if hasattr(headers, "get_list"):
        set_cookie_headers = headers.get_list("set-cookie")
    if not set_cookie_headers:
        header = headers.get("set-cookie", "")
        if header:
            set_cookie_headers = [header]

    for raw_header in set_cookie_headers:
        parsed = SimpleCookie()
        try:
            parsed.load(raw_header)
        except Exception:
            continue

        qid = parsed.get("qid")
        refresh = parsed.get("refresh_token")
        auth = parsed.get("lS_authToken")
        if qid and qid.value:
            state["qid"] = state["qid"] or qid.value
            state["refresh_token"] = state["refresh_token"] or qid.value
        if refresh and refresh.value:
            state["refresh_token"] = state["refresh_token"] or refresh.value
            state["qid"] = state["qid"] or refresh.value
        if auth and auth.value:
            state["access_token"] = state["access_token"] or auth.value

    return state


def _merge_cookie_states(*states: dict) -> dict:
    merged = {"qid": "", "refresh_token": "", "access_token": ""}
    for state in states:
        if not state:
            continue
        for key in merged:
            if not merged[key] and state.get(key):
                merged[key] = state[key]

    if not merged["refresh_token"] and merged["qid"]:
        merged["refresh_token"] = merged["qid"]
    if not merged["qid"] and merged["refresh_token"]:
        merged["qid"] = merged["refresh_token"]

    merged["cookie_header"] = _build_anything_cookie_header(
        refresh_token=merged["refresh_token"],
        access_token=merged["access_token"],
        qid=merged["qid"],
    )
    return merged


def _extract_anything_cookie_state(resp: httpx.Response, cookie_jar=None) -> dict:
    return _merge_cookie_states(
        _extract_cookie_state_from_jar(resp.cookies.jar),
        _extract_cookie_state_from_headers(resp.headers),
        _extract_cookie_state_from_jar(cookie_jar),
    )


def _extract_refresh_token_from_response(resp: httpx.Response) -> str:
    """Extract qid/refresh_token from cookies or Set-Cookie headers."""
    return _extract_anything_cookie_state(resp).get("refresh_token", "")


def _extract_magic_link_code(*urls: str | None) -> str:
    for raw_url in urls:
        if not raw_url:
            continue
        code = (parse_qs(urlparse(raw_url).query).get("code") or [None])[0]
        if code:
            return code
    return ""


async def verify_magic_link_code(
    email: str,
    code: str,
    proxy_url: str | None = None,
    client: httpx.AsyncClient | None = None,
    referer_url: str | None = None,
) -> dict:
    """Verify magic link code via SignInWithMagicLinkCode GraphQL mutation."""
    effective_proxy = proxy_url or settings.proxy_url
    proxy_kwargs = {"proxy": effective_proxy} if effective_proxy else {}

    mutation = """
mutation SignInWithMagicLinkCode($input: SignInWithMagicLinkCodeInput!) {
  signInWithMagicLinkCode(input: $input) {
    ... on SignInWithMagicLinkCodePayload {
      accessToken
      user {
        id
        email
        username
        profile { firstName lastName __typename }
        __typename
      }
      __typename
    }
    __typename
  }
}
""".strip()

    async def _run(active_client: httpx.AsyncClient) -> dict:
        gql_headers = {
            "accept": "application/graphql-response+json,application/json;q=0.9",
            "accept-language": f"{LANGUAGE},zh;q=0.9",
            "apollographql-client-name": "flux-web",
            "content-type": "application/json",
            "origin": ANYTHING_BASE,
            "referer": referer_url or f"{ANYTHING_BASE}/auth/magic-link?code={code}&email={email}",
            "user-agent": USER_AGENT,
        }
        resp = await active_client.post(GRAPHQL_URL, json={
            "operationName": "SignInWithMagicLinkCode",
            "variables": {
                "input": {
                    "email": email,
                    "codeAttempt": code,
                }
            },
            "extensions": {
                "clientLibrary": {"name": "@apollo/client", "version": "4.1.6"}
            },
            "query": mutation,
        }, headers=gql_headers)

        if resp.status_code != 200:
            logger.error(f"[{email}] SignInWithMagicLinkCode HTTP {resp.status_code}, body={resp.text[:300]}")

        data = resp.json()
        result = (data.get("data") or {}).get("signInWithMagicLinkCode") or {}
        if not result:
            errors = data.get("errors") or []
            logger.error(f"[{email}] GraphQL errors: {errors}")
            raise RuntimeError(f"Magic Link 验证 GraphQL 错误: {errors}")

        cookie_state = _extract_anything_cookie_state(resp, active_client.cookies.jar)
        if cookie_state["refresh_token"]:
            result["refreshToken"] = cookie_state["refresh_token"]
        if cookie_state["cookie_header"]:
            result["cookieHeader"] = cookie_state["cookie_header"]

        logger.info(f"[{email}] SignInWithMagicLinkCode 响应: typename={result.get('__typename')}, "
                    f"has_token={bool(result.get('accessToken'))}, "
                    f"has_refresh={bool(result.get('refreshToken'))}")
        return result

    if client is not None:
        return await _run(client)

    async with httpx.AsyncClient(timeout=60.0, **proxy_kwargs) as scoped_client:
        return await _run(scoped_client)


async def request_magic_link(
    email: str,
    proxy_url: str | None = None,
    max_retries: int = 2,
) -> dict:
    """Send a magic login link request to Anything for the given email.
    Includes warmup + retry logic from anything_register.py.
    Returns the GraphQL response data."""
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            async with _http(proxy_url) as client:
                # Warmup session (like anything_register.py's build_signup_session)
                warmup_headers = {
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "accept-language": f"{LANGUAGE},zh;q=0.9",
                    "cache-control": "no-cache",
                    "pragma": "no-cache",
                    "user-agent": USER_AGENT,
                }
                await client.get(f"{ANYTHING_BASE}/login", headers=warmup_headers)

                # GraphQL request with full headers (from anything_register.py)
                gql_headers = {
                    "accept": "application/graphql-response+json,application/json;q=0.9",
                    "accept-language": f"{LANGUAGE},zh;q=0.9",
                    "apollographql-client-name": "flux-web",
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "origin": ANYTHING_BASE,
                    "pragma": "no-cache",
                    "referer": f"{ANYTHING_BASE}/login",
                    "user-agent": USER_AGENT,
                }

                resp = await client.post(GRAPHQL_URL, json={
                    "operationName": "SignUpWithAppPrompt",
                    "variables": {
                        "input": {
                            "email": email,
                            "postLoginRedirect": None,
                            "language": LANGUAGE,
                        }
                    },
                    "extensions": {
                        "clientLibrary": {"name": "@apollo/client", "version": "4.1.6"}
                    },
                    "query": LOGIN_MUTATION,
                }, headers=gql_headers)
                resp.raise_for_status()
                data = resp.json()

                result = (data.get("data") or {}).get("signUpAndStartAgent") or {}
                if not result:
                    raise RuntimeError(f"GraphQL 响应异常: {data}")

                if result.get("__typename") == "SignUpAndStartAgentErrorResult":
                    errors = result.get("errors", [])
                    raise RuntimeError(f"请求 Magic Link 失败: {errors}")

                cookie_state = _extract_anything_cookie_state(resp, client.cookies.jar)
                if cookie_state["refresh_token"]:
                    result["refreshToken"] = cookie_state["refresh_token"]
                if cookie_state["cookie_header"]:
                    result["cookieHeader"] = cookie_state["cookie_header"]

                logger.info(f"[{email}] Magic Link 请求已发送 (尝试 {attempt})")
                return result

        except Exception as e:
            last_error = e
            logger.warning(f"[{email}] Magic Link 请求失败 (尝试 {attempt}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(2)

    raise RuntimeError(f"请求 Magic Link 失败 (已重试 {max_retries} 次): {last_error}")


async def open_magic_link(url: str, proxy_url: str | None = None, email: str = "") -> dict:
    """Open a magic link URL, follow redirects STEP BY STEP to capture cookies.
    Borrowed from anything_register.py: manual redirect following ensures we never
    miss Set-Cookie headers set at intermediate redirect hops."""
    effective_proxy = proxy_url or settings.proxy_url
    proxy_kwargs = {"proxy": effective_proxy} if effective_proxy else {}

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": f"{LANGUAGE},zh;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Referer": f"{ANYTHING_BASE}/login",
    }

    cookie_state = _merge_cookie_states()
    redirect_chain = []
    current_url = url
    max_redirects = 15

    async with httpx.AsyncClient(
        timeout=60.0,
        follow_redirects=False,  # Manual redirect following
        **proxy_kwargs,
    ) as client:
        for step in range(max_redirects):
            resp = await client.get(current_url, headers=headers)
            redirect_chain.append(current_url)
            hop_state = _extract_anything_cookie_state(resp, client.cookies.jar)
            cookie_state = _merge_cookie_states(cookie_state, hop_state)
            if hop_state["access_token"] or hop_state["refresh_token"]:
                logger.info(
                    f"Step {step+1}: 捕获 cookies "
                    f"(auth={bool(hop_state['access_token'])}, refresh={bool(hop_state['refresh_token'])})"
                )

            # Follow redirect or stop
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                if not location:
                    break
                # Handle relative URLs
                if location.startswith("/"):
                    parsed = urlparse(current_url)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                current_url = location
                logger.debug(f"Step {step+1}: 重定向到 {current_url[:80]}")
            else:
                # Final response (no more redirects)
                # Log response details for debugging
                logger.info(f"Step {step+1}: 最终响应 HTTP {resp.status_code}, "
                           f"url={current_url[:80]}, "
                           f"cookies={[c.name for c in resp.cookies.jar]}, "
                           f"content-type={resp.headers.get('content-type', '')}")
                break

        cookie_state = _merge_cookie_states(
            cookie_state,
            _extract_cookie_state_from_jar(client.cookies.jar),
        )
        final_url = current_url
        code = _extract_magic_link_code(final_url, *redirect_chain, url)
        verify_error = ""

        if email and code and not cookie_state["refresh_token"] and not cookie_state["access_token"]:
            try:
                logger.info("Magic Link 页面未直接下发 token，改为同会话调用 SignInWithMagicLinkCode")
                verify_result = await verify_magic_link_code(
                    email,
                    code,
                    proxy_url,
                    client=client,
                    referer_url=final_url,
                )
                cookie_state = _merge_cookie_states(
                    cookie_state,
                    {
                        "access_token": verify_result.get("accessToken") or "",
                        "refresh_token": verify_result.get("refreshToken") or "",
                    },
                    _extract_cookie_state_from_jar(client.cookies.jar),
                )
            except Exception as e:
                verify_error = str(e)
                logger.warning(f"Magic Link 同会话验证失败: {verify_error}")

        logger.info(f"Magic Link 打开完成: {final_url[:80]}, 重定向链: {len(redirect_chain)} 步")

        if not cookie_state["refresh_token"]:
            logger.warning(f"未能从 cookies 中提取 refresh_token, "
                         f"final_url={final_url}, redirects={redirect_chain}")

        return {
            "refresh_token": cookie_state["refresh_token"],
            "access_token": cookie_state["access_token"],
            "cookie_header": cookie_state["cookie_header"],
            "final_url": final_url,
            "redirect_chain": redirect_chain,
            "code": code,
            "verify_error": verify_error,
        }


async def get_tokens_from_refresh(
    refresh_token: str,
    proxy_url: str | None = None,
    access_token: str = "",
    cookie_header: str = "",
    max_retries: int = 2,
) -> dict:
    """Use refresh_token to get access_token via Anything's refresh endpoint."""
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            async with _http(proxy_url) as client:
                effective_cookie_header = cookie_header or _build_anything_cookie_header(
                    refresh_token=refresh_token,
                    access_token=access_token,
                )
                resp = await client.post(
                    f"{ANYTHING_BASE}/api/refresh_token",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": ANYTHING_BASE,
                        "Referer": f"{ANYTHING_BASE}/",
                        "Cookie": effective_cookie_header,
                        "User-Agent": USER_AGENT,
                    },
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"Token 刷新失败: HTTP {resp.status_code}")

                data = resp.json()
                access_token = data.get("accessToken") or data.get("access_token")
                if not access_token:
                    raise RuntimeError(f"Token 刷新响应中无 accessToken: {data}")

                cookie_state = _extract_anything_cookie_state(resp, client.cookies.jar)
                new_refresh = cookie_state["refresh_token"] or refresh_token

                return {
                    "access_token": access_token,
                    "refresh_token": new_refresh,
                    "cookie_header": cookie_state["cookie_header"] or effective_cookie_header,
                }
        except Exception as e:
            last_error = e
            logger.warning(f"Token 刷新失败 (尝试 {attempt}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(1)

    raise RuntimeError(f"Token 刷新失败 (已重试 {max_retries} 次): {last_error}")


async def login_and_add_account(
    outlook_id: int,
    email: str,
    ms_refresh_token: str,
    client_id: str,
    proxy_url: str | None = None,
    progress_callback=None,
    reload_account_pool: bool = True,
) -> dict:
    """Full auto-login flow for an Outlook account.

    Steps:
    1. Request Anything to send Magic Link
    2. Poll Outlook for the email
    3. Open the link, capture cookies
    4. Get access_token
    5. Get user info + project groups
    6. Save to DB
    """
    now = datetime.now(timezone.utc).isoformat()

    try:
        # Step 1: Request magic link
        if progress_callback:
            await progress_callback("sending_magic_link")
        logger.info(f"[{email}] Step 1/6: 请求发送 Magic Link...")
        signup_result = await request_magic_link(email, proxy_url)
        logger.info(f"[{email}] Step 1/6: Magic Link 请求成功, result keys: {list(signup_result.keys())}")
        logger.info(f"[{email}] accessToken={bool(signup_result.get('accessToken'))}, "
                    f"projectGroup={signup_result.get('projectGroup')}, "
                    f"typename={signup_result.get('__typename')}")

        access_token = signup_result.get("accessToken") or ""
        refresh_token = signup_result.get("refreshToken") or ""
        project_group_id = (signup_result.get("projectGroup") or {}).get("id") or ""
        cookie_header = signup_result.get("cookieHeader") or _build_anything_cookie_header(
            refresh_token=refresh_token,
            access_token=access_token,
        )
        method = "direct_signup" if access_token and project_group_id else "magic_link"

        if access_token and project_group_id and refresh_token:
            logger.info(f"[{email}] 首次注册已直接拿到 refresh_token，先标准化刷新一次 token")
            try:
                refreshed = await get_tokens_from_refresh(
                    refresh_token,
                    proxy_url,
                    access_token=access_token,
                    cookie_header=cookie_header,
                )
                access_token = refreshed["access_token"]
                refresh_token = refreshed["refresh_token"]
                cookie_header = refreshed.get("cookie_header") or cookie_header
            except Exception as e:
                logger.warning(f"[{email}] 直接 token 刷新失败，继续使用当前 access_token: {e}")
        elif access_token and project_group_id:
            logger.info(f"[{email}] 首次注册直接拿到 accessToken/projectGroup，但缺少 refresh_token，继续轮询邮件补齐")

        if not refresh_token:
            try:
                if progress_callback:
                    await progress_callback("polling_email")
                logger.info(f"[{email}] Step 2/6: 轮询 Outlook 收件箱...")
                outlook = OutlookClient(ms_refresh_token, client_id, proxy_url, email_address=email)
                magic_link = await outlook.poll_magic_link(
                    max_attempts=30, interval=5, since=now,
                )
                logger.info(f"[{email}] Step 2/6: 收到 Magic Link 邮件")

                if progress_callback:
                    await progress_callback("opening_link")
                logger.info(f"[{email}] Step 3/6: 打开 Magic Link，优先抓取 cookies...")
                opened = await open_magic_link(magic_link, proxy_url, email=email)
                cookie_header = opened.get("cookie_header") or cookie_header
                access_token = opened.get("access_token") or access_token
                refresh_token = opened.get("refresh_token") or refresh_token

                if progress_callback:
                    await progress_callback("verifying_code")

                if refresh_token:
                    logger.info(f"[{email}] Step 4/6: 已从跳转链拿到 refresh_token，调用 refresh 接口换取 access_token")
                    refreshed = await get_tokens_from_refresh(
                        refresh_token,
                        proxy_url,
                        access_token=access_token,
                        cookie_header=cookie_header,
                    )
                    access_token = refreshed["access_token"]
                    refresh_token = refreshed["refresh_token"]
                    cookie_header = refreshed.get("cookie_header") or cookie_header
                    method = "magic_link_cookie"
                else:
                    code = opened.get("code") or _extract_magic_link_code(
                        opened.get("final_url"),
                        *opened.get("redirect_chain", []),
                        magic_link,
                    )
                    if not code:
                        raise RuntimeError(
                            f"未能从 Magic Link 提取验证码, url={opened.get('final_url', '')[:100]}"
                        )
                    if opened.get("verify_error"):
                        raise RuntimeError(opened["verify_error"])

                    logger.info(f"[{email}] Step 4/6: 跳转链未拿到 refresh_token，回退到 GraphQL 验证 code={code}")
                    verify_result = await verify_magic_link_code(email, code, proxy_url)
                    access_token = verify_result.get("accessToken") or access_token
                    refresh_token = verify_result.get("refreshToken") or refresh_token
                    cookie_header = verify_result.get("cookieHeader") or cookie_header

                    if refresh_token:
                        refreshed = await get_tokens_from_refresh(
                            refresh_token,
                            proxy_url,
                            access_token=access_token,
                            cookie_header=cookie_header,
                        )
                        access_token = refreshed["access_token"]
                        refresh_token = refreshed["refresh_token"]
                        cookie_header = refreshed.get("cookie_header") or cookie_header
                        method = "magic_link_code"
                    else:
                        logger.warning(f"[{email}] Magic Link 验证成功，但依然没有拿到 refresh_token")
                        method = "magic_link_code_no_refresh"

            except Exception as link_error:
                if not (access_token and project_group_id):
                    raise
                logger.warning(
                    f"[{email}] 补抓 refresh_token 失败，回退到首次注册直出的 access_token: {link_error}"
                )
                method = "direct_signup_fallback"

        if not access_token:
            raise RuntimeError("未能获取 Anything access_token")

        # Get user info + project groups via API
        client = AnythingClient(
            access_token=access_token,
            refresh_token=refresh_token,
            proxy_url=proxy_url,
        )
        user_info = await client.get_me()
        groups = await client.get_project_groups()

        if groups:
            project_group_id = groups[0]["id"]
        elif not project_group_id:
            raise RuntimeError("该账号没有可用的 project group")

        logger.info(f"[{email}] 用户 {user_info.get('username')}, "
                    f"project_group={project_group_id}")

        # Step 5: Save to accounts table
        if progress_callback:
            await progress_callback("saving")
        logger.info(f"[{email}] Step 5/5: 保存到数据库...")
        account_id = await execute(
            "INSERT INTO accounts (name, email, access_token, refresh_token, "
            "project_group_id, proxy_url, status, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)",
            (
                user_info.get("username") or user_info.get("email") or email,
                user_info.get("email") or email,
                access_token,
                refresh_token,
                project_group_id,
                proxy_url,
                now, now,
            ),
        )

        # Update outlook_accounts status
        await execute(
            "UPDATE outlook_accounts SET status = 'linked', linked_account_id = ?, "
            "last_error = NULL, updated_at = ? WHERE id = ?",
            (account_id, now, outlook_id),
        )

        # Reload account pool for single-account flows. Batch login can defer
        # this to the caller to avoid repeated concurrent reloads.
        if reload_account_pool:
            await account_pool.load()

        result = {
            "success": True,
            "account_id": account_id,
            "email": user_info.get("email") or email,
            "username": user_info.get("username", ""),
            "project_group_id": project_group_id,
            "method": method,
        }
        logger.info(f"[{email}] ✅ 自动登录成功! account_id={account_id}")
        return result

    except Exception as e:
        error_msg = str(e)
        logger.error(f"[{email}] ❌ 自动登录失败: {error_msg}")
        await execute(
            "UPDATE outlook_accounts SET status = 'error', last_error = ?, updated_at = ? WHERE id = ?",
            (error_msg, now, outlook_id),
        )
        return {"success": False, "error": error_msg}
