
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import config

try:
    import psycopg
except Exception:  # optional until DATABASE_URL exists
    psycopg = None


class StudioDB:
    def __init__(self) -> None:
        self.url = config.database_url
        self._sqlite_path = Path("/tmp/tagphonk-studio-v4.sqlite3")
        self._lock = threading.Lock()

    @property
    def is_postgres(self) -> bool:
        return bool(self.url.startswith(("postgres://", "postgresql://")) and psycopg)

    def _connect(self):
        if self.is_postgres:
            return psycopg.connect(self.url)
        con = sqlite3.connect(self._sqlite_path)
        con.row_factory = sqlite3.Row
        return con

    def init(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS studio_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                language_code TEXT,
                settings_json TEXT NOT NULL DEFAULT '{}',
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS studio_projects (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                telegram_file_id TEXT,
                filename TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                album TEXT,
                duration_seconds INTEGER DEFAULT 0,
                size_bytes BIGINT DEFAULT 0,
                snapshot_json TEXT NOT NULL DEFAULT '{}',
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL
            )
            """ if self.is_postgres else """
            CREATE TABLE IF NOT EXISTS studio_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                telegram_file_id TEXT,
                filename TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                album TEXT,
                duration_seconds INTEGER DEFAULT 0,
                size_bytes INTEGER DEFAULT 0,
                snapshot_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS studio_events (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id BIGINT,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at BIGINT NOT NULL
            )
            """ if self.is_postgres else """
            CREATE TABLE IF NOT EXISTS studio_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS studio_presets (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                preset_json TEXT NOT NULL,
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL
            )
            """ if self.is_postgres else """
            CREATE TABLE IF NOT EXISTS studio_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                preset_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """,
        ]
        with self._lock:
            con = self._connect()
            try:
                cur = con.cursor()
                for statement in statements:
                    cur.execute(statement)
                con.commit()
            finally:
                con.close()

    def upsert_user(self, user: dict[str, Any]) -> None:
        now = int(time.time())
        user_id = int(user["id"])
        username = user.get("username") or ""
        first_name = user.get("first_name") or ""
        language_code = user.get("language_code") or ""
        with self._lock:
            con = self._connect()
            try:
                cur = con.cursor()
                if self.is_postgres:
                    cur.execute(
                        """
                        INSERT INTO studio_users
                            (user_id, username, first_name, language_code, created_at, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (user_id) DO UPDATE SET
                            username=EXCLUDED.username,
                            first_name=EXCLUDED.first_name,
                            language_code=EXCLUDED.language_code,
                            updated_at=EXCLUDED.updated_at
                        """,
                        (user_id, username, first_name, language_code, now, now),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO studio_users
                            (user_id, username, first_name, language_code, created_at, updated_at)
                        VALUES (?,?,?,?,?,?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            username=excluded.username,
                            first_name=excluded.first_name,
                            language_code=excluded.language_code,
                            updated_at=excluded.updated_at
                        """,
                        (user_id, username, first_name, language_code, now, now),
                    )
                con.commit()
            finally:
                con.close()

    def add_project(self, payload: dict[str, Any]) -> None:
        now = int(time.time())
        args = (
            int(payload["user_id"]),
            payload.get("telegram_file_id") or "",
            payload.get("filename") or "track.mp3",
            payload.get("title") or "",
            payload.get("artist") or "",
            payload.get("album") or "",
            int(payload.get("duration_seconds") or 0),
            int(payload.get("size_bytes") or 0),
            json.dumps(payload.get("snapshot") or {}, ensure_ascii=False),
            now,
            now,
        )
        with self._lock:
            con = self._connect()
            try:
                cur = con.cursor()
                placeholders = "%s" if self.is_postgres else "?"
                sql = f"""
                    INSERT INTO studio_projects
                    (user_id, telegram_file_id, filename, title, artist, album,
                     duration_seconds, size_bytes, snapshot_json, created_at, updated_at)
                    VALUES ({','.join([placeholders]*11)})
                """
                cur.execute(sql, args)
                con.commit()
            finally:
                con.close()

    def get_project(self, user_id: int, project_id: int) -> dict[str, Any] | None:
        with self._lock:
            con = self._connect()
            try:
                cur = con.cursor()
                query = (
                    "SELECT id,user_id,telegram_file_id,filename,title,artist,album,"
                    " duration_seconds,size_bytes,snapshot_json,created_at,updated_at"
                    " FROM studio_projects WHERE user_id=%s AND id=%s"
                    if self.is_postgres
                    else "SELECT id,user_id,telegram_file_id,filename,title,artist,album,"
                         " duration_seconds,size_bytes,snapshot_json,created_at,updated_at"
                         " FROM studio_projects WHERE user_id=? AND id=?"
                )
                cur.execute(query, (user_id, project_id))
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                result = dict(zip(cols, row))
                try:
                    result["snapshot"] = json.loads(result.pop("snapshot_json") or "{}")
                except Exception:
                    result["snapshot"] = {}
                    result.pop("snapshot_json", None)
                return result
            finally:
                con.close()

    def event(self, event_type: str, user_id: int | None = None, details: dict | None = None) -> None:
        now = int(time.time())
        details_json = json.dumps(details or {}, ensure_ascii=False)
        with self._lock:
            con = self._connect()
            try:
                cur = con.cursor()
                if self.is_postgres:
                    cur.execute(
                        "INSERT INTO studio_events (user_id,event_type,details_json,created_at) VALUES (%s,%s,%s,%s)",
                        (user_id, event_type, details_json, now),
                    )
                else:
                    cur.execute(
                        "INSERT INTO studio_events (user_id,event_type,details_json,created_at) VALUES (?,?,?,?)",
                        (user_id, event_type, details_json, now),
                    )
                con.commit()
            finally:
                con.close()

    def recent_projects(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            con = self._connect()
            try:
                cur = con.cursor()
                if self.is_postgres:
                    cur.execute(
                        """
                        SELECT id, filename, title, artist, album, duration_seconds,
                               size_bytes, created_at, updated_at
                        FROM studio_projects
                        WHERE user_id=%s
                        ORDER BY updated_at DESC
                        LIMIT %s
                        """,
                        (user_id, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, filename, title, artist, album, duration_seconds,
                               size_bytes, created_at, updated_at
                        FROM studio_projects
                        WHERE user_id=?
                        ORDER BY updated_at DESC
                        LIMIT ?
                        """,
                        (user_id, limit),
                    )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            finally:
                con.close()

    def stats(self, user_id: int | None = None) -> dict[str, int]:
        with self._lock:
            con = self._connect()
            try:
                cur = con.cursor()
                if user_id is None:
                    cur.execute("SELECT COUNT(*) FROM studio_users")
                    users = int(cur.fetchone()[0])
                    cur.execute("SELECT COUNT(*) FROM studio_projects")
                    projects = int(cur.fetchone()[0])
                    cur.execute("SELECT COUNT(*) FROM studio_events")
                    events = int(cur.fetchone()[0])
                    cur.execute("SELECT COALESCE(SUM(size_bytes),0) FROM studio_projects")
                    bytes_processed = int(cur.fetchone()[0] or 0)
                    return {
                        "users": users,
                        "projects": projects,
                        "events": events,
                        "bytes_processed": bytes_processed,
                    }

                if self.is_postgres:
                    cur.execute("SELECT COUNT(*) FROM studio_projects WHERE user_id=%s", (user_id,))
                else:
                    cur.execute("SELECT COUNT(*) FROM studio_projects WHERE user_id=?", (user_id,))
                projects = int(cur.fetchone()[0])

                if self.is_postgres:
                    cur.execute(
                        "SELECT COALESCE(SUM(size_bytes),0) FROM studio_projects WHERE user_id=%s",
                        (user_id,),
                    )
                else:
                    cur.execute(
                        "SELECT COALESCE(SUM(size_bytes),0) FROM studio_projects WHERE user_id=?",
                        (user_id,),
                    )
                bytes_processed = int(cur.fetchone()[0] or 0)
                return {"projects": projects, "bytes_processed": bytes_processed}
            finally:
                con.close()

    def get_settings(self, user_id: int) -> dict[str, Any]:
        with self._lock:
            con = self._connect()
            try:
                cur = con.cursor()
                if self.is_postgres:
                    cur.execute("SELECT settings_json FROM studio_users WHERE user_id=%s", (user_id,))
                else:
                    cur.execute("SELECT settings_json FROM studio_users WHERE user_id=?", (user_id,))
                row = cur.fetchone()
                if not row:
                    return {}
                try:
                    return json.loads(row[0] or "{}")
                except Exception:
                    return {}
            finally:
                con.close()

    def set_settings(self, user_id: int, settings: dict[str, Any]) -> None:
        payload = json.dumps(settings, ensure_ascii=False)
        now = int(time.time())
        with self._lock:
            con = self._connect()
            try:
                cur = con.cursor()
                if self.is_postgres:
                    cur.execute(
                        "UPDATE studio_users SET settings_json=%s, updated_at=%s WHERE user_id=%s",
                        (payload, now, user_id),
                    )
                else:
                    cur.execute(
                        "UPDATE studio_users SET settings_json=?, updated_at=? WHERE user_id=?",
                        (payload, now, user_id),
                    )
                con.commit()
            finally:
                con.close()

    def presets(self, user_id: int) -> list[dict[str, Any]]:
        with self._lock:
            con = self._connect()
            try:
                cur = con.cursor()
                if self.is_postgres:
                    cur.execute(
                        "SELECT id,name,preset_json,updated_at FROM studio_presets WHERE user_id=%s ORDER BY updated_at DESC",
                        (user_id,),
                    )
                else:
                    cur.execute(
                        "SELECT id,name,preset_json,updated_at FROM studio_presets WHERE user_id=? ORDER BY updated_at DESC",
                        (user_id,),
                    )
                result = []
                for row in cur.fetchall():
                    try:
                        data = json.loads(row[2])
                    except Exception:
                        data = {}
                    result.append({"id": row[0], "name": row[1], "preset": data, "updated_at": row[3]})
                return result
            finally:
                con.close()

    def save_preset(self, user_id: int, name: str, preset: dict[str, Any]) -> None:
        now = int(time.time())
        payload = json.dumps(preset, ensure_ascii=False)
        with self._lock:
            con = self._connect()
            try:
                cur = con.cursor()
                if self.is_postgres:
                    cur.execute(
                        "INSERT INTO studio_presets (user_id,name,preset_json,created_at,updated_at) VALUES (%s,%s,%s,%s,%s)",
                        (user_id, name[:80], payload, now, now),
                    )
                else:
                    cur.execute(
                        "INSERT INTO studio_presets (user_id,name,preset_json,created_at,updated_at) VALUES (?,?,?,?,?)",
                        (user_id, name[:80], payload, now, now),
                    )
                con.commit()
            finally:
                con.close()


db = StudioDB()
