"""PWA plumbing: web manifest and service worker (both public)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from app import APP_VERSION

router = APIRouter(tags=["pwa"])
VERSION_PLACEHOLDER = "__APP_VERSION__"

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
THEME = "#0b0f14"

MANIFEST = {
    "name": "Edge Finder",
    "short_name": "Edges",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": THEME,
    "theme_color": THEME,
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {
            "src": "/static/icons/icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable",
        },
    ],
}


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> JSONResponse:
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    # The cache name and precache URLs carry the app version so a deploy invalidates them.
    body = (
        (STATIC_DIR / "sw.js").read_text(encoding="utf-8").replace(VERSION_PLACEHOLDER, APP_VERSION)
    )
    return Response(
        content=body,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )
