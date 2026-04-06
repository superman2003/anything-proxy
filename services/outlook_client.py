"""Outlook email reader via IMAP + OAuth2.

Uses a pre-existing refresh_token to authenticate via IMAP XOAUTH2,
then searches for Magic Login Link emails.
Credential format: email----password----client_id----ms_refresh_token
"""

import asyncio
import base64
import email as email_lib
import imaplib
import logging
import re
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

IMAP_HOST = "outlook.office365.com"
IMAP_PORT = 993


# ─── HTML link extraction (from anything_register.py) ──────────────────


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[dict] = []
        self._href: Optional[str] = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href", "")
            self._text = []

    def handle_data(self, data):
        if self._href is not None and data:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            text = " ".join(p.strip() for p in self._text if p.strip())
            self.links.append({"href": self._href, "text": text})
            self._href = None


def extract_magic_link(html: str) -> Optional[str]:
    """Extract the Magic Login Link URL from email HTML."""
    parser = LinkParser()
    parser.feed(html or "")

    keywords = ["sign in", "magic login", "log in", "login"]
    for link in parser.links:
        href = (link["href"] or "").strip()
        text = (link["text"] or "").lower()
        if href and any(k in text for k in keywords):
            return href

    for link in parser.links:
        href = (link["href"] or "").strip()
        if href and "anything.com/ls/click" in href:
            return href

    m = re.search(r"https?://[^\s\"'<>]+", html or "")
    return m.group(0) if m else None


# ─── Credential parser ─────────────────────────────────────────────────


def parse_outlook_line(line: str) -> dict:
    """Parse: email----password----client_id----ms_refresh_token"""
    parts = line.strip().split("----")
    if len(parts) < 4:
        raise ValueError(f"格式错误: 需要 email----password----client_id----refresh_token，得到 {len(parts)} 段")
    return {
        "email": parts[0].strip(),
        "password": parts[1].strip(),
        "client_id": parts[2].strip(),
        "ms_refresh_token": parts[3].strip(),
    }


# ─── IMAP + OAuth2 client ─────────────────────────────────────────────


def _build_xoauth2_string(user: str, access_token: str) -> str:
    """Build XOAUTH2 authentication string for IMAP."""
    auth_string = f"user={user}\x01auth=Bearer {access_token}\x01\x01"
    return auth_string


