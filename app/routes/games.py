"""Game detail. PLACEHOLDER — replaced by the UI implementer."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templating import templates

router = APIRouter(tags=["games"])


@router.get("/games/{game_id}", response_class=HTMLResponse)
async def game_detail(request: Request, game_id: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "game.html", {"game": None, "game_id": game_id, "markets": []}
    )
