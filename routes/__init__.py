from fastapi import APIRouter

from .proxy import router as proxy_router
from .admin_auth import router as admin_auth_router
from .admin_accounts import router as admin_accounts_router
from .admin_login_flow import router as admin_login_flow_router
from .admin_usage import router as admin_usage_router
from .admin_pages import router as admin_pages_router

api_router = APIRouter()

# Proxy routes (no prefix)
api_router.include_router(proxy_router)

# Admin API
api_router.include_router(admin_auth_router, prefix="/admin")
api_router.include_router(admin_accounts_router, prefix="/admin/api")
api_router.include_router(admin_login_flow_router, prefix="/admin/api")
api_router.include_router(admin_usage_router, prefix="/admin/api")

# Admin pages (must be last - catch-all routes)
api_router.include_router(admin_pages_router, prefix="/admin")