def _get_email_html(msg: email_lib.message.Message) -> str:
    """Extract HTML content from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def _parse_since_datetime(since: Optional[str]) -> datetime | None:
    if not since:
        return None
    try:
        dt = datetime.fromisoformat(since)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    # Email Date / IMAP timestamps are usually second precision, while our
    # request timestamp includes microseconds. Normalize to second precision
    # so the newest message in the same second is not treated as stale.
    return dt.replace(microsecond=0)


def _get_email_received_at(msg: email_lib.message.Message) -> datetime | None:
    raw_date = msg.get("Date")
    if not raw_date:
        return None
    try:
        dt = parsedate_to_datetime(raw_date)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class OutlookClient:
    """Read Outlook emails via IMAP with OAuth2 (XOAUTH2)."""

    TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

    def __init__(self, ms_refresh_token: str, client_id: str,
                 proxy_url: str | None = None, email_address: str = ""):
        self._refresh_token = ms_refresh_token
        self._client_id = client_id
        self._access_token = ""
        self._proxy_url = proxy_url or settings.proxy_url
        self._email = email_address

    def _http(self) -> httpx.AsyncClient:
        kw = {"timeout": 30.0, "follow_redirects": True}
        if self._proxy_url:
            kw["proxy"] = self._proxy_url
        return httpx.AsyncClient(**kw)

    async def get_token(self) -> str:
        """Exchange refresh_token for an IMAP-scoped access_token."""
        async with self._http() as c:
            # Try multiple scopes - the token may have been obtained with different scope
            scopes_to_try = [
                "https://outlook.office365.com/IMAP.AccessAsUser.All offline_access",
                "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
                "https://outlook.office365.com/.default offline_access",
            ]

            last_error = None
            for scope in scopes_to_try:
                resp = await c.post(self.TOKEN_URL, data={
                    "client_id": self._client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "scope": scope,
                })
                data = resp.json()
                if resp.status_code == 200:
                    self._access_token = data["access_token"]
                    if data.get("refresh_token"):
                        self._refresh_token = data["refresh_token"]
                    logger.info(f"[{self._email}] IMAP access token 获取成功 (scope={scope})")
                    return self._access_token
                last_error = data
                logger.debug(f"[{self._email}] scope={scope} 失败: {data.get('error')}")

            error_desc = (last_error or {}).get("error_description") or ""
            raise RuntimeError(
                f"Microsoft token 交换失败 (所有 scope 均失败): {error_desc[:150]}"
            )

    async def _ensure_token(self):
        if not self._access_token:
            await self.get_token()

    def _connect_imap(self) -> imaplib.IMAP4_SSL:
        """Connect to Outlook IMAP and authenticate with XOAUTH2."""
        ctx = ssl.create_default_context()
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)
        auth_string = _build_xoauth2_string(self._email, self._access_token)
        imap.authenticate("XOAUTH2", lambda x: auth_string.encode())
        logger.info(f"[{self._email}] IMAP 连接成功")
        return imap

    def _search_magic_link_emails(self, imap: imaplib.IMAP4_SSL) -> list[str]:
        """Search INBOX for Magic Login Link emails, return message IDs."""
        imap.select("INBOX")
        # Search by subject
        status, data = imap.search(None, '(SUBJECT "Magic Login Link")')
        if status != "OK":
            return []
        msg_ids = data[0].decode().split()
        # Return newest first
        return list(reversed(msg_ids))

    def _fetch_and_extract(self, imap: imaplib.IMAP4_SSL, msg_id: str) -> Optional[dict]:
        """Fetch a single email and extract the magic link plus metadata."""
        status, data = imap.fetch(msg_id, "(RFC822)")
        if status != "OK" or not data or not data[0]:
            return None
        raw = data[0][1]
        msg = email_lib.message_from_bytes(raw)
        html = _get_email_html(msg)
        link = extract_magic_link(html)
        if not link:
            return None
        return {
            "link": link,
            "subject": msg.get("Subject", ""),
            "received_at": _get_email_received_at(msg),
        }

    def _delete_email(self, imap: imaplib.IMAP4_SSL, msg_id: str):
        """Mark email as deleted."""
        try:
            imap.store(msg_id, "+FLAGS", "\\Deleted")
            imap.expunge()
            logger.info(f"[{self._email}] 已删除已处理的邮件 #{msg_id}")
        except Exception as e:
            logger.warning(f"[{self._email}] 删除邮件失败(非致命): {e}")

    async def poll_magic_link(
        self,
        max_attempts: int = 30,
        interval: int = 5,
        since: Optional[str] = None,
        stale_fallback_after_seconds: int = 30,
    ) -> str:
        """Poll inbox for 'Magic Login Link' email via IMAP, return the link URL."""
        await self._ensure_token()
        since_dt = _parse_since_datetime(since)

        for attempt in range(1, max_attempts + 1):
            logger.info(f"[{self._email}] 轮询 IMAP 邮件 {attempt}/{max_attempts}...")
            try:
                # Run IMAP operations in a thread to avoid blocking
                allow_stale = stale_fallback_after_seconds >= 0 and attempt * interval >= stale_fallback_after_seconds
                link = await asyncio.to_thread(self._poll_once, since_dt, allow_stale)
                if link:
                    return link
            except imaplib.IMAP4.error as e:
                err_str = str(e)
                if "AUTHENTICATE" in err_str.upper():
                    # Token might be expired, refresh it
                    logger.warning(f"[{self._email}] IMAP 认证失败，尝试刷新 token...")
                    await self.get_token()
                else:
                    logger.warning(f"[{self._email}] IMAP 错误: {e}")
            except Exception as e:
                logger.warning(f"[{self._email}] 轮询出错: {e}")

            if attempt < max_attempts:
                await asyncio.sleep(interval)

        raise TimeoutError(
            f"轮询超时({max_attempts * interval}秒)，未收到 Magic Login Link 邮件。"
        )

    def _poll_once(self, since_dt: datetime | None = None, allow_stale: bool = False) -> Optional[str]:
        """Single IMAP poll attempt (runs in thread)."""
        if since_dt:
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            else:
                since_dt = since_dt.astimezone(timezone.utc)
            since_dt = since_dt.replace(microsecond=0)
        imap = self._connect_imap()
        try:
            msg_ids = self._search_magic_link_emails(imap)
            if not msg_ids:
                return None

            fallback_mail = None
            fallback_msg_id = None
            # Try newest emails first
            for msg_id in msg_ids[:5]:
                mail = self._fetch_and_extract(imap, msg_id)
                if not mail:
                    continue
                received_at = mail.get("received_at")
                if since_dt and received_at and received_at < since_dt:
                    logger.info(
                        f"[{self._email}] 跳过旧 Magic Link 邮件 #{msg_id}: "
                        f"received_at={received_at.isoformat()}, since={since_dt.isoformat()}"
                    )
                    if allow_stale and fallback_mail is None:
                        fallback_mail = mail
                        fallback_msg_id = msg_id
                    continue
                link = mail.get("link")
                if link:
                    self._delete_email(imap, msg_id)
                    logger.info(f"[{self._email}] 找到 Magic Link!")
                    return link
            if allow_stale and fallback_mail and fallback_mail.get("link"):
                self._delete_email(imap, fallback_msg_id)
                logger.info(
                    f"[{self._email}] 30 秒内未等到新邮件，回退使用最近一封旧 Magic Link 邮件 #{fallback_msg_id}"
                )
                return fallback_mail["link"]
            return None
        finally:
            try:
                imap.logout()
            except Exception:
                pass
