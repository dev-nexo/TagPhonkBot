
from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from mutagen.id3 import (
    APIC, COMM, ID3, ID3NoHeaderError, TALB, TBPM, TCOM, TCON, TCOP,
    TDRC, TIT2, TPE1, TPE2, TPOS, TPUB, TRCK, TSRC, USLT, WXXX,
)
from mutagen.mp3 import MP3
from PIL import Image, UnidentifiedImageError

from .config import config
from .db import db
from .security import verify_telegram_init_data

router = APIRouter()
BOT_TOKEN = ""
BOT: Any = None
SESSIONS: dict[str, dict[str, Any]] = {}
SESSION_TTL = 2 * 60 * 60
MAX_COVER_BYTES = 10 * 1024 * 1024

FIELD_SPECS = {
    "title": ("TIT2", TIT2),
    "artist": ("TPE1", TPE1),
    "album": ("TALB", TALB),
    "albumartist": ("TPE2", TPE2),
    "year": ("TDRC", TDRC),
    "genre": ("TCON", TCON),
    "track": ("TRCK", TRCK),
    "disc": ("TPOS", TPOS),
    "composer": ("TCOM", TCOM),
    "bpm": ("TBPM", TBPM),
    "publisher": ("TPUB", TPUB),
    "copyright": ("TCOP", TCOP),
    "isrc": ("TSRC", TSRC),
}
ALL_FIELDS = tuple(FIELD_SPECS) + ("comment", "website", "lyrics")
Image.MAX_IMAGE_PIXELS = 40_000_000


def _auth(init_data: str | None) -> dict[str, Any]:
    return verify_telegram_init_data(init_data or "", BOT_TOKEN)


def _uid(init_data: str | None) -> int:
    return int(_auth(init_data)["id"])


def _tags(path: str | Path) -> ID3:
    try:
        return ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
        tags.save(path, v2_version=3)
        return ID3(path)


def _read(tags: ID3, field: str) -> str:
    if field == "comment":
        frames = tags.getall("COMM")
        vals = getattr(frames[0], "text", []) if frames else []
        return str(vals[0]).strip() if vals else ""
    if field == "lyrics":
        frames = tags.getall("USLT")
        return str(getattr(frames[0], "text", "")).strip() if frames else ""
    if field == "website":
        frame = tags.get("WXXX:Website")
        return str(getattr(frame, "url", "")).strip() if frame else ""
    frame_id, _ = FIELD_SPECS[field]
    frame = tags.get(frame_id)
    if not frame or not hasattr(frame, "text"):
        return ""
    return " / ".join(str(x) for x in frame.text).strip()


def _write(tags: ID3, field: str, value: Any) -> None:
    value = "" if value is None else str(value).strip()
    if len(value) > 5000:
        raise ValueError("Поле слишком длинное.")
    if field == "comment":
        tags.delall("COMM")
        if value:
            tags.add(COMM(encoding=3, lang="eng", desc="", text=[value]))
        return
    if field == "lyrics":
        tags.delall("USLT")
        if value:
            tags.add(USLT(encoding=3, lang="eng", desc="", text=value))
        return
    if field == "website":
        tags.delall("WXXX:Website")
        if value:
            tags.add(WXXX(encoding=3, desc="Website", url=value))
        return
    frame_id, cls = FIELD_SPECS[field]
    tags.delall(frame_id)
    if value:
        tags.add(cls(encoding=3, text=[value]))


