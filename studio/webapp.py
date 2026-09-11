
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import config
from .db import db
from .security import verify_telegram_init_data

router = APIRouter()
BOT_TOKEN = ""


def _auth(init_data: str | None) -> dict[str, Any]:
    return verify_telegram_init_data(init_data or "", BOT_TOKEN)


@router.get("/app")
async def mini_app():
    return FileResponse(Path(__file__).resolve().parent / "web" / "index.html")


@router.get("/api/studio/status")
async def status():
    return {
        "ok": True,
        "name": "TagPhonk Studio",
        "version": "4.0.0",
        "features": [
            "projects", "presets", "history", "audio-analysis", "waveform",
            "slowed", "sped-up", "bass-boost", "reverb", "normalize",
            "zip-batch", "metadata-export", "fingerprint-ready",
        ],
        "acoustid_configured": bool(config.acoustid_api_key),
        "database": "postgres" if db.is_postgres else "sqlite-fallback",
    }


@router.get("/api/studio/me")
async def me(x_telegram_init_data: str | None = Header(default=None)):
    user = _auth(x_telegram_init_data)
    await asyncio.to_thread(db.upsert_user, user)
    settings = await asyncio.to_thread(db.get_settings, int(user["id"]))
    stats = await asyncio.to_thread(db.stats, int(user["id"]))
    return {
        "user": user,
        "settings": settings,
        "stats": stats,
        "is_admin": int(user["id"]) in config.admin_user_ids,
    }


@router.get("/api/studio/projects")
async def projects(x_telegram_init_data: str | None = Header(default=None)):
    user = _auth(x_telegram_init_data)
    rows = await asyncio.to_thread(db.recent_projects, int(user["id"]), 30)
    return {"items": rows}


@router.get("/api/studio/presets")
async def presets(x_telegram_init_data: str | None = Header(default=None)):
    user = _auth(x_telegram_init_data)
    custom = await asyncio.to_thread(db.presets, int(user["id"]))
    builtin = [
        {"id": "builtin-clean", "name": "Clean Release", "preset": {"kind": "clean"}},
        {"id": "builtin-phonk", "name": "Phonk Release", "preset": {"kind": "phonk"}},
        {"id": "builtin-dj", "name": "DJ Library", "preset": {"kind": "dj"}},
        {"id": "builtin-minimal", "name": "Minimal", "preset": {"kind": "minimal"}},
    ]
    return {"builtin": builtin, "custom": custom}


@router.post("/api/studio/settings")
async def save_settings(
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
):
    user = _auth(x_telegram_init_data)
    payload = await request.json()
    allowed = {
        "filename_template",
        "default_genre",
        "remove_comments",
        "normalize_cover",
        "language",
    }
    safe = {k: payload[k] for k in payload if k in allowed}
    await asyncio.to_thread(db.set_settings, int(user["id"]), safe)
    return {"ok": True, "settings": safe}


@router.post("/api/studio/presets")
async def save_preset(
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
):
    user = _auth(x_telegram_init_data)
    payload = await request.json()
    name = str(payload.get("name") or "My preset").strip()[:80]
    preset = payload.get("preset") if isinstance(payload.get("preset"), dict) else {}
    await asyncio.to_thread(db.save_preset, int(user["id"]), name, preset)
    return {"ok": True}


@router.get("/api/studio/admin/stats")
async def admin_stats(x_admin_key: str | None = Header(default=None)):
    if not config.admin_key or x_admin_key != config.admin_key:
        raise HTTPException(status_code=403, detail="forbidden")
    return await asyncio.to_thread(db.stats, None)


def install_web(app: FastAPI, bot_token: str) -> None:
    global BOT_TOKEN
    BOT_TOKEN = bot_token
    static_dir = Path(__file__).resolve().parent / "web"
    app.mount("/studio-static", StaticFiles(directory=static_dir), name="studio-static")
    app.include_router(router)
