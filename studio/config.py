
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class StudioConfig:
    public_url: str = os.environ.get("PUBLIC_URL", "").rstrip("/")
    database_url: str = os.environ.get("DATABASE_URL", "").strip()
    redis_url: str = os.environ.get("REDIS_URL", "").strip()
    acoustid_api_key: str = os.environ.get("ACOUSTID_API_KEY", "").strip()
    admin_key: str = os.environ.get("ADMIN_KEY", "").strip()
    admin_user_ids_raw: str = os.environ.get("ADMIN_USER_IDS", "").strip()
    max_file_bytes: int = int(os.environ.get("MAX_FILE_BYTES", str(20 * 1024 * 1024)))
    max_batch_files: int = int(os.environ.get("MAX_BATCH_FILES", "10"))
    max_batch_unpacked: int = int(os.environ.get("MAX_BATCH_UNPACKED", str(45 * 1024 * 1024)))

    @property
    def admin_user_ids(self) -> set[int]:
        result: set[int] = set()
        for item in self.admin_user_ids_raw.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                result.add(int(item))
            except ValueError:
                continue
        return result


config = StudioConfig()
