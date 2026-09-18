"""Diagnostics. PLACEHOLDER — replaced by the UI implementer."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templating import templates

router = APIRouter(tags=["diagnostics"])


@router.get("/diagnostics", response_class=HTMLResponse)
async def diagnostics_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "diagnostics.html", {"scans": [], "unmatched": [], "unparseable": []}
    )
