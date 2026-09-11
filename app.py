import html
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from mutagen.id3 import (
    APIC, COMM, ID3, ID3NoHeaderError, TALB, TCOM, TCON, TDRC,
    TIT2, TPE1, TPE2, TPOS, TRCK
)
from mutagen.mp3 import MP3
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-this-secret")

api = FastAPI(title="MP3 Tag Editor Bot")
bot_app = Application.builder().token(BOT_TOKEN).build()

TEXT_FIELDS = {
    "title": ("Название", "TIT2", TIT2),
    "artist": ("Исполнитель", "TPE1", TPE1),
    "album": ("Альбом", "TALB", TALB),
    "albumartist": ("Автор альбома", "TPE2", TPE2),
    "year": ("Год", "TDRC", TDRC),
    "genre": ("Жанр", "TCON", TCON),
    "track": ("Номер трека", "TRCK", TRCK),
    "disc": ("Номер диска", "TPOS", TPOS),
    "composer": ("Композитор", "TCOM", TCOM),
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Название", callback_data="edit:title"),
            InlineKeyboardButton("👤 Исполнитель", callback_data="edit:artist"),
        ],
        [
            InlineKeyboardButton("💿 Альбом", callback_data="edit:album"),
            InlineKeyboardButton("👥 Автор альбома", callback_data="edit:albumartist"),
        ],
        [
            InlineKeyboardButton("📅 Год", callback_data="edit:year"),
            InlineKeyboardButton("🎵 Жанр", callback_data="edit:genre"),
        ],
        [
            InlineKeyboardButton("🔢 Трек", callback_data="edit:track"),
            InlineKeyboardButton("💽 Диск", callback_data="edit:disc"),
        ],
        [
            InlineKeyboardButton("🎼 Композитор", callback_data="edit:composer"),
            InlineKeyboardButton("💬 Комментарий", callback_data="edit:comment"),
        ],
        [
            InlineKeyboardButton("🖼 Новая обложка", callback_data="cover:set"),
            InlineKeyboardButton("🗑 Удалить обложку", callback_data="cover:remove"),
        ],
        [InlineKeyboardButton("🧹 Очистить все теги", callback_data="tags:clear")],
        [InlineKeyboardButton("📤 Готово, вернуть MP3", callback_data="file:send")],
    ])


def get_tags(path: str) -> ID3:
    try:
        return ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
        tags.save(path, v2_version=3)
        return ID3(path)


def frame_text(tags: ID3, frame_id: str) -> str:
    frame = tags.get(frame_id)
    if not frame or not hasattr(frame, "text"):
        return "—"
    value = " / ".join(str(x) for x in frame.text).strip()
    return value or "—"


def comment_text(tags: ID3) -> str:
    frames = tags.getall("COMM")
    if not frames:
        return "—"
    values = getattr(frames[0], "text", [])
    return str(values[0]).strip() if values else "—"


def summary(path: str) -> str:
    tags = get_tags(path)
    try:
        audio = MP3(path)
        sec = int(audio.info.length)
        duration = f"{sec // 60}:{sec % 60:02d}"
        bitrate = f"{round(audio.info.bitrate / 1000)} kbps"
    except Exception:
        duration, bitrate = "—", "—"

    values = {
        "Название": frame_text(tags, "TIT2"),
        "Исполнитель": frame_text(tags, "TPE1"),
        "Альбом": frame_text(tags, "TALB"),
        "Автор альбома": frame_text(tags, "TPE2"),
        "Год": frame_text(tags, "TDRC"),
        "Жанр": frame_text(tags, "TCON"),
        "Трек": frame_text(tags, "TRCK"),
        "Диск": frame_text(tags, "TPOS"),
        "Композитор": frame_text(tags, "TCOM"),
        "Комментарий": comment_text(tags),
        "Обложка": "есть" if tags.getall("APIC") else "нет",
    }

    lines = ["🎧 <b>MP3 Tag Editor</b>", ""]
    lines.extend(f"{name}: <b>{html.escape(value)}</b>" for name, value in values.items())
    lines.extend(["", f"Длительность: {duration}", f"Битрейт: {bitrate}"])
    return "\n".join(lines)


def current_file(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    path = context.user_data.get("mp3_path")
    return path if path and Path(path).exists() else None


def cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    work_dir = context.user_data.get("work_dir")
    if work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)


def safe_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", value).strip(" .")
    if not value:
        value = "edited"
    if not value.lower().endswith(".mp3"):
        value += ".mp3"
    return value[:180]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🎵 <b>MP3 Tag Editor</b>\n\n"
        "Пришли MP3-файл. Можно менять название, исполнителя, альбом, автора альбома, "
        "год, жанр, номер трека/диска, композитора, комментарий и обложку.\n\n"
        "Аудио не перекодируется, меняются только ID3-теги.",
        parse_mode="HTML",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting"] = None
    await update.effective_message.reply_text("Редактирование поля отменено.")


async def receive_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    file_id = None
    filename = "track.mp3"

    if message.audio:
        file_id = message.audio.file_id
        filename = message.audio.file_name or "track.mp3"
    elif message.document:
        name = message.document.file_name or ""
        mime = (message.document.mime_type or "").lower()
        if not (name.lower().endswith(".mp3") or mime in {"audio/mpeg", "audio/mp3"}):
            return
        file_id = message.document.file_id
        filename = name or "track.mp3"

    if not file_id:
        return

    cleanup(context)
    context.user_data.clear()

    work_dir = tempfile.mkdtemp(prefix=f"mp3tags_{update.effective_user.id}_")
    mp3_path = str(Path(work_dir) / "source.mp3")

    telegram_file = await context.bot.get_file(file_id)
    await telegram_file.download_to_drive(mp3_path)

    try:
        MP3(mp3_path)
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        await message.reply_text("Не получилось прочитать этот файл как MP3.")
        return

    context.user_data.update({
        "work_dir": work_dir,
        "mp3_path": mp3_path,
        "original_name": filename,
        "awaiting": None,
    })

    await message.reply_text(summary(mp3_path), reply_markup=keyboard(), parse_mode="HTML")


