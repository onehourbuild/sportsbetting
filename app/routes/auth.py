"""Password gate: GET/POST /login, POST /logout."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.templating import templates

router = APIRouter(tags=["auth"])


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or value == "/login":
        return "/"
    return value


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/") -> HTMLResponse:
    from app.main import is_authenticated

    if is_authenticated(request):
        return RedirectResponse(url=_safe_next(next), status_code=303)  # type: ignore[return-value]
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "next": _safe_next(next)}
    )


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form("/"),
):
    from app.main import SESSION_COOKIE, SESSION_MAX_AGE_S, sign_session

    settings = request.app.state.settings
    expected = settings.app_password
    ok = bool(expected) and hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8"))
    if not ok and not settings.auth_enabled:
        ok = True  # dev with no password: nothing to check
    if not ok:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Wrong password.", "next": _safe_next(next)},
            status_code=401,
        )
    response = RedirectResponse(url=_safe_next(next), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(settings),
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    from app.main import SESSION_COOKIE

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
