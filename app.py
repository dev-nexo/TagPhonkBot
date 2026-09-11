import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    ID3NoHeaderError,
    TALB,
    TBPM,
    TCOM,
    TCON,
    TCOP,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TSRC,
    USLT,
    WXXX,
)
from mutagen.mp3 import MP3
from PIL import Image, UnidentifiedImageError
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

VERSION = "3.0.0"
BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-this-secret")

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_COVER_BYTES = 10 * 1024 * 1024
BATCH_FILE_LIMIT = 10
BATCH_TOTAL_LIMIT = 45 * 1024 * 1024
UNDO_LIMIT = 10
TEMP_TTL_SECONDS = 6 * 60 * 60

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/recording/"
COVER_ART_URL = "https://coverartarchive.org/release/{release_id}/front-500"
MUSICBRAINZ_USER_AGENT = (
    "TagPhonkBot/3.0 (https://github.com/dev-nexo/TagPhonkBot)"
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Never log outgoing Telegram Bot API URLs at INFO level:
# the bot token is embedded in those URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("tagphonk")

bot_app = Application.builder().token(BOT_TOKEN).updater(None).build()


@asynccontextmanager
async def lifespan(app: FastAPI):
    purge_stale_temp()
    await bot_app.initialize()
    await bot_app.start()

    await bot_app.bot.set_my_commands(
        [
            BotCommand("start", "Открыть TagPhonk V3"),
            BotCommand("batch", "Пакетно обработать MP3"),
            BotCommand("privacy", "Как обрабатываются файлы"),
            BotCommand("cancel", "Отменить текущее действие"),
            BotCommand("help", "Помощь"),
        ]
    )

    try:
        await bot_app.bot.set_my_short_description(
            "Редактор MP3-тегов, обложек и метаданных прямо в Telegram."
        )
        await bot_app.bot.set_my_description(
            "TagPhonk V3: меняй ID3-теги и обложки, ищи метаданные через MusicBrainz, "
            "отменяй изменения и обрабатывай несколько MP3 одним пакетом."
        )
    except TelegramError:
        logger.warning("Could not update bot description", exc_info=True)

    if PUBLIC_URL:
        await bot_app.bot.set_webhook(
            url=f"{PUBLIC_URL}/telegram",
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            secret_token=WEBHOOK_SECRET,
        )
        logger.info("Webhook configured: %s/telegram", PUBLIC_URL)

    try:
        yield
    finally:
        await bot_app.stop()
        await bot_app.shutdown()


api = FastAPI(title="TagPhonk V3", version=VERSION, lifespan=lifespan)

mb_lock = asyncio.Lock()
mb_last_request = 0.0

FIELD_SPECS = {
    "title": ("Название", "TIT2", TIT2),
    "artist": ("Исполнитель", "TPE1", TPE1),
    "album": ("Альбом", "TALB", TALB),
    "albumartist": ("Автор альбома", "TPE2", TPE2),
    "year": ("Год", "TDRC", TDRC),
    "genre": ("Жанр", "TCON", TCON),
    "track": ("Номер трека", "TRCK", TRCK),
    "disc": ("Номер диска", "TPOS", TPOS),
    "composer": ("Композитор", "TCOM", TCOM),
    "bpm": ("BPM", "TBPM", TBPM),
    "publisher": ("Издатель", "TPUB", TPUB),
    "copyright": ("Copyright", "TCOP", TCOP),
    "isrc": ("ISRC", "TSRC", TSRC),
}

WIZARD_FIELDS = [
    "title",
    "artist",
    "album",
    "albumartist",
    "year",
    "genre",
    "track",
    "disc",
    "composer",
    "bpm",
]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

JUNK_PREFIXES = {
    "AENC",
    "ENCR",
    "GEOB",
    "GRID",
    "MCDI",
    "OWNE",
    "POSS",
    "PRIV",
    "RBUF",
    "RVA2",
    "SEEK",
    "SIGN",
    "SYTC",
    "TENC",
    "TSSE",
    "UFID",
}


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Быстро изменить", callback_data="menu:quick"),
                InlineKeyboardButton("🧭 Изменить всё", callback_data="wizard:start"),
            ],
            [
                InlineKeyboardButton("✨ Авто-теги", callback_data="auto:search"),
                InlineKeyboardButton("🖼 Найти обложку", callback_data="cover:find"),
            ],
            [
                InlineKeyboardButton("🧰 Доп. теги", callback_data="menu:advanced"),
                InlineKeyboardButton("📄 Имя файла", callback_data="menu:rename"),
            ],
            [
                InlineKeyboardButton("🧹 Очистить мусор", callback_data="tags:clean"),
                InlineKeyboardButton("↩️ Отменить", callback_data="undo:last"),
            ],
            [
                InlineKeyboardButton("ℹ️ Все теги", callback_data="info:full"),
                InlineKeyboardButton("⚠️ Стереть всё", callback_data="tags:clear_ask"),
            ],
            [InlineKeyboardButton("📦 Пакетный режим", callback_data="batch:start")],
            [InlineKeyboardButton("📤 Скачать готовый MP3", callback_data="file:send")],
        ]
    )


def quick_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
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
            [InlineKeyboardButton("⬅️ Назад", callback_data="nav:main")],
        ]
    )


def advanced_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🥁 BPM", callback_data="edit:bpm"),
                InlineKeyboardButton("🏷 Издатель", callback_data="edit:publisher"),
            ],
            [
                InlineKeyboardButton("© Copyright", callback_data="edit:copyright"),
                InlineKeyboardButton("🔐 ISRC", callback_data="edit:isrc"),
            ],
            [
                InlineKeyboardButton("🔗 Сайт", callback_data="edit:website"),
                InlineKeyboardButton("📝 Lyrics", callback_data="edit:lyrics"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="nav:main")],
        ]
    )


