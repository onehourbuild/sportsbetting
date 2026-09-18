"""Preferences. PLACEHOLDER — replaced by the UI implementer."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.services.prefs import get_prefs
from app.templating import templates

router = APIRouter(tags=["settings"])


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    prefs = get_prefs(session)
    return templates.TemplateResponse(request, "settings.html", {"prefs": prefs, "error": None})
