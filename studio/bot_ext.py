
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mutagen.mp3 import MP3
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .audio_tools import acoustid_lookup, analyze, convert, preview_30, transform_mp3, waveform
from .config import config
from .db import db
from .security import SlidingWindowRateLimiter, safe_extract_mp3_zip
from .tags_extra import (
    apply_builtin_preset,
    apply_name_cleanup,
    export_csv_bytes,
    export_json_bytes,
    snapshot,
)

logger = logging.getLogger("tagphonk.studio.bot")
helpers: SimpleNamespace | None = None
PUBLIC_URL = ""
callback_limiter = SlidingWindowRateLimiter(limit=20, window_seconds=30)
upload_limiter = SlidingWindowRateLimiter(limit=8, window_seconds=60)


def studio_keyboard() -> InlineKeyboardMarkup:
    rows = []
    if PUBLIC_URL:
        rows.append(
            [InlineKeyboardButton("🖥 Открыть Studio", web_app=WebAppInfo(url=f"{PUBLIC_URL}/app"))]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton("📊 Анализ", callback_data="studio:analysis"),
                InlineKeyboardButton("📈 Waveform", callback_data="studio:waveform"),
            ],
            [
                InlineKeyboardButton("🐢 Slowed", callback_data="studio:fx:slowed90"),
                InlineKeyboardButton("⚡ Sped Up", callback_data="studio:fx:sped115"),
            ],
            [
                InlineKeyboardButton("🔊 Bass Boost", callback_data="studio:fx:bass"),
                InlineKeyboardButton("🔥 Phonk FX", callback_data="studio:fx:phonk"),
            ],
            [
                InlineKeyboardButton("🎚 Normalize", callback_data="studio:fx:normalize"),
                InlineKeyboardButton("🧠 Распознать", callback_data="studio:fingerprint"),
            ],
            [InlineKeyboardButton("⬅️ К тегам", callback_data="nav:main")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def _private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")


async def _bot_link(context: ContextTypes.DEFAULT_TYPE) -> str:
    username = context.bot.username
    if not username:
        me = await context.bot.get_me()
        username = me.username
    return f"https://t.me/{username}?start=studio"


async def group_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _private(update):
        return
    link = await _bot_link(context)
    await update.effective_message.reply_text(
        "🎵 <b>TagPhonk</b> работает с MP3 в личке, чтобы не превращать группу в панель самолёта.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎛 Открыть TagPhonk", url=link)]]
        ),
    )
    raise ApplicationHandlerStop


async def group_file_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _private(update):
        return

    message = update.effective_message
    is_mp3 = bool(message.audio)
    if message.document:
        name = (message.document.file_name or "").lower()
        mime = (message.document.mime_type or "").lower()
        is_mp3 = is_mp3 or name.endswith(".mp3") or mime in {"audio/mpeg", "audio/mp3"}

    if not is_mp3:
        return

    link = await _bot_link(context)
    await message.reply_text(
        "🎧 MP3 увидел. Редактирование делаю в личке.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Открыть редактор", url=link)]]
        ),
    )
    raise ApplicationHandlerStop


async def studio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _private(update):
        return
    path = helpers.current_file(context) if helpers else None
    text = (
        "🎛 <b>TagPhonk Studio</b>\n\n"
        "Аудио-анализ, waveform, Slowed/Sped Up, Bass Boost, Reverb, Normalize, "
        "конвертация, пресеты, экспорт метаданных и fingerprint lookup."
    )
    if not path:
        text += "\n\nСначала пришли MP3."
    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=studio_keyboard(),
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in config.admin_user_ids:
        await update.effective_message.reply_text("Админ-панель для этого аккаунта не настроена.")
        return
    stats = await asyncio.to_thread(db.stats, None)
    await update.effective_message.reply_text(
        "🛠 <b>TagPhonk Admin</b>\n\n"
        f"Users: <b>{stats['users']}</b>\n"
        f"Projects: <b>{stats['projects']}</b>\n"
        f"Events: <b>{stats['events']}</b>\n"
        f"Processed: <b>{stats['bytes_processed'] / 1024 / 1024:.1f} MB</b>",
        parse_mode="HTML",
    )


