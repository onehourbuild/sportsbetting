"""Bet ledger. PLACEHOLDER — replaced by the UI implementer."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templating import templates

router = APIRouter(tags=["bets"])


@router.get("/bets", response_class=HTMLResponse)
async def bets_ledger(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "bets.html", {"open_bets": [], "settled_bets": [], "summary": None}
    )
