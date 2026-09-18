"""Home: edges list. PLACEHOLDER — replaced by the UI implementer."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templating import templates

router = APIRouter(tags=["edges"])


@router.get("/", response_class=HTMLResponse)
async def edges_home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "edges.html", {"opportunities": [], "scan": None})