def rename_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("ARTIST - TITLE.mp3", callback_data="rename:artist_title")],
            [InlineKeyboardButton("TITLE.mp3", callback_data="rename:title")],
            [
                InlineKeyboardButton(
                    "ARTIST - TITLE [YEAR].mp3",
                    callback_data="rename:artist_title_year",
                )
            ],
            [InlineKeyboardButton("✍️ Свой шаблон", callback_data="rename:custom")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="nav:main")],
        ]
    )


def clear_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🗑 Да, стереть", callback_data="tags:clear_yes"),
                InlineKeyboardButton("Отмена", callback_data="nav:main"),
            ]
        ]
    )


def wizard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏭ Пропустить", callback_data="wizard:skip"),
                InlineKeyboardButton("✖️ Закончить", callback_data="wizard:stop"),
            ]
        ]
    )


def batch_collect_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Закончить добавление", callback_data="batch:finish")],
            [InlineKeyboardButton("✖️ Отмена", callback_data="batch:cancel")],
        ]
    )


def batch_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👤 Artist всем", callback_data="batch:set:artist"),
                InlineKeyboardButton("💿 Album всем", callback_data="batch:set:album"),
            ],
            [
                InlineKeyboardButton("🎵 Genre всем", callback_data="batch:set:genre"),
                InlineKeyboardButton("📅 Year всем", callback_data="batch:set:year"),
            ],
            [InlineKeyboardButton("🖼 Одна обложка всем", callback_data="batch:cover")],
            [InlineKeyboardButton("🧹 Очистить мусор у всех", callback_data="batch:clean")],
            [InlineKeyboardButton("📦 Скачать ZIP", callback_data="batch:zip")],
            [InlineKeyboardButton("✖️ Закрыть пакет", callback_data="batch:cancel")],
        ]
    )


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📦 Пакетный режим", callback_data="batch:start")]]
    )


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


