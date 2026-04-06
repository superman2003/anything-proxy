"""Admin page rendering routes."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from routes.admin_auth import _verify_token

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    token = request.cookies.get("admin_token")
    if not token or not _verify_token(token):
        return RedirectResponse(url="/admin/login")
    return templates.TemplateResponse(request=request, name="accounts.html")
