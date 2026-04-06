"""Admin authentication - simple password-based with HMAC cookie."""

import hashlib
import hmac
import logging
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

_SECRET = hashlib.sha256(f"anything-proxy-{settings.admin_password}".encode()).hexdigest()
TOKEN_MAX_AGE = 86400 * 7  # 7 days


def _make_token(timestamp: int) -> str:
    msg = f"{timestamp}:{settings.admin_password}"
    sig = hmac.new(_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{timestamp}:{sig}"


def _verify_token(token: str) -> bool:
    try:
        ts_str, sig = token.split(":", 1)
        ts = int(ts_str)
        if time.time() - ts > TOKEN_MAX_AGE:
            return False
        expected = _make_token(ts)
        return hmac.compare_digest(token, expected)
    except Exception:
        return False


def require_admin(request: Request):
    """Check admin auth from cookie or Authorization header."""
    token = request.cookies.get("admin_token")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token or not _verify_token(token):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
async def login(req: LoginRequest, response: Response):
    if req.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="密码错误")
    token = _make_token(int(time.time()))
    response.set_cookie(
        key="admin_token",
        value=token,
        max_age=TOKEN_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return {"success": True, "token": token}


@router.get("/check-auth")
async def check_auth(request: Request):
    try:
        require_admin(request)
        return {"authenticated": True}
    except HTTPException:
        return {"authenticated": False}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("admin_token")
    return {"success": True}