async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("awaiting") != "cover":
        return

    path = current_file(context)
    if not path:
        return

    message = update.effective_message
    file_id = None
    mime = "image/jpeg"

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        name = (message.document.file_name or "").lower()
        suffix = Path(name).suffix
        doc_mime = (message.document.mime_type or "").lower()
        if suffix not in IMAGE_EXTENSIONS and not doc_mime.startswith("image/"):
            await message.reply_text("Нужна картинка JPG, PNG или WEBP.")
            return
        file_id = message.document.file_id
        mime = doc_mime if doc_mime.startswith("image/") else {
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(suffix, "image/jpeg")

    if not file_id:
        return

    cover_path = str(Path(context.user_data["work_dir"]) / "cover.bin")
    telegram_file = await context.bot.get_file(file_id)
    await telegram_file.download_to_drive(cover_path)
    data = Path(cover_path).read_bytes()

    if len(data) > 10 * 1024 * 1024:
        await message.reply_text("Обложка слишком большая. Максимум 10 МБ.")
        return

    tags = get_tags(path)
    tags.delall("APIC")
    tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    tags.save(path, v2_version=3)
    context.user_data["awaiting"] = None

    await message.reply_text(summary(path), reply_markup=keyboard(), parse_mode="HTML")


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting = context.user_data.get("awaiting")
    path = current_file(context)

    if not awaiting or awaiting == "cover" or not path:
        return

    value = (update.effective_message.text or "").strip()
    remove = value == "-"
    tags = get_tags(path)

    if awaiting == "comment":
        tags.delall("COMM")
        if not remove:
            tags.add(COMM(encoding=3, lang="eng", desc="", text=[value]))
    elif awaiting in TEXT_FIELDS:
        _, frame_id, frame_type = TEXT_FIELDS[awaiting]
        tags.delall(frame_id)
        if not remove:
            tags.add(frame_type(encoding=3, text=[value]))
    else:
        return

    tags.save(path, v2_version=3)
    context.user_data["awaiting"] = None
    await update.effective_message.reply_text(
        summary(path), reply_markup=keyboard(), parse_mode="HTML"
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    path = current_file(context)
    if not path:
        await query.message.reply_text("Файл уже недоступен. Пришли MP3 ещё раз.")
        return

    data = query.data or ""

    if data.startswith("edit:"):
        field = data.split(":", 1)[1]
        context.user_data["awaiting"] = field
        label = "Комментарий" if field == "comment" else TEXT_FIELDS[field][0]
        await query.message.reply_text(
            f"Введи новое значение для «{label}».\n"
            "Чтобы очистить поле, отправь: -"
        )
        return

    if data == "cover:set":
        context.user_data["awaiting"] = "cover"
        await query.message.reply_text("Пришли новую обложку как фото или JPG/PNG/WEBP.")
        return

    tags = get_tags(path)

    if data == "cover:remove":
        tags.delall("APIC")
        tags.save(path, v2_version=3)

    elif data == "tags:clear":
        tags.clear()
        tags.save(path, v2_version=3)

    elif data == "file:send":
        artist = frame_text(tags, "TPE1")
        title = frame_text(tags, "TIT2")

        if artist != "—" and title != "—":
            filename = safe_filename(f"{artist} - {title}")
        elif title != "—":
            filename = safe_filename(title)
        else:
            filename = safe_filename(context.user_data.get("original_name", "edited.mp3"))

        await query.message.reply_document(
            document=Path(path),
            filename=filename,
            caption="✅ Готово. Теги сохранены.",
        )
        return
    else:
        return

    context.user_data["awaiting"] = None
    await query.message.edit_text(summary(path), reply_markup=keyboard(), parse_mode="HTML")


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    document = update.effective_message.document
    if not document:
        return

    name = (document.file_name or "").lower()
    mime = (document.mime_type or "").lower()

    if context.user_data.get("awaiting") == "cover" and (
        Path(name).suffix in IMAGE_EXTENSIONS or mime.startswith("image/")
    ):
        await receive_image(update, context)
        return

    if name.endswith(".mp3") or mime in {"audio/mpeg", "audio/mp3"}:
        await receive_audio(update, context)


bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("help", start))
bot_app.add_handler(CommandHandler("cancel", cancel))
bot_app.add_handler(CallbackQueryHandler(on_button))
bot_app.add_handler(MessageHandler(filters.AUDIO, receive_audio))
bot_app.add_handler(MessageHandler(filters.PHOTO, receive_image))
bot_app.add_handler(MessageHandler(filters.Document.ALL, receive_document))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text))


@api.on_event("startup")
async def on_startup() -> None:
    await bot_app.initialize()
    await bot_app.start()

    if PUBLIC_URL:
        await bot_app.bot.set_webhook(
            url=f"{PUBLIC_URL}/telegram/{WEBHOOK_SECRET}",
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )


@api.on_event("shutdown")
async def on_shutdown() -> None:
    await bot_app.stop()
    await bot_app.shutdown()


@api.get("/")
async def root():
    return {"ok": True, "service": "mp3-tag-editor-bot"}


@api.get("/health")
async def health():
    return {"ok": True}


@api.post("/telegram/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    payload = await request.json()
    update = Update.de_json(payload, bot_app.bot)
    await bot_app.process_update(update)
    return {"ok": True}
