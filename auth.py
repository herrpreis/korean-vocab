"""Household passcode.

Scope is deliberately narrow: it locks the two pages a stranger could
stumble onto and do damage from — the home page (browse and build a week)
and Maintenance (delete recipes, restore backups, edit units). It does not
lock the API or the /weeks/{id}/... pages.

Those stay open on purpose. Cowork browser sessions hit this domain
directly — filling a basket, posting new recipes, mirroring images — with
no way to type a passcode into a fetch() call. Week pages already have
unguessable ids, which was the original protection for anything reachable
by a session that can't log in. Locking the API behind this cookie would
break every Cowork workflow built so far, so it doesn't.

One shared code, not per-person accounts — this is a two-person kitchen
tool, not a multi-tenant app.
"""

import hashlib
import hmac
import os
import time

from fastapi import Request
from fastapi.responses import RedirectResponse

PASSCODE = os.environ.get("APP_PASSCODE", "")
SECRET = os.environ.get("APP_SECRET", "")  # signs the cookie; set in Railway
COOKIE_NAME = "mp_session"
COOKIE_MAX_AGE = 180 * 24 * 60 * 60  # 180 days — a kitchen tool, not a bank

# Only these need the passcode. Everything else — the API, /weeks/{id}/...,
# /img/... — stays reachable without login; see the module docstring.
PROTECTED_PATHS = {"/", "/admin", "/admin/conversions", "/weeks",
                    "/recipes/archived", "/ingredients/browse"}


def configured():
    return bool(PASSCODE and SECRET)


def _sign(value: str) -> str:
    return hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def make_cookie_value() -> str:
    issued = str(int(time.time()))
    return f"{issued}.{_sign(issued)}"


def cookie_valid(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    issued, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(issued)):
        return False
    try:
        age = time.time() - int(issued)
    except ValueError:
        return False
    return 0 <= age <= COOKIE_MAX_AGE


async def require_login(request: Request, call_next):
    """Middleware: redirect to /login unless the cookie checks out.

    Only fires for PROTECTED_PATHS. A no-op entirely when APP_PASSCODE or
    APP_SECRET isn't set, so forgetting to configure it leaves the app open
    rather than locking everyone out.
    """
    if not configured() or request.url.path not in PROTECTED_PATHS:
        return await call_next(request)

    if cookie_valid(request.cookies.get(COOKIE_NAME)):
        return await call_next(request)

    nxt = request.url.path
    if request.url.query:
        nxt += f"?{request.url.query}"
    return RedirectResponse(f"/login?next={nxt}")