async def zip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _private(update):
        return
    doc = update.effective_message.document
    if not doc:
        return
    name = doc.file_name or ""
    if not name.lower().endswith(".zip"):
        return

    user_id = update.effective_user.id
    if not upload_limiter.allow(user_id):
        await update.effective_message.reply_text("Слишком много загрузок подряд. Подожди немного.")
        raise ApplicationHandlerStop

    if doc.file_size and doc.file_size > config.max_file_bytes:
        await update.effective_message.reply_text("ZIP слишком большой для Bot API.")
        raise ApplicationHandlerStop

    batch_dir = tempfile.mkdtemp(prefix=f"tagphonk_zip_{user_id}_")
    zip_path = Path(batch_dir) / "input.zip"
    tg_file = await context.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(zip_path)

    try:
        mp3s = safe_extract_mp3_zip(
            zip_path,
            Path(batch_dir) / "files",
            config.max_batch_files,
            config.max_batch_unpacked,
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Не распаковал ZIP: {html.escape(str(exc))}")
        raise ApplicationHandlerStop

    valid: list[str] = []
    names: list[str] = []
    total = 0
    for item in mp3s:
        try:
            MP3(item)
        except Exception:
            continue
        valid.append(str(item))
        names.append(item.name)
        total += item.stat().st_size

    if not valid:
        await update.effective_message.reply_text("В ZIP нет читаемых MP3.")
        raise ApplicationHandlerStop

    old = context.user_data.get("batch_dir")
    if old and Path(old).exists():
        import shutil
        shutil.rmtree(old, ignore_errors=True)

    context.user_data["batch_dir"] = batch_dir
    context.user_data["batch_paths"] = valid
    context.user_data["batch_names"] = names
    context.user_data["batch_total_bytes"] = total
    context.user_data["batch_collecting"] = False
    context.user_data["awaiting"] = None

    await asyncio.to_thread(
        db.event,
        "zip_batch",
        user_id,
        {"files": len(valid), "bytes": total},
    )

    await update.effective_message.reply_text(
        f"📦 ZIP принят: <b>{len(valid)}</b> MP3, {total/1024/1024:.1f} MB.\n"
        "Пакет уже собран.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⚙️ Открыть пакет", callback_data="batch:finish")]]
        ),
    )
    raise ApplicationHandlerStop


async def observe_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _private(update) or not helpers:
        return
    path = helpers.current_file(context)
    if not path or not Path(path).exists():
        return

    message = update.effective_message
    file_id = ""
    if message.audio:
        file_id = message.audio.file_id
    elif message.document:
        name = (message.document.file_name or "").lower()
        if name.endswith(".mp3"):
            file_id = message.document.file_id

    if not file_id:
        return

    try:
        audio = MP3(path)
        duration = int(audio.info.length)
    except Exception:
        duration = 0

    snap = snapshot(path)
    payload = {
        "user_id": update.effective_user.id,
        "telegram_file_id": file_id,
        "filename": context.user_data.get("original_name") or Path(path).name,
        "title": snap.get("title", ""),
        "artist": snap.get("artist", ""),
        "album": snap.get("album", ""),
        "duration_seconds": duration,
        "size_bytes": Path(path).stat().st_size,
        "snapshot": snap,
    }
    user = {
        "id": update.effective_user.id,
        "username": update.effective_user.username or "",
        "first_name": update.effective_user.first_name or "",
        "language_code": update.effective_user.language_code or "",
    }
    await asyncio.to_thread(db.upsert_user, user)
    await asyncio.to_thread(db.add_project, payload)
    await asyncio.to_thread(db.event, "upload", update.effective_user.id, payload)


async def _refresh_card(query, context, note: str) -> None:
    if not helpers:
        return
    path = helpers.current_file(context)
    if path:
        await helpers.send_track_card(query.message, path, context, note)