def _safe_name(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s{2,}", " ", value).strip(" .-_")
    return (value or "TagPhonk")[:170]


def _meta(session: dict[str, Any]) -> dict[str, Any]:
    path = Path(session["path"])
    tags = _tags(path)
    try:
        audio = MP3(path)
        duration = round(float(audio.info.length), 2)
        bitrate = round(int(audio.info.bitrate or 0) / 1000)
    except Exception:
        duration, bitrate = 0, 0
    covers = tags.getall("APIC")
    artist, title = _read(tags, "artist"), _read(tags, "title")
    name = _safe_name(f"{artist} - {title}" if artist and title else title or path.stem) + ".mp3"
    return {
        "session_id": session["id"],
        "filename": session["original_name"],
        "download_name": name,
        "fields": {f: _read(tags, f) for f in ALL_FIELDS},
        "cover": {"exists": bool(covers)},
        "technical": {
            "duration": duration,
            "bitrate_kbps": bitrate,
            "size_bytes": path.stat().st_size,
        },
    }


def _cleanup_sessions() -> None:
    now = time.time()
    for sid, session in list(SESSIONS.items()):
        if now - session["updated_at"] > SESSION_TTL:
            shutil.rmtree(session["dir"], ignore_errors=True)
            SESSIONS.pop(sid, None)


def _session(session_id: str, user_id: int) -> dict[str, Any]:
    _cleanup_sessions()
    s = SESSIONS.get(session_id)
    if not s or s["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Сессия редактора не найдена.")
    s["updated_at"] = time.time()
    return s


def _create_session(user_id: int, source: str | Path, original_name: str) -> dict[str, Any]:
    work = Path(tempfile.mkdtemp(prefix=f"tagphonk_web_{user_id}_"))
    target = work / "track.mp3"
    shutil.copy2(source, target)
    try:
        MP3(target)
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Файл не читается как MP3.") from exc
    sid = uuid.uuid4().hex
    s = {
        "id": sid,
        "user_id": user_id,
        "dir": str(work),
        "path": str(target),
        "original_name": original_name or "track.mp3",
        "updated_at": time.time(),
        "undo": [],
    }
    SESSIONS[sid] = s
    return s


def _undo_save(s: dict[str, Any]) -> None:
    folder = Path(s["dir"]) / "undo"
    folder.mkdir(exist_ok=True)
    backup = folder / f"{time.time_ns()}.mp3"
    shutil.copy2(s["path"], backup)
    s["undo"].append(str(backup))
    while len(s["undo"]) > 8:
        Path(s["undo"].pop(0)).unlink(missing_ok=True)


def _normalize_cover(raw: bytes) -> bytes:
    if len(raw) > MAX_COVER_BYTES:
        raise HTTPException(status_code=413, detail="Обложка больше 10 МБ.")
    try:
        with Image.open(BytesIO(raw)) as image:
            image.load()
            image.thumbnail((1800, 1800))
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                bg = Image.new("RGB", rgba.size, "white")
                bg.paste(rgba, mask=rgba.getchannel("A"))
                image = bg
            else:
                image = image.convert("RGB")
            out = BytesIO()
            image.save(out, "JPEG", quality=92, optimize=True)
            return out.getvalue()
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=400, detail="Не получилось прочитать изображение.") from exc


@router.get("/app")
async def mini_app():
    return FileResponse(Path(__file__).resolve().parent / "web" / "index.html")


@router.get("/api/studio/status")
async def status():
    return {
        "ok": True,
        "name": "TagPhonk",
        "database": "postgres" if db.is_postgres else "local",
        "editor": True,
    }


@router.get("/api/studio/me")
async def me(x_telegram_init_data: str | None = Header(default=None)):
    user = _auth(x_telegram_init_data)
    await asyncio.to_thread(db.upsert_user, user)
    return {
        "user": user,
        "settings": await asyncio.to_thread(db.get_settings, int(user["id"])),
        "stats": await asyncio.to_thread(db.stats, int(user["id"])),
    }


@router.get("/api/studio/projects")
async def projects(x_telegram_init_data: str | None = Header(default=None)):
    user_id = _uid(x_telegram_init_data)
    return {"items": await asyncio.to_thread(db.recent_projects, user_id, 30)}


@router.post("/api/studio/projects/{project_id}/open")
async def open_project(project_id: int, x_telegram_init_data: str | None = Header(default=None)):
    user_id = _uid(x_telegram_init_data)
    project = await asyncio.to_thread(db.get_project, user_id, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Проект не найден.")
    file_id = str(project.get("telegram_file_id") or "")
    if not file_id or BOT is None:
        raise HTTPException(status_code=409, detail="Этот проект нельзя переоткрыть. Загрузи MP3 заново.")
    temp = Path(tempfile.mkdtemp(prefix="tagphonk_reopen_")) / "source.mp3"
    try:
        tg_file = await BOT.get_file(file_id)
        await tg_file.download_to_drive(temp)
        return _meta(_create_session(user_id, temp, project.get("filename") or "track.mp3"))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=409, detail="Telegram больше не дал скачать этот файл.") from exc
    finally:
        shutil.rmtree(temp.parent, ignore_errors=True)


@router.post("/api/studio/editor/open")
async def editor_open(
    file: UploadFile = File(...),
    x_telegram_init_data: str | None = Header(default=None),
):
    user_id = _uid(x_telegram_init_data)
    name = file.filename or "track.mp3"
    if not name.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Нужен MP3-файл.")
    temp_dir = Path(tempfile.mkdtemp(prefix="tagphonk_upload_"))
    temp = temp_dir / "upload.mp3"
    total = 0
    try:
        with temp.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > config.max_file_bytes:
                    raise HTTPException(status_code=413, detail="Максимальный размер MP3 — 20 МБ.")
                out.write(chunk)
        data = _meta(_create_session(user_id, temp, name))
        await asyncio.to_thread(db.event, "miniapp_open", user_id, {"filename": name, "size": total})
        return data
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.get("/api/studio/editor/{session_id}")
async def editor_state(session_id: str, x_telegram_init_data: str | None = Header(default=None)):
    return _meta(_session(session_id, _uid(x_telegram_init_data)))


