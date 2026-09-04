"""SQLite persistence.

Everything that must survive a container restart lives here: the download
queue with its per-piece progress, the metadata cache and the handful of
settings the WebUI can change.  The database file is stored in ``/config`` so
an Unraid appdata backup captures the full state.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS titles (
    title_id      TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    region        TEXT NOT NULL DEFAULT '',
    content_id    TEXT NOT NULL DEFAULT '',
    icon_url      TEXT NOT NULL DEFAULT '',
    payload       TEXT NOT NULL DEFAULT '{}',
    version_file_uri TEXT,
    fetched_at    REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_titles_name ON titles(name);

CREATE TABLE IF NOT EXISTS searches (
    query      TEXT PRIMARY KEY,
    payload    TEXT NOT NULL DEFAULT '[]',
    fetched_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS downloads (
    id                TEXT PRIMARY KEY,
    title_id          TEXT NOT NULL DEFAULT '',
    title_name        TEXT NOT NULL DEFAULT '',
    content_id        TEXT NOT NULL DEFAULT '',
    content_ver       TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL DEFAULT 'app',
    manifest_url      TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT 'manual',
    required_firmware TEXT,
    package_digest    TEXT,
    total_size        INTEGER NOT NULL DEFAULT 0,
    downloaded        INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'queued',
    error             TEXT,
    output_path       TEXT NOT NULL DEFAULT '',
    temp_path         TEXT NOT NULL DEFAULT '',
    retries           INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL DEFAULT 0,
    updated_at        REAL NOT NULL DEFAULT 0,
    started_at        REAL,
    finished_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);

CREATE TABLE IF NOT EXISTS download_pieces (
    download_id TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    url         TEXT NOT NULL,
    offset      INTEGER NOT NULL DEFAULT 0,
    size        INTEGER NOT NULL DEFAULT 0,
    hash_value  TEXT,
    hash_algo   TEXT,
    downloaded  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (download_id, idx),
    FOREIGN KEY (download_id) REFERENCES downloads(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

SCHEMA_VERSION = 1


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        row = await self.fetch_one("SELECT version FROM schema_info LIMIT 1")
        if row is None:
            await self.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
        log.info("Database ready", extra={"path": str(self.path)})

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # -- low level ----------------------------------------------------------
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        await self.conn.execute(sql, tuple(params))

    async def executemany(self, sql: str, params: Iterable[Iterable[Any]]) -> None:
        await self.conn.executemany(sql, [tuple(p) for p in params])

    async def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cursor:
            return await cursor.fetchone()

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> List[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cursor:
            return list(await cursor.fetchall())

    # -- settings -----------------------------------------------------------
    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self.fetch_one("SELECT value FROM app_settings WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def all_settings(self) -> Dict[str, str]:
        rows = await self.fetch_all("SELECT key, value FROM app_settings")
        return {row["key"]: row["value"] for row in rows}

    # -- metadata cache -----------------------------------------------------
    async def store_title(
        self,
        title_id: str,
        name: str,
        region: str,
        content_id: str,
        icon_url: str,
        payload: Dict[str, Any],
        version_file_uri: Optional[str] = None,
    ) -> None:
        await self.execute(
            """
            INSERT INTO titles(title_id, name, region, content_id, icon_url, payload,
                               version_file_uri, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(title_id) DO UPDATE SET
                name = excluded.name,
                region = excluded.region,
                content_id = excluded.content_id,
                icon_url = excluded.icon_url,
                payload = excluded.payload,
                version_file_uri = COALESCE(excluded.version_file_uri, titles.version_file_uri),
                fetched_at = excluded.fetched_at
            """,
            (title_id, name, region, content_id, icon_url, json.dumps(payload), version_file_uri, time.time()),
        )

    async def get_title(self, title_id: str) -> Optional[aiosqlite.Row]:
        return await self.fetch_one("SELECT * FROM titles WHERE title_id = ?", (title_id,))

    async def set_version_file_uri(self, title_id: str, uri: str) -> None:
        await self.execute(
            "INSERT INTO titles(title_id, version_file_uri, fetched_at) VALUES (?, ?, 0) "
            "ON CONFLICT(title_id) DO UPDATE SET version_file_uri = excluded.version_file_uri",
            (title_id, uri),
        )

    async def search_titles(self, query: str, limit: int = 50) -> List[aiosqlite.Row]:
        like = f"%{query.strip()}%"
        return await self.fetch_all(
            "SELECT title_id, name, region, icon_url FROM titles "
            "WHERE name LIKE ? COLLATE NOCASE OR title_id LIKE ? COLLATE NOCASE "
            "ORDER BY name LIMIT ?",
            (like, like, limit),
        )

    async def store_search(self, query: str, payload: List[Dict[str, Any]]) -> None:
        await self.execute(
            "INSERT INTO searches(query, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(query) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
            (query.strip().lower(), json.dumps(payload), time.time()),
        )

    async def get_search(self, query: str) -> Optional[aiosqlite.Row]:
        return await self.fetch_one("SELECT * FROM searches WHERE query = ?", (query.strip().lower(),))

    async def clear_cache(self) -> None:
        await self.execute("DELETE FROM searches")
        await self.execute("UPDATE titles SET fetched_at = 0")