async def on_studio_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data.startswith("studio:"):
        return

    user_id = update.effective_user.id
    if not callback_limiter.allow(user_id):
        await query.answer("Слишком быстро.", show_alert=False)
        raise ApplicationHandlerStop

    await query.answer()
    path = helpers.current_file(context) if helpers else None

    if query.data == "studio:menu":
        await query.message.reply_text("🎛 TagPhonk Studio", reply_markup=studio_keyboard())
        raise ApplicationHandlerStop

    if not path:
        await query.message.reply_text("Сначала пришли MP3.")
        raise ApplicationHandlerStop

    data = query.data

    try:
        if data == "studio:analysis":
            result = await analyze(path)
            notes = ", ".join(result["notes"]) if result["notes"] else "критичных замечаний нет"
            await query.message.reply_text(
                "📊 <b>Audio Analysis</b>\n\n"
                f"Codec: <b>{html.escape(str(result['codec']))}</b>\n"
                f"Duration: <b>{result['duration']} s</b>\n"
                f"Bitrate: <b>{result['bitrate_kbps']} kbps</b>\n"
                f"Sample rate: <b>{result['sample_rate']} Hz</b>\n"
                f"Channels: <b>{result['channels']}</b>\n"
                f"Mean volume: <b>{result['mean_volume_db']} dB</b>\n"
                f"Max volume: <b>{result['max_volume_db']} dB</b>\n"
                f"Quality Score*: <b>{result['quality_score']}/100</b>\n"
                f"Notes: {html.escape(notes)}\n\n"
                f"<i>{html.escape(result['disclaimer'])}</i>",
                parse_mode="HTML",
            )
            await asyncio.to_thread(db.event, "analysis", user_id, result)

        elif data == "studio:waveform":
            out = str(Path(context.user_data["work_dir"]) / "waveform.png")
            await waveform(path, out)
            await query.message.reply_photo(
                photo=Path(out),
                caption="📈 Waveform",
            )
            await asyncio.to_thread(db.event, "waveform", user_id, {})

        elif data.startswith("studio:fx:"):
            preset = data.split(":", 2)[2]
            helpers.save_undo(context, path)
            out = str(Path(context.user_data["work_dir"]) / f"audio_{preset}.mp3")
            await query.message.reply_text("🎛 Обрабатываю аудио…")
            await transform_mp3(path, out, preset)
            context.user_data["mp3_path"] = out
            await _refresh_card(query, context, f"✅ Audio FX: {preset}. Аудио было перекодировано.")
            await asyncio.to_thread(db.event, "audio_fx", user_id, {"preset": preset})

        elif data == "studio:preview":
            out = str(Path(context.user_data["work_dir"]) / "preview_30s.mp3")
            await preview_30(path, out)
            await query.message.reply_audio(audio=Path(out), caption="✂️ 30-second preview")

        elif data.startswith("studio:convert:"):
            fmt = data.rsplit(":", 1)[1]
            out = str(Path(context.user_data["work_dir"]) / f"converted.{fmt}")
            await query.message.reply_text(f"🔄 Конвертирую в {fmt.upper()}…")
            await convert(path, out, fmt)
            await query.message.reply_document(
                document=Path(out),
                filename=f"TagPhonk-converted.{fmt}",
                caption=f"✅ {fmt.upper()} готов.",
            )
            await asyncio.to_thread(db.event, "convert", user_id, {"format": fmt})

        elif data == "studio:cleanname":
            helpers.save_undo(context, path)
            result = apply_name_cleanup(path, context.user_data.get("original_name"))
            await _refresh_card(query, context, f"🧼 Нормализовал имя/теги. Изменений: {len(result['changes'])}.")

        elif data.startswith("studio:preset:"):
            preset = data.rsplit(":", 1)[1]
            helpers.save_undo(context, path)
            result = apply_builtin_preset(path, preset)
            await _refresh_card(query, context, f"✅ Preset {preset}: {len(result['changes'])} изменений.")
            await asyncio.to_thread(db.event, "preset", user_id, {"preset": preset})

        elif data == "studio:export:json":
            blob = BytesIO(export_json_bytes(path))
            blob.name = "metadata.json"
            await query.message.reply_document(document=blob, filename="metadata.json")

        elif data == "studio:export:csv":
            row = snapshot(path)
            row["filename"] = context.user_data.get("original_name") or Path(path).name
            blob = BytesIO(export_csv_bytes([row]))
            blob.name = "metadata.csv"
            await query.message.reply_document(document=blob, filename="metadata.csv")

        elif data == "studio:fingerprint":
            await query.message.reply_text("🧠 Считаю audio fingerprint…")
            result = await acoustid_lookup(path)
            if not result.get("enabled"):
                await query.message.reply_text(
                    "Fingerprint-модуль установлен, но сейчас недоступен:\n"
                    f"{html.escape(result.get('reason', 'unknown'))}"
                )
            else:
                payload = result.get("result") or {}
                results = payload.get("results") or []
                if not results:
                    await query.message.reply_text("AcoustID совпадений не нашёл.")
                else:
                    lines = ["🧠 <b>AcoustID matches</b>"]
                    for item in results[:5]:
                        score = item.get("score")
                        recordings = item.get("recordings") or []
                        if recordings:
                            rec = recordings[0]
                            title = rec.get("title") or "?"
                            artists = ", ".join(
                                a.get("name", "?")
                                for a in (rec.get("artists") or [])
                                if isinstance(a, dict)
                            )
                            lines.append(
                                f"• <b>{html.escape(artists)} — {html.escape(title)}</b> · {score}"
                            )
                    await query.message.reply_text("\n".join(lines), parse_mode="HTML")
            await asyncio.to_thread(db.event, "fingerprint", user_id, {"enabled": result.get("enabled")})

        elif data == "studio:export:batch":
            paths = context.user_data.get("batch_paths") or []
            if not paths:
                await query.message.reply_text("Пакет пуст.")
            else:
                rows = []
                for item in paths:
                    row = snapshot(item)
                    row["filename"] = Path(item).name
                    rows.append(row)
                blob = BytesIO(export_csv_bytes(rows))
                blob.name = "batch_metadata.csv"
                await query.message.reply_document(document=blob, filename="batch_metadata.csv")

        else:
            await query.message.reply_text("Studio action пока не распознана.")

    except Exception as exc:
        logger.exception("Studio action failed: %s", data)
        await query.message.reply_text(
            "⚠️ Studio-операция не выполнилась.\n"
            f"<code>{html.escape(str(exc)[:600])}</code>",
            parse_mode="HTML",
        )

    raise ApplicationHandlerStop