@router.patch("/api/studio/editor/{session_id}")
async def editor_save(
    session_id: str,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
):
    user_id = _uid(x_telegram_init_data)
    s = _session(session_id, user_id)
    payload = await request.json()
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    changes = {k: v for k, v in fields.items() if k in ALL_FIELDS}
    if changes:
        _undo_save(s)
        tags = _tags(s["path"])
        for field, value in changes.items():
            _write(tags, field, value)
        tags.save(s["path"], v2_version=3)
        await asyncio.to_thread(db.event, "miniapp_save", user_id, {"fields": list(changes)})
    return _meta(s)


@router.post("/api/studio/editor/{session_id}/undo")
async def editor_undo(session_id: str, x_telegram_init_data: str | None = Header(default=None)):
    s = _session(session_id, _uid(x_telegram_init_data))
    while s["undo"]:
        backup = Path(s["undo"].pop())
        if backup.exists():
            shutil.copy2(backup, s["path"])
            backup.unlink(missing_ok=True)
            return _meta(s)
    raise HTTPException(status_code=409, detail="История отмены пустая.")


@router.post("/api/studio/editor/{session_id}/cover")
async def editor_cover(
    session_id: str,
    file: UploadFile = File(...),
    x_telegram_init_data: str | None = Header(default=None),
):
    s = _session(session_id, _uid(x_telegram_init_data))
    raw = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        raw.extend(chunk)
        if len(raw) > MAX_COVER_BYTES:
            raise HTTPException(status_code=413, detail="Обложка больше 10 МБ.")
    data = _normalize_cover(bytes(raw))
    _undo_save(s)
    tags = _tags(s["path"])
    tags.delall("APIC")
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=data))
    tags.save(s["path"], v2_version=3)
    return _meta(s)


@router.delete("/api/studio/editor/{session_id}/cover")
async def editor_cover_delete(session_id: str, x_telegram_init_data: str | None = Header(default=None)):
    s = _session(session_id, _uid(x_telegram_init_data))
    tags = _tags(s["path"])
    if tags.getall("APIC"):
        _undo_save(s)
        tags.delall("APIC")
        tags.save(s["path"], v2_version=3)
    return _meta(s)


@router.get("/api/studio/editor/{session_id}/cover")
async def editor_cover_get(session_id: str, x_telegram_init_data: str | None = Header(default=None)):
    s = _session(session_id, _uid(x_telegram_init_data))
    covers = _tags(s["path"]).getall("APIC")
    if not covers:
        raise HTTPException(status_code=404, detail="Обложки нет.")
    return Response(
        content=covers[0].data,
        media_type=covers[0].mime or "image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/studio/editor/{session_id}/download")
async def editor_download(session_id: str, x_telegram_init_data: str | None = Header(default=None)):
    s = _session(session_id, _uid(x_telegram_init_data))
    meta = _meta(s)
    return FileResponse(s["path"], media_type="audio/mpeg", filename=meta["download_name"])


@router.delete("/api/studio/editor/{session_id}")
async def editor_close(session_id: str, x_telegram_init_data: str | None = Header(default=None)):
    s = _session(session_id, _uid(x_telegram_init_data))
    SESSIONS.pop(session_id, None)
    shutil.rmtree(s["dir"], ignore_errors=True)
    return {"ok": True}


@router.post("/api/studio/settings")
async def save_settings(request: Request, x_telegram_init_data: str | None = Header(default=None)):
    user = _auth(x_telegram_init_data)
    payload = await request.json()
    allowed = {"filename_template", "default_genre", "remove_comments", "normalize_cover", "language"}
    safe = {k: payload[k] for k in payload if k in allowed}
    await asyncio.to_thread(db.set_settings, int(user["id"]), safe)
    return {"ok": True, "settings": safe}


@router.get("/api/studio/admin/stats")
async def admin_stats(x_admin_key: str | None = Header(default=None)):
    if not config.admin_key or x_admin_key != config.admin_key:
        raise HTTPException(status_code=403, detail="forbidden")
    return await asyncio.to_thread(db.stats, None)


def install_web(app: FastAPI, bot_token: str, bot: Any) -> None:
    global BOT_TOKEN, BOT
    BOT_TOKEN = bot_token
    BOT = bot
    static_dir = Path(__file__).resolve().parent / "web"
    app.mount("/studio-static", StaticFiles(directory=static_dir), name="studio-static")
    app.include_router(router)
