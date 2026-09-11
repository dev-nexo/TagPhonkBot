
from __future__ import annotations

import hashlib
import hmac
import json
import time
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import HTTPException


def verify_telegram_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Open this Mini App inside Telegram.")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing Telegram hash.")

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram signature.")

    auth_date = int(pairs.get("auth_date", "0") or 0)
    if auth_date and time.time() - auth_date > max_age:
        raise HTTPException(status_code=401, detail="Telegram authorization expired.")

    user_raw = pairs.get("user", "")
    try:
        user = json.loads(user_raw) if user_raw else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="Invalid Telegram user.") from exc

    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user is missing.")

    return user


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, key: int) -> bool:
        now = time.monotonic()
        queue = self._hits[key]
        while queue and now - queue[0] > self.window_seconds:
            queue.popleft()
        if len(queue) >= self.limit:
            return False
        queue.append(now)
        return True


def safe_extract_mp3_zip(
    zip_path: Path,
    destination: Path,
    max_files: int,
    max_total_bytes: int,
) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total = 0

    with zipfile.ZipFile(zip_path) as archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]

        for info in infos:
            if len(extracted) >= max_files:
                break

            raw_name = info.filename.replace("\\", "/")
            path = Path(raw_name)

            if path.is_absolute() or ".." in path.parts:
                continue
            if path.suffix.lower() != ".mp3":
                continue
            if info.file_size <= 0:
                continue

            total += info.file_size
            if total > max_total_bytes:
                raise ValueError("ZIP unpacked size limit exceeded.")

            if info.compress_size > 0 and info.file_size / info.compress_size > 150:
                raise ValueError("Suspicious compression ratio.")

            target = destination / f"{len(extracted)+1:02d}_{path.name}"
            with archive.open(info, "r") as source, target.open("wb") as out:
                copied = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > info.file_size + 1024:
                        raise ValueError("ZIP member size mismatch.")
                    out.write(chunk)
            extracted.append(target)

    return extracted