async def startup(bot_app: Application, public_url: str) -> None:
    if public_url:
        try:
            await bot_app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Studio",
                    web_app=WebAppInfo(url=f"{public_url}/app"),
                )
            )
        except Exception:
            logger.exception("Could not set Mini App menu button.")

    try:
        await asyncio.to_thread(db.init)
    except Exception:
        logger.exception("Studio DB initialization failed.")

    asyncio.create_task(__import__("studio.audio_tools", fromlist=["warm_ffmpeg"]).warm_ffmpeg())


def install(bot_app: Application, passed_helpers: dict[str, Any], public_url: str) -> None:
    global helpers, PUBLIC_URL
    helpers = SimpleNamespace(**passed_helpers)
    PUBLIC_URL = public_url.rstrip("/")

    bot_app.add_handler(CommandHandler("start", group_start), group=-2)
    bot_app.add_handler(CommandHandler("studio", studio_command), group=-2)
    bot_app.add_handler(CommandHandler("admin", admin_command), group=-2)

    bot_app.add_handler(
        MessageHandler(filters.AUDIO | filters.Document.ALL, group_file_guard),
        group=-3,
    )
    bot_app.add_handler(
        MessageHandler(filters.Document.ALL, zip_handler),
        group=-2,
    )
    bot_app.add_handler(
        CallbackQueryHandler(on_studio_button, pattern=r"^studio:"),
        group=-2,
    )

    bot_app.add_handler(
        MessageHandler(filters.AUDIO | filters.Document.ALL, observe_upload),
        group=2,
    )