def clip_text(value: str, limit: int = 180) -> str:
    value = str(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def read_field(tags: ID3, field: str) -> str:
    if field == "comment":
        frames = tags.getall("COMM")
        if not frames:
            return "—"
        values = getattr(frames[0], "text", [])
        return str(values[0]).strip() if values else "—"

    if field == "lyrics":
        frames = tags.getall("USLT")
        if not frames:
            return "—"
        value = str(getattr(frames[0], "text", "")).strip()
        return value or "—"

    if field == "website":
        frame = tags.get("WXXX:Website")
        if frame and getattr(frame, "url", ""):
            return str(frame.url).strip() or "—"
        return "—"

    spec = FIELD_SPECS.get(field)
    if not spec:
        return "—"
    return frame_text(tags, spec[1])


def write_field(tags: ID3, field: str, value: Optional[str]) -> None:
    value = None if value is None else value.strip()

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

    spec = FIELD_SPECS.get(field)
    if not spec:
        raise ValueError(f"Unknown field: {field}")

    _, frame_id, frame_type = spec
    tags.delall(frame_id)
    if value:
        tags.add(frame_type(encoding=3, text=[value]))


def get_cover(tags: ID3) -> Optional[APIC]:
    covers = tags.getall("APIC")
    return covers[0] if covers else None


def cover_details(tags: ID3) -> str:
    cover = get_cover(tags)
    if not cover:
        return "нет"

    size_kb = max(1, round(len(cover.data) / 1024))
    try:
        with Image.open(BytesIO(cover.data)) as image:
            width, height = image.size
        return f"{width}×{height}, {size_kb} KB"
    except Exception:
        return f"{size_kb} KB"


def file_stats(path: str) -> tuple[str, str, str]:
    try:
        audio = MP3(path)
        seconds = int(audio.info.length)
        duration = f"{seconds // 60}:{seconds % 60:02d}"
        bitrate = f"{round(audio.info.bitrate / 1000)} kbps"
    except Exception:
        duration = "—"
        bitrate = "—"

    size = Path(path).stat().st_size
    if size >= 1024 * 1024:
        file_size = f"{size / (1024 * 1024):.1f} MB"
    else:
        file_size = f"{max(1, round(size / 1024))} KB"

    return duration, bitrate, file_size


def compact_summary(path: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    tags = get_tags(path)
    duration, bitrate, file_size = file_stats(path)

    title = html.escape(clip_text(frame_text(tags, "TIT2"), 90))
    artist = html.escape(clip_text(frame_text(tags, "TPE1"), 90))
    album = html.escape(clip_text(frame_text(tags, "TALB"), 90))
    year = html.escape(clip_text(frame_text(tags, "TDRC"), 30))
    genre = html.escape(clip_text(frame_text(tags, "TCON"), 60))
    cover = html.escape(cover_details(tags))
    template = html.escape(context.user_data.get("filename_template", "{artist} - {title}"))

    return (
        "🎧 <b>TagPhonk V3</b>\n\n"
        f"<b>{artist} — {title}</b>\n"
        f"💿 {album}\n"
        f"📅 {year}   🎵 {genre}\n"
        f"⏱ {duration}   🎚 {bitrate}\n"
        f"📦 {file_size}\n"
        f"🖼 {cover}\n\n"
        f"📄 Шаблон: <code>{template}</code>"
    )


def full_summary(path: str) -> str:
    tags = get_tags(path)
    duration, bitrate, file_size = file_stats(path)
    lyrics = read_field(tags, "lyrics")

    values = [
        ("Название", read_field(tags, "title")),
        ("Исполнитель", read_field(tags, "artist")),
        ("Альбом", read_field(tags, "album")),
        ("Автор альбома", read_field(tags, "albumartist")),
        ("Год", read_field(tags, "year")),
        ("Жанр", read_field(tags, "genre")),
        ("Трек", read_field(tags, "track")),
        ("Диск", read_field(tags, "disc")),
        ("Композитор", read_field(tags, "composer")),
        ("BPM", read_field(tags, "bpm")),
        ("Издатель", read_field(tags, "publisher")),
        ("Copyright", read_field(tags, "copyright")),
        ("ISRC", read_field(tags, "isrc")),
        ("Сайт", read_field(tags, "website")),
        ("Комментарий", read_field(tags, "comment")),
        ("Lyrics", "есть" if lyrics != "—" else "нет"),
        ("Обложка", cover_details(tags)),
    ]

    lines = ["ℹ️ <b>Все метаданные</b>", ""]
    for name, value in values:
        lines.append(f"{name}: <b>{html.escape(clip_text(value, 240))}</b>")

    lines.extend(
        [
            "",
            f"Длительность: {duration}",
            f"Битрейт: {bitrate}",
            f"Размер: {file_size}",
        ]
    )
    return "\n".join(lines)


def current_file(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    path = context.user_data.get("mp3_path")
    return path if path and Path(path).exists() else None


def cleanup_single(context: ContextTypes.DEFAULT_TYPE) -> None:
    work_dir = context.user_data.get("work_dir")
    if work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)
    for key in (
        "work_dir",
        "mp3_path",
        "original_name",
        "awaiting",
        "undo_stack",
        "mb_results",
        "last_release_id",
        "cover_candidate",
        "cover_candidate_mime",
        "wizard_index",
    ):
        context.user_data.pop(key, None)


def cleanup_batch(context: ContextTypes.DEFAULT_TYPE) -> None:
    batch_dir = context.user_data.get("batch_dir")
    if batch_dir:
        shutil.rmtree(batch_dir, ignore_errors=True)
    for key in (
        "batch_dir",
        "batch_paths",
        "batch_names",
        "batch_collecting",
        "batch_total_bytes",
    ):
        context.user_data.pop(key, None)
    if str(context.user_data.get("awaiting", "")).startswith("batch:"):
        context.user_data["awaiting"] = None


def purge_stale_temp() -> None:
    base = Path(tempfile.gettempdir())
    now = time.time()

    for child in base.iterdir():
        if not child.is_dir() or not child.name.startswith("tagphonk_"):
            continue
        try:
            if now - child.stat().st_mtime > TEMP_TTL_SECONDS:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


def safe_filename(value: str, extension: str = ".mp3") -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", value).strip(" .")
    value = re.sub(r"\s{2,}", " ", value)
    if not value:
        value = "edited"
    if extension and not value.lower().endswith(extension.lower()):
        value += extension
    return value[:180]


def render_output_name(path: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    tags = get_tags(path)
    template = context.user_data.get("filename_template", "{artist} - {title}")
    values = {
        "artist": "" if read_field(tags, "artist") == "—" else read_field(tags, "artist"),
        "title": "" if read_field(tags, "title") == "—" else read_field(tags, "title"),
        "album": "" if read_field(tags, "album") == "—" else read_field(tags, "album"),
        "year": "" if read_field(tags, "year") == "—" else read_field(tags, "year"),
        "track": "" if read_field(tags, "track") == "—" else read_field(tags, "track"),
    }

    try:
        rendered = template.format_map(values)
    except (KeyError, ValueError):
        rendered = f"{values['artist']} - {values['title']}"

    rendered = re.sub(r"\s*-\s*$", "", rendered).strip()
    rendered = re.sub(r"^\s*-\s*", "", rendered).strip()
    rendered = re.sub(r"\[\s*\]", "", rendered).strip()

    if not rendered:
        original = context.user_data.get("original_name", "edited.mp3")
        return safe_filename(Path(original).stem)

    return safe_filename(rendered)


def save_undo(context: ContextTypes.DEFAULT_TYPE, path: str) -> None:
    work_dir = context.user_data.get("work_dir")
    if not work_dir:
        return

    undo_dir = Path(work_dir) / "undo"
    undo_dir.mkdir(exist_ok=True)
    stack = context.user_data.setdefault("undo_stack", [])

    backup = undo_dir / f"{time.time_ns()}.mp3"
    shutil.copy2(path, backup)
    stack.append(str(backup))

    while len(stack) > UNDO_LIMIT:
        old = stack.pop(0)
        Path(old).unlink(missing_ok=True)


def undo_last(context: ContextTypes.DEFAULT_TYPE, path: str) -> bool:
    stack = context.user_data.setdefault("undo_stack", [])
    while stack:
        backup = Path(stack.pop())
        if backup.exists():
            shutil.copy2(backup, path)
            backup.unlink(missing_ok=True)
            return True
    return False


def normalize_cover(data: bytes, max_size: int = 1600) -> bytes:
    if len(data) > MAX_COVER_BYTES:
        raise ValueError("cover-too-large")

    try:
        with Image.open(BytesIO(data)) as image:
            image.load()

            if image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")

            image.thumbnail((max_size, max_size))

            output = BytesIO()
            image.save(output, format="JPEG", quality=92, optimize=True)
            return output.getvalue()
    except UnidentifiedImageError as exc:
        raise ValueError("bad-cover") from exc


def preview_cover_bytes(path: str) -> Optional[BytesIO]:
    tags = get_tags(path)
    cover = get_cover(tags)
    if not cover:
        return None

    try:
        data = normalize_cover(cover.data, max_size=1000)
    except ValueError:
        return None

    bio = BytesIO(data)
    bio.name = "cover.jpg"
    return bio


def clean_junk_tags(path: str) -> int:
    tags = get_tags(path)
    removed = 0

    for key in list(tags.keys()):
        prefix = key.split(":", 1)[0]
        if prefix in JUNK_PREFIXES:
            del tags[key]
            removed += 1

    covers = tags.getall("APIC")
    if len(covers) > 1:
        first = covers[0]
        removed += len(covers) - 1
        tags.delall("APIC")
        tags.add(first)

    tags.save(path, v2_version=3)
    return removed


def set_cover(path: str, data: bytes, mime: str = "image/jpeg") -> None:
    tags = get_tags(path)
    tags.delall("APIC")
    tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    tags.save(path, v2_version=3)


async def send_track_card(
    message: Any,
    path: str,
    context: ContextTypes.DEFAULT_TYPE,
    note: Optional[str] = None,
) -> None:
    caption = compact_summary(path, context)
    if note:
        caption = f"{html.escape(note)}\n\n{caption}"

    cover = preview_cover_bytes(path)
    if cover:
        await message.reply_photo(
            photo=cover,
            caption=caption,
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    else:
        await message.reply_text(
            caption,
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )


def field_label(field: str) -> str:
    if field == "comment":
        return "Комментарий"
    if field == "website":
        return "Сайт"
    if field == "lyrics":
        return "Lyrics"
    return FIELD_SPECS[field][0]


async def ask_wizard_step(message: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    path = current_file(context)
    if not path:
        return

    index = int(context.user_data.get("wizard_index", 0))
    if index >= len(WIZARD_FIELDS):
        context.user_data["awaiting"] = None
        context.user_data.pop("wizard_index", None)
        await send_track_card(message, path, context, "✅ Все основные поля пройдены.")
        return

    field = WIZARD_FIELDS[index]
    tags = get_tags(path)
    current = read_field(tags, field)
    context.user_data["awaiting"] = f"wizard:{field}"

    await message.reply_text(
        f"🧭 <b>{index + 1}/{len(WIZARD_FIELDS)} · {html.escape(field_label(field))}</b>\n\n"
        f"Сейчас: <code>{html.escape(current)}</code>\n\n"
        "Отправь новое значение.\n"
        "Отправь <code>-</code>, чтобы очистить поле.",
        parse_mode="HTML",
        reply_markup=wizard_keyboard(),
    )


def build_musicbrainz_query(path: str, original_name: str = "") -> str:
    tags = get_tags(path)
    title = read_field(tags, "title")
    artist = read_field(tags, "artist")

    def esc(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    if title != "—" and artist != "—":
        return f'recording:"{esc(title)}" AND artist:"{esc(artist)}"'
    if title != "—":
        return f'recording:"{esc(title)}"'

    original = Path(original_name).stem if original_name else Path(path).stem
    if " - " in original:
        artist_guess, title_guess = original.split(" - ", 1)
        return (
            f'recording:"{esc(title_guess)}" '
            f'AND artist:"{esc(artist_guess)}"'
        )

    return f'recording:"{esc(original)}"'


def artist_credit_name(credit: Any) -> str:
    if not isinstance(credit, list):
        return ""

    parts: list[str] = []
    for item in credit:
        if isinstance(item, dict):
            parts.append(str(item.get("name") or item.get("artist", {}).get("name") or ""))
            join = str(item.get("joinphrase") or "")
            if join:
                parts.append(join)
    return "".join(parts).strip()


def simplify_mb_recording(recording: dict[str, Any]) -> dict[str, str]:
    releases = recording.get("releases") or []
    release = releases[0] if releases and isinstance(releases[0], dict) else {}
    release_credit = release.get("artist-credit") or []

    return {
        "title": str(recording.get("title") or ""),
        "artist": artist_credit_name(recording.get("artist-credit") or []),
        "album": str(release.get("title") or ""),
        "albumartist": artist_credit_name(release_credit),
        "year": str(
            (recording.get("first-release-date") or release.get("date") or "")
        )[:4],
        "recording_id": str(recording.get("id") or ""),
        "release_id": str(release.get("id") or ""),
        "score": str(recording.get("score") or ""),
    }


async def musicbrainz_search(path: str, original_name: str = "") -> list[dict[str, str]]:
    global mb_last_request

    query = build_musicbrainz_query(path, original_name)

    async with mb_lock:
        elapsed = time.monotonic() - mb_last_request
        if elapsed < 1.05:
            await asyncio.sleep(1.05 - elapsed)

        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
        params = {"query": query, "fmt": "json", "limit": 5}

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0),
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = await client.get(MUSICBRAINZ_URL, params=params)
            mb_last_request = time.monotonic()
            response.raise_for_status()
            payload = response.json()

    results: list[dict[str, str]] = []
    for recording in payload.get("recordings", []):
        if isinstance(recording, dict):
            item = simplify_mb_recording(recording)
            if item["title"]:
                results.append(item)

    return results[:5]


async def fetch_cover_art(release_id: str) -> Optional[bytes]:
    if not release_id:
        return None

    url = COVER_ART_URL.format(release_id=release_id)
    headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
        headers=headers,
    ) as client:
        response = await client.get(url)

    if response.status_code == 404:
        return None
    response.raise_for_status()
    return normalize_cover(response.content, max_size=1600)


def apply_mb_result(path: str, result: dict[str, str]) -> None:
    tags = get_tags(path)

    for field in ("title", "artist", "album", "albumartist", "year"):
        value = result.get(field, "").strip()
        if value:
            write_field(tags, field, value)

    tags.save(path, v2_version=3)


def media_info(message: Any) -> tuple[Optional[str], str, int]:
    if message.audio:
        return (
            message.audio.file_id,
            message.audio.file_name or "track.mp3",
            int(message.audio.file_size or 0),
        )

    if message.document:
        name = message.document.file_name or ""
        mime = (message.document.mime_type or "").lower()
        if name.lower().endswith(".mp3") or mime in {"audio/mpeg", "audio/mp3"}:
            return (
                message.document.file_id,
                name or "track.mp3",
                int(message.document.file_size or 0),
            )

    return None, "", 0


async def download_mp3(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    destination: str,
) -> tuple[bool, str]:
    file_id, filename, file_size = media_info(message)

    if not file_id:
        return False, "Это не MP3."

    if file_size and file_size > MAX_DOWNLOAD_BYTES:
        return (
            False,
            "Telegram Bot API позволяет боту скачать максимум 20 МБ на файл. "
            "Пришли MP3 до 20 МБ.",
        )

    try:
        telegram_file = await context.bot.get_file(file_id)
        await telegram_file.download_to_drive(destination)
    except BadRequest as exc:
        if "too big" in str(exc).lower() or "file is too big" in str(exc).lower():
            return False, "Файл слишком большой для загрузки ботом. Лимит — 20 МБ."
        logger.exception("Telegram rejected MP3 download")
        return False, "Telegram не дал скачать этот файл."
    except TelegramError:
        logger.exception("Telegram MP3 download failed")
        return False, "Не получилось скачать файл из Telegram."

    try:
        MP3(destination)
    except Exception:
        Path(destination).unlink(missing_ok=True)
        return False, "Не получилось прочитать этот файл как MP3."

    return True, filename


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🎵 <b>TagPhonk V3</b>\n\n"
        "Редактор MP3-метаданных прямо в Telegram.\n\n"
        "Что умею:\n"
        "• менять основные и расширенные ID3-теги;\n"
        "• менять и искать обложки;\n"
        "• искать метаданные через MusicBrainz;\n"
        "• откатывать изменения;\n"
        "• чистить служебный мусор;\n"
        "• переименовывать готовый файл по шаблону;\n"
        "• пакетно обрабатывать до 10 MP3.\n\n"
        "Пришли MP3 или открой пакетный режим.",
        parse_mode="HTML",
        reply_markup=start_keyboard(),
    )


async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🔒 <b>Файлы и приватность</b>\n\n"
        "MP3 временно обрабатываются на сервере и не используются как постоянное "
        "облачное хранилище. Временные рабочие папки автоматически очищаются.\n\n"
        "Для авто-тегов бот отправляет текстовый поисковый запрос в MusicBrainz. "
        "Сам MP3 туда не загружается.",
        parse_mode="HTML",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting"] = None
    context.user_data.pop("wizard_index", None)
    await update.effective_message.reply_text("Текущее действие отменено.")


async def batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_batch(update.effective_message, context)


async def start_batch(message: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    cleanup_batch(context)
    purge_stale_temp()

    batch_dir = tempfile.mkdtemp(prefix="tagphonk_batch_")
    context.user_data["batch_dir"] = batch_dir
    context.user_data["batch_paths"] = []
    context.user_data["batch_names"] = []
    context.user_data["batch_total_bytes"] = 0
    context.user_data["batch_collecting"] = True
    context.user_data["awaiting"] = None

    await message.reply_text(
        "📦 <b>Пакетный режим</b>\n\n"
        f"Отправляй MP3 по одному. Максимум {BATCH_FILE_LIMIT} файлов и примерно "
        "45 МБ суммарно, чтобы готовый ZIP влез в лимиты Telegram.\n\n"
        "Когда закончишь, нажми кнопку ниже.",
        parse_mode="HTML",
        reply_markup=batch_collect_keyboard(),
    )


async def receive_batch_audio(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    paths = context.user_data.setdefault("batch_paths", [])
    names = context.user_data.setdefault("batch_names", [])

    if len(paths) >= BATCH_FILE_LIMIT:
        await message.reply_text(
            f"Уже {BATCH_FILE_LIMIT} файлов. Нажми «Закончить добавление».",
            reply_markup=batch_collect_keyboard(),
        )
        return

    file_id, filename, file_size = media_info(message)
    if not file_id:
        return

    current_total = int(context.user_data.get("batch_total_bytes", 0))
    projected = current_total + max(file_size, 0)
    if file_size and projected > BATCH_TOTAL_LIMIT:
        await message.reply_text(
            "Этот файл превысит общий лимит пакета примерно в 45 МБ. "
            "Закончи текущий пакет или отправь файл поменьше.",
            reply_markup=batch_collect_keyboard(),
        )
        return

    index = len(paths) + 1
    destination = str(Path(context.user_data["batch_dir"]) / f"{index:02d}.mp3")
    ok, result = await download_mp3(message, context, destination)

    if not ok:
        await message.reply_text(result, reply_markup=batch_collect_keyboard())
        return

    actual_size = Path(destination).stat().st_size
    if current_total + actual_size > BATCH_TOTAL_LIMIT:
        Path(destination).unlink(missing_ok=True)
        await message.reply_text(
            "Общий размер пакета превысит 45 МБ. Этот файл не добавлен.",
            reply_markup=batch_collect_keyboard(),
        )
        return

    paths.append(destination)
    names.append(result)
    context.user_data["batch_total_bytes"] = current_total + actual_size

    total_mb = context.user_data["batch_total_bytes"] / (1024 * 1024)
    await message.reply_text(
        f"✅ Добавлено: <b>{html.escape(result)}</b>\n"
        f"Файлов: {len(paths)}/{BATCH_FILE_LIMIT} · {total_mb:.1f} МБ",
        parse_mode="HTML",
        reply_markup=batch_collect_keyboard(),
    )


async def receive_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("batch_collecting"):
        await receive_batch_audio(update, context)
        return

    message = update.effective_message
    purge_stale_temp()

    cleanup_single(context)
    work_dir = tempfile.mkdtemp(prefix="tagphonk_user_")
    mp3_path = str(Path(work_dir) / "source.mp3")

    ok, result = await download_mp3(message, context, mp3_path)
    if not ok:
        shutil.rmtree(work_dir, ignore_errors=True)
        await message.reply_text(result)
        return

    context.user_data.update(
        {
            "work_dir": work_dir,
            "mp3_path": mp3_path,
            "original_name": result,
            "awaiting": None,
            "undo_stack": [],
            "filename_template": "{artist} - {title}",
        }
    )

    await send_track_card(message, mp3_path, context, "✅ MP3 загружен.")


async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting = context.user_data.get("awaiting")
    if awaiting not in {"cover", "batch:cover"}:
        return

    message = update.effective_message
    file_id = None
    file_size = 0

    if message.photo:
        item = message.photo[-1]
        file_id = item.file_id
        file_size = int(item.file_size or 0)
    elif message.document:
        name = (message.document.file_name or "").lower()
        suffix = Path(name).suffix
        mime = (message.document.mime_type or "").lower()

        if suffix not in IMAGE_EXTENSIONS and not mime.startswith("image/"):
            await message.reply_text("Нужна картинка JPG, PNG или WEBP.")
            return

        file_id = message.document.file_id
        file_size = int(message.document.file_size or 0)

    if not file_id:
        return

    if file_size and file_size > MAX_COVER_BYTES:
        await message.reply_text("Обложка слишком большая. Максимум 10 МБ.")
        return

    try:
        telegram_file = await context.bot.get_file(file_id)
        raw = BytesIO()
        await telegram_file.download_to_memory(out=raw)
        data = normalize_cover(raw.getvalue())
    except ValueError:
        await message.reply_text("Не получилось прочитать изображение.")
        return
    except TelegramError:
        logger.exception("Cover download failed")
        await message.reply_text("Не получилось скачать обложку из Telegram.")
        return

    if awaiting == "batch:cover":
        paths = context.user_data.get("batch_paths", [])
        if not paths:
            await message.reply_text("В пакете нет файлов.")
            return

        for path in paths:
            set_cover(path, data)

        context.user_data["awaiting"] = None
        await message.reply_text(
            f"✅ Одна обложка установлена для {len(paths)} файлов.",
            reply_markup=batch_menu_keyboard(),
        )
        return

    path = current_file(context)
    if not path:
        await message.reply_text("Сначала пришли MP3.")
        return

    save_undo(context, path)
    set_cover(path, data)
    context.user_data["awaiting"] = None
    await send_track_card(message, path, context, "✅ Обложка заменена.")


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting = str(context.user_data.get("awaiting") or "")
    if not awaiting:
        return

    message = update.effective_message
    value = (message.text or "").strip()

    if awaiting.startswith("batch:"):
        field = awaiting.split(":", 1)[1]
        if field not in {"artist", "album", "genre", "year"}:
            return

        paths = context.user_data.get("batch_paths", [])
        if not paths:
            context.user_data["awaiting"] = None
            await message.reply_text("В пакете нет файлов.")
            return

        normalized = None if value == "-" else value
        for path in paths:
            tags = get_tags(path)
            write_field(tags, field, normalized)
            tags.save(path, v2_version=3)

        context.user_data["awaiting"] = None
        await message.reply_text(
            f"✅ Поле «{field_label(field)}» обновлено для {len(paths)} файлов.",
            reply_markup=batch_menu_keyboard(),
        )
        return

    path = current_file(context)
    if not path:
        context.user_data["awaiting"] = None
        return

    if awaiting == "rename_template":
        allowed = {"artist", "title", "album", "year", "track"}
        placeholders = set(re.findall(r"\{([^{}]+)\}", value))
        unknown = placeholders - allowed

        if unknown:
            await message.reply_text(
                "Неизвестные поля: "
                + ", ".join(sorted(unknown))
                + "\nМожно: {artist}, {title}, {album}, {year}, {track}"
            )
            return

        if len(value) > 120:
            await message.reply_text("Шаблон слишком длинный. Максимум 120 символов.")
            return

        context.user_data["filename_template"] = value or "{artist} - {title}"
        context.user_data["awaiting"] = None
        await send_track_card(message, path, context, "✅ Шаблон имени файла сохранён.")
        return

    if awaiting.startswith("wizard:"):
        field = awaiting.split(":", 1)[1]
        save_undo(context, path)

        tags = get_tags(path)
        write_field(tags, field, None if value == "-" else value)
        tags.save(path, v2_version=3)

        context.user_data["wizard_index"] = int(
            context.user_data.get("wizard_index", 0)
        ) + 1
        await ask_wizard_step(message, context)
        return

    field = awaiting
    if field not in FIELD_SPECS and field not in {"comment", "website", "lyrics"}:
        return

    save_undo(context, path)
    tags = get_tags(path)
    write_field(tags, field, None if value == "-" else value)
    tags.save(path, v2_version=3)

    context.user_data["awaiting"] = None
    await send_track_card(message, path, context, f"✅ «{field_label(field)}» обновлено.")


async def handle_batch_button(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> bool:
    if data == "batch:start":
        await start_batch(query.message, context)
        return True

    if data == "batch:cancel":
        cleanup_batch(context)
        await query.message.reply_text("Пакетный режим закрыт.")
        return True

    if data == "batch:finish":
        paths = context.user_data.get("batch_paths", [])
        if not paths:
            await query.message.reply_text(
                "Сначала добавь хотя бы один MP3.",
                reply_markup=batch_collect_keyboard(),
            )
            return True

        context.user_data["batch_collecting"] = False
        await query.message.reply_text(
            f"📦 В пакете {len(paths)} файлов. Что делаем?",
            reply_markup=batch_menu_keyboard(),
        )
        return True

    if data.startswith("batch:set:"):
        field = data.split(":", 2)[2]
        if field not in {"artist", "album", "genre", "year"}:
            return True

        context.user_data["awaiting"] = f"batch:{field}"
        await query.message.reply_text(
            f"Введи «{field_label(field)}» для всех файлов.\n"
            "Отправь «-», чтобы очистить поле у всех."
        )
        return True

    if data == "batch:cover":
        context.user_data["awaiting"] = "batch:cover"
        await query.message.reply_text("Пришли JPG/PNG/WEBP. Она будет установлена всем файлам.")
        return True

    if data == "batch:clean":
        paths = context.user_data.get("batch_paths", [])
        total = 0
        for path in paths:
            total += clean_junk_tags(path)

        await query.message.reply_text(
            f"🧹 Готово. Удалено служебных/лишних фреймов: {total}.",
            reply_markup=batch_menu_keyboard(),
        )
        return True

    if data == "batch:zip":
        paths = context.user_data.get("batch_paths", [])
        names = context.user_data.get("batch_names", [])

        if not paths:
            await query.message.reply_text("В пакете нет файлов.")
            return True

        batch_dir = Path(context.user_data["batch_dir"])
        zip_path = batch_dir / "TagPhonk_V3_batch.zip"
        used: set[str] = set()

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for index, path in enumerate(paths):
                tags = get_tags(path)
                artist = "" if read_field(tags, "artist") == "—" else read_field(tags, "artist")
                title = "" if read_field(tags, "title") == "—" else read_field(tags, "title")

                if artist or title:
                    base = safe_filename(f"{artist} - {title}")
                else:
                    original = names[index] if index < len(names) else f"track_{index + 1}.mp3"
                    base = safe_filename(Path(original).stem)

                candidate = base
                n = 2
                while candidate.lower() in used:
                    candidate = safe_filename(f"{Path(base).stem} ({n})")
                    n += 1

                used.add(candidate.lower())
                archive.write(path, arcname=candidate)

        if zip_path.stat().st_size > 50 * 1024 * 1024:
            zip_path.unlink(missing_ok=True)
            await query.message.reply_text(
                "ZIP получился больше 50 МБ и не влезает в лимит отправки Telegram."
            )
            return True

        await query.message.reply_document(
            document=zip_path,
            filename="TagPhonk_V3_batch.zip",
            caption=f"✅ Готово. Файлов в архиве: {len(paths)}.",
        )
        return True

    return False


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("batch:"):
        if await handle_batch_button(query, context, data):
            return

    path = current_file(context)
    if not path:
        await query.message.reply_text("Сначала пришли MP3.")
        return

    if data == "nav:main":
        await send_track_card(query.message, path, context)
        return

    if data == "menu:quick":
        await query.message.reply_text("✏️ Что изменить?", reply_markup=quick_keyboard())
        return

    if data == "menu:advanced":
        await query.message.reply_text(
            "🧰 Расширенные ID3-поля:",
            reply_markup=advanced_keyboard(),
        )
        return

    if data == "menu:rename":
        await query.message.reply_text(
            "📄 Выбери шаблон имени готового файла.\n"
            "Метаданные внутри MP3 это не меняет.",
            reply_markup=rename_keyboard(),
        )
        return

    if data.startswith("rename:"):
        mode = data.split(":", 1)[1]
        templates = {
            "artist_title": "{artist} - {title}",
            "title": "{title}",
            "artist_title_year": "{artist} - {title} [{year}]",
        }

        if mode == "custom":
            context.user_data["awaiting"] = "rename_template"
            await query.message.reply_text(
                "Отправь свой шаблон.\n\n"
                "Доступно: <code>{artist}</code>, <code>{title}</code>, "
                "<code>{album}</code>, <code>{year}</code>, <code>{track}</code>\n\n"
                "Например:\n"
                "<code>{track}. {artist} - {title} [{year}]</code>",
                parse_mode="HTML",
            )
            return

        if mode in templates:
            context.user_data["filename_template"] = templates[mode]
            await send_track_card(query.message, path, context, "✅ Шаблон выбран.")
        return

    if data.startswith("edit:"):
        field = data.split(":", 1)[1]
        if field not in FIELD_SPECS and field not in {"comment", "website", "lyrics"}:
            return

        context.user_data["awaiting"] = field
        current = read_field(get_tags(path), field)

        await query.message.reply_text(
            f"✏️ <b>{html.escape(field_label(field))}</b>\n\n"
            f"Сейчас: <code>{html.escape(current)}</code>\n\n"
            "Отправь новое значение.\n"
            "Чтобы очистить поле, отправь <code>-</code>.",
            parse_mode="HTML",
        )
        return

    if data == "wizard:start":
        context.user_data["wizard_index"] = 0
        await ask_wizard_step(query.message, context)
        return

    if data == "wizard:skip":
        context.user_data["wizard_index"] = int(
            context.user_data.get("wizard_index", 0)
        ) + 1
        await ask_wizard_step(query.message, context)
        return

    if data == "wizard:stop":
        context.user_data["awaiting"] = None
        context.user_data.pop("wizard_index", None)
        await send_track_card(query.message, path, context, "Редактирование по шагам закончено.")
        return

    if data == "cover:set":
        context.user_data["awaiting"] = "cover"
        await query.message.reply_text("Пришли новую обложку как JPG/PNG/WEBP.")
        return

    if data == "cover:remove":
        tags = get_tags(path)
        if not tags.getall("APIC"):
            await query.message.reply_text("Обложки и так нет.")
            return

        save_undo(context, path)
        tags.delall("APIC")
        tags.save(path, v2_version=3)
        await send_track_card(query.message, path, context, "🗑 Обложка удалена.")
        return

    if data == "info:full":
        await query.message.reply_text(
            full_summary(path),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="nav:main")]]
            ),
        )
        return

    if data == "tags:clean":
        save_undo(context, path)
        removed = clean_junk_tags(path)
        await send_track_card(
            query.message,
            path,
            context,
            f"🧹 Удалено служебных/лишних фреймов: {removed}.",
        )
        return

    if data == "undo:last":
        if undo_last(context, path):
            await send_track_card(query.message, path, context, "↩️ Последнее изменение отменено.")
        else:
            await query.message.reply_text("История отмены пока пустая.")
        return

    if data == "tags:clear_ask":
        await query.message.reply_text(
            "⚠️ Будут удалены <b>все</b> ID3-теги и обложка.\n"
            "Сам аудиопоток останется нетронутым.",
            parse_mode="HTML",
            reply_markup=clear_confirm_keyboard(),
        )
        return

    if data == "tags:clear_yes":
        save_undo(context, path)
        tags = get_tags(path)
        tags.clear()
        tags.save(path, v2_version=3)
        await send_track_card(query.message, path, context, "🗑 Все ID3-теги удалены.")
        return

    if data == "auto:search":
        await query.message.reply_text("✨ Ищу варианты в MusicBrainz…")

        try:
            results = await musicbrainz_search(path, context.user_data.get("original_name", ""))
        except httpx.HTTPError:
            logger.exception("MusicBrainz search failed")
            await query.message.reply_text(
                "MusicBrainz сейчас не ответил. Попробуй ещё раз чуть позже."
            )
            return

        if not results:
            await query.message.reply_text(
                "Ничего подходящего не нашёл. "
                "Проверь название/исполнителя или сначала задай их вручную."
            )
            return

        context.user_data["mb_results"] = results
        buttons = []

        lines = ["✨ <b>Нашёл варианты:</b>", ""]
        for index, result in enumerate(results):
            artist = result["artist"] or "?"
            title = result["title"] or "?"
            album = result["album"] or "?"
            year = result["year"] or "?"
            score = result["score"] or "?"

            lines.append(
                f"{index + 1}. <b>{html.escape(artist)} — {html.escape(title)}</b>\n"
                f"   {html.escape(album)} · {html.escape(year)} · score {html.escape(score)}"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"{index + 1}. {artist[:22]} — {title[:25]}",
                        callback_data=f"mb:{index}",
                    )
                ]
            )

        buttons.append([InlineKeyboardButton("Отмена", callback_data="nav:main")])

        await query.message.reply_text(
            "\n\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("mb:"):
        try:
            index = int(data.split(":", 1)[1])
            result = context.user_data.get("mb_results", [])[index]
        except (ValueError, IndexError, TypeError):
            await query.message.reply_text("Этот результат уже устарел. Запусти поиск заново.")
            return

        save_undo(context, path)
        apply_mb_result(path, result)
        context.user_data["last_release_id"] = result.get("release_id", "")
        await send_track_card(query.message, path, context, "✨ Метаданные применены.")
        return

    if data == "cover:find":
        release_id = str(context.user_data.get("last_release_id") or "")

        if not release_id:
            await query.message.reply_text("🖼 Ищу релиз и обложку…")
            try:
                results = await musicbrainz_search(path, context.user_data.get("original_name", ""))
            except httpx.HTTPError:
                logger.exception("MusicBrainz search for cover failed")
                await query.message.reply_text("MusicBrainz сейчас не ответил.")
                return

            for result in results:
                if result.get("release_id"):
                    release_id = result["release_id"]
                    break

        if not release_id:
            await query.message.reply_text(
                "Не нашёл релиз, по которому можно получить обложку."
            )
            return

        try:
            cover_data = await fetch_cover_art(release_id)
        except httpx.HTTPError:
            logger.exception("Cover Art Archive request failed")
            await query.message.reply_text("Cover Art Archive сейчас не ответил.")
            return

        if not cover_data:
            await query.message.reply_text("Для найденного релиза обложки в архиве нет.")
            return

        context.user_data["cover_candidate"] = cover_data
        context.user_data["cover_candidate_mime"] = "image/jpeg"
        bio = BytesIO(cover_data)
        bio.name = "cover.jpg"

        await query.message.reply_photo(
            photo=bio,
            caption="🖼 Найденная обложка. Поставить её в MP3?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Поставить",
                            callback_data="cover:apply_candidate",
                        ),
                        InlineKeyboardButton("Отмена", callback_data="nav:main"),
                    ]
                ]
            ),
        )
        return

    if data == "cover:apply_candidate":
        cover_data = context.user_data.get("cover_candidate")
        if not isinstance(cover_data, (bytes, bytearray)):
            await query.message.reply_text("Обложка уже недоступна. Найди её заново.")
            return

        save_undo(context, path)
        set_cover(path, bytes(cover_data), "image/jpeg")
        context.user_data.pop("cover_candidate", None)
        await send_track_card(query.message, path, context, "✅ Обложка установлена.")
        return

    if data == "file:send":
        filename = render_output_name(path, context)
        await query.message.reply_document(
            document=Path(path),
            filename=filename,
            caption="✅ Готово. Аудио не перекодировалось, изменены только метаданные.",
        )
        return


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    document = update.effective_message.document
    if not document:
        return

    name = (document.file_name or "").lower()
    mime = (document.mime_type or "").lower()

    if context.user_data.get("awaiting") in {"cover", "batch:cover"} and (
        Path(name).suffix in IMAGE_EXTENSIONS or mime.startswith("image/")
    ):
        await receive_image(update, context)
        return

    if name.endswith(".mp3") or mime in {"audio/mpeg", "audio/mp3"}:
        await receive_audio(update, context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled Telegram update error", exc_info=context.error)


bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("help", start))
bot_app.add_handler(CommandHandler("privacy", privacy))
bot_app.add_handler(CommandHandler("batch", batch_command))
bot_app.add_handler(CommandHandler("cancel", cancel))
bot_app.add_handler(CallbackQueryHandler(on_button))
bot_app.add_handler(MessageHandler(filters.AUDIO, receive_audio))
bot_app.add_handler(MessageHandler(filters.PHOTO, receive_image))
bot_app.add_handler(MessageHandler(filters.Document.ALL, receive_document))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text))
bot_app.add_error_handler(on_error)


@api.get("/")
async def root():
    return {
        "ok": True,
        "service": "tagphonkbot",
        "version": VERSION,
    }


@api.get("/health")
async def health():
    return {"ok": True, "version": VERSION}


@api.post("/telegram")
async def webhook(request: Request):
    secret = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    payload = await request.json()
    update = Update.de_json(payload, bot_app.bot)
    await bot_app.process_update(update)
    return {"ok": True}
