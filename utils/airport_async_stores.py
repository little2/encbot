"""Async airport stores sharing the database lifecycle from ``async_stores``.

This module is intentionally not wired into ``main_airport.py`` yet.  It gives
the airport's six tables one SQLite/PostgreSQL contract so the application can
be converted independently in a later deployment.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Protocol, runtime_checkable

from utils.async_stores import DatabaseStores, create_stores
from utils.invite_link_utils import SharedInviteLink


@runtime_checkable
class BatchStore(Protocol):
    async def upsert_channel_location(
        self, batch_id: str, channel_chat_id: int, channel_message_id: int,
        batch_content: str = "", tag: str = "", uploader_user_id: int = 0,
        no_forward: bool = False, if_spoiler: bool = False,
        anonymous: bool = True, flash_seconds: int = 0,
        valid_until: str = "99991231235959",
    ) -> None: ...
    async def update_discussion_location(
        self, channel_chat_id: int, channel_message_id: int,
        discussion_chat_id: int, discussion_message_id: int,
    ) -> bool: ...
    async def get(self, batch_id: str) -> dict | None: ...
    async def get_discussion_location(
        self, channel_chat_id: int, channel_message_id: int,
    ) -> tuple[int | None, int | None] | None: ...


@runtime_checkable
class BatchViewStore(Protocol):
    async def record(self, batch_id: str, user_id: int) -> bool: ...
    async def get_hot_batches(
        self, days: int = 7, limit: int = 10,
    ) -> list[tuple[str, str, int]]: ...


@runtime_checkable
class ReceivedMediaStore(Protocol):
    async def cleanup_stale_pending(self) -> int: ...
    async def claim(
        self, file_unique_id: str, file_id: str, file_type: str, user_id: int,
        source_chat_id: int, source_message_id: int,
    ) -> bool: ...
    async def is_accepted(self, file_unique_id: str) -> bool: ...
    async def claim_batch(
        self, items: list[dict], user_id: int, batch_id: str,
    ) -> list[str]: ...
    async def release_pending(
        self, file_unique_id: str, source_chat_id: int, source_message_id: int,
    ) -> bool: ...
    async def release_pending_many(self, file_unique_ids: list[str]) -> int: ...
    async def release_pending_batch(self, batch_id: str) -> int: ...
    async def accept_batch(self, file_unique_ids: list[str], batch_id: str) -> int: ...
    async def get_batch_ids(self, file_unique_ids: list[str]) -> dict[str, str | None]: ...
    async def get_media_by_batch_id(self, batch_id: str) -> list[dict[str, str | int]]: ...


@runtime_checkable
class SharedInviteLinkStore(Protocol):
    async def get(self, link_key: str) -> SharedInviteLink | None: ...
    async def save(
        self, link_key: str, chat_id: int, invite_link: str, name: str,
        created_at: int | None = None,
    ) -> SharedInviteLink: ...
    async def mark_validated(self, link_key: str) -> None: ...
    async def set_chat_id(self, link_key: str, chat_id: int) -> SharedInviteLink: ...
    async def delete(self, link_key: str) -> bool: ...


def _placeholders(start: int, count: int) -> str:
    return ", ".join(f"${index}" for index in range(start, start + count))


def _batch(row) -> dict | None:
    if row is None:
        return None
    return {
        "batch_id": str(row["batch_id"]),
        "channel_chat_id": int(row["channel_chat_id"]),
        "channel_message_id": int(row["channel_message_id"]),
        "discussion_chat_id": int(row["discussion_chat_id"]) if row["discussion_chat_id"] is not None else None,
        "discussion_message_id": int(row["discussion_message_id"]) if row["discussion_message_id"] is not None else None,
        "batch_content": str(row["batch_content"] or ""),
        "tag": str(row["tag"] or ""),
        "uploader_user_id": int(row["uploader_user_id"]),
        "no_forward": bool(row["no_forward"]),
        "if_spoiler": bool(row["if_spoiler"]),
        "anonymous": bool(row["anonymous"]),
        "flash_seconds": int(row["flash_seconds"]),
        "valid_until": str(row["valid_until"]),
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
    }


def _invite(row) -> SharedInviteLink | None:
    if row is None:
        return None
    return SharedInviteLink(
        link_key=str(row["link_key"]), chat_id=int(row["chat_id"]),
        invite_link=str(row["invite_link"]), name=str(row["name"]),
        created_at=int(row["created_at"]), validated_at=int(row["validated_at"]),
    )


class _BatchStore:
    def __init__(self, database):
        self.database = database

    async def upsert_channel_location(
        self, batch_id, channel_chat_id, channel_message_id, batch_content="",
        tag="", uploader_user_id=0, no_forward=False, if_spoiler=False,
        anonymous=True, flash_seconds=0, valid_until="99991231235959",
    ):
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            raise ValueError("batch_id is required")
        now = int(time.time())
        async with self.database.transaction(write=True) as conn:
            await conn.execute("""
                INSERT INTO batch (
                    batch_id, channel_chat_id, channel_message_id,
                    discussion_chat_id, discussion_message_id, batch_content,
                    tag, uploader_user_id, no_forward, if_spoiler, anonymous,
                    flash_seconds, valid_until, created_at, updated_at
                ) VALUES ($1, $2, $3, NULL, NULL, $4, $5, $6, $7, $8,
                          $9, $10, $11, $12, $12)
                ON CONFLICT(batch_id) DO UPDATE SET
                    discussion_chat_id = CASE
                        WHEN batch.channel_chat_id = excluded.channel_chat_id
                         AND batch.channel_message_id = excluded.channel_message_id
                        THEN batch.discussion_chat_id ELSE NULL END,
                    discussion_message_id = CASE
                        WHEN batch.channel_chat_id = excluded.channel_chat_id
                         AND batch.channel_message_id = excluded.channel_message_id
                        THEN batch.discussion_message_id ELSE NULL END,
                    channel_chat_id = excluded.channel_chat_id,
                    channel_message_id = excluded.channel_message_id,
                    batch_content = excluded.batch_content, tag = excluded.tag,
                    uploader_user_id = excluded.uploader_user_id,
                    no_forward = excluded.no_forward, if_spoiler = excluded.if_spoiler,
                    anonymous = excluded.anonymous, flash_seconds = excluded.flash_seconds,
                    valid_until = excluded.valid_until, updated_at = excluded.updated_at
            """, batch_id, int(channel_chat_id), int(channel_message_id),
                 str(batch_content or "").strip(), str(tag or "").strip(),
                 int(uploader_user_id), int(bool(no_forward)), int(bool(if_spoiler)),
                 int(bool(anonymous)), max(0, int(flash_seconds)),
                 str(valid_until or "99991231235959"), now)

    async def update_discussion_location(
        self, channel_chat_id, channel_message_id,
        discussion_chat_id, discussion_message_id,
    ) -> bool:
        async with self.database.transaction(write=True) as conn:
            row = await conn.fetchrow("""
                UPDATE batch SET discussion_chat_id = $1,
                    discussion_message_id = $2, updated_at = $3
                WHERE channel_chat_id = $4 AND channel_message_id = $5
                RETURNING batch_id
            """, int(discussion_chat_id), int(discussion_message_id), int(time.time()),
                 int(channel_chat_id), int(channel_message_id))
            return row is not None

    async def get(self, batch_id: str) -> dict | None:
        async with self.database.transaction() as conn:
            return _batch(await conn.fetchrow(
                "SELECT * FROM batch WHERE batch_id = $1", str(batch_id or "").strip()
            ))

    async def get_discussion_location(self, channel_chat_id, channel_message_id):
        async with self.database.transaction() as conn:
            row = await conn.fetchrow("""
                SELECT discussion_chat_id, discussion_message_id FROM batch
                WHERE channel_chat_id = $1 AND channel_message_id = $2
            """, int(channel_chat_id), int(channel_message_id))
            if row is None:
                return None
            return (
                int(row["discussion_chat_id"]) if row["discussion_chat_id"] is not None else None,
                int(row["discussion_message_id"]) if row["discussion_message_id"] is not None else None,
            )


class _BatchViewStore:
    def __init__(self, database):
        self.database = database

    async def record(self, batch_id: str, user_id: int) -> bool:
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            raise ValueError("batch_id is required")
        if int(user_id) <= 0:
            raise ValueError("user_id must be positive")
        async with self.database.transaction(write=True) as conn:
            row = await conn.fetchrow("""
                INSERT INTO batch_view (batch_id, user_id, timestamp)
                VALUES ($1, $2, $3) ON CONFLICT(batch_id, user_id) DO NOTHING
                RETURNING batch_id
            """, batch_id, int(user_id), int(time.time()))
            return row is not None

    async def get_hot_batches(self, days=7, limit=10):
        cutoff = int(time.time()) - max(1, int(days)) * 24 * 60 * 60
        limit = min(100, max(1, int(limit)))
        async with self.database.transaction() as conn:
            rows = await conn.fetch("""
                SELECT batch_view.batch_id, COALESCE(batch.batch_content, '') AS batch_content,
                       COUNT(*) AS view_count
                FROM batch_view INNER JOIN batch ON batch.batch_id = batch_view.batch_id
                WHERE batch_view.timestamp >= $1
                GROUP BY batch_view.batch_id, batch.batch_content
                ORDER BY view_count DESC, batch_view.batch_id ASC LIMIT $2
            """, cutoff, limit)
            return [(str(row["batch_id"]), str(row["batch_content"]), int(row["view_count"]))
                    for row in rows]


class _ClaimConflict(Exception):
    def __init__(self, conflicts):
        super().__init__("media already claimed")
        self.conflicts = conflicts


class _ReceivedMediaStore:
    PENDING_RETENTION_SECONDS = 24 * 60 * 60

    def __init__(self, database):
        self.database = database

    async def cleanup_stale_pending(self) -> int:
        cutoff = int(time.time()) - self.PENDING_RETENTION_SECONDS
        async with self.database.transaction(write=True) as conn:
            rows = await conn.fetch("""
                DELETE FROM received_media WHERE status = 'pending' AND created_at < $1
                RETURNING file_unique_id
            """, cutoff)
            return len(rows)

    async def claim(self, file_unique_id, file_id, file_type, user_id,
                    source_chat_id, source_message_id) -> bool:
        unique_id, reusable_file_id = str(file_unique_id or "").strip(), str(file_id or "").strip()
        if not unique_id:
            raise ValueError("file_unique_id is required")
        if not reusable_file_id:
            raise ValueError("file_id is required")
        async with self.database.transaction(write=True) as conn:
            row = await conn.fetchrow("""
                INSERT INTO received_media (
                    file_unique_id, file_id, file_type, first_user_id,
                    source_chat_id, source_message_id, status, created_at, accepted_at
                ) VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, NULL)
                ON CONFLICT(file_unique_id) DO NOTHING RETURNING file_unique_id
            """, unique_id, reusable_file_id, str(file_type), int(user_id),
                 int(source_chat_id), int(source_message_id), int(time.time()))
            if row is None:
                await conn.execute("""
                    UPDATE received_media SET file_id = $1
                    WHERE file_unique_id = $2 AND file_id = ''
                """, reusable_file_id, unique_id)
            return row is not None

    async def is_accepted(self, file_unique_id: str) -> bool:
        async with self.database.transaction() as conn:
            row = await conn.fetchrow("""
                SELECT 1 AS found FROM received_media
                WHERE file_unique_id = $1 AND status = 'accepted' LIMIT 1
            """, str(file_unique_id))
            return row is not None

    @staticmethod
    def _batch_rows(items, user_id, batch_id):
        rows, seen = [], set()
        for item in items:
            unique_id = str(item.get("file_unique_id", "")).strip()
            if not unique_id or unique_id in seen:
                continue
            seen.add(unique_id)
            rows.append((
                unique_id, str(item.get("file_id", "")), str(item.get("file_type", "")),
                max(0, int(item.get("duration", 0) or 0)), int(user_id),
                int(item.get("source_chat_id", 0)), int(item.get("source_message_id", 0)),
                batch_id, str(item.get("thumb_file_id", "") or "") or None,
                str(item.get("thumb_file_unique_id", "") or "") or None,
                str(item.get("thumb_phash", "") or "") or None, int(time.time()),
                "pending",
            ))
        return rows

    async def claim_batch(self, items, user_id, batch_id):
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            raise ValueError("batch_id is required")
        if len(batch_id) > 64:
            raise ValueError("batch_id cannot exceed 64 characters")
        rows = self._batch_rows(items, user_id, batch_id)
        if not rows:
            raise ValueError("media items are required")
        unique_ids = [row[0] for row in rows]
        try:
            async with self.database.transaction(write=True) as conn:
                ids_sql = _placeholders(1, len(unique_ids))
                await conn.execute(f"""
                    DELETE FROM received_media WHERE status = 'pending'
                      AND batch_id IS NULL AND file_unique_id IN ({ids_sql})
                """, *unique_ids)

                values, params = [], []
                for row in rows:
                    start = len(params) + 1
                    values.append(
                        f"({_placeholders(start, 13)}, NULL)"
                    )
                    params.extend(row)
                inserted = await conn.fetch(f"""
                    INSERT INTO received_media (
                        file_unique_id, file_id, file_type, duration, first_user_id,
                        source_chat_id, source_message_id, batch_id, thumb_file_id,
                        thumb_file_unique_id, thumb_phash, created_at, status, accepted_at
                    ) VALUES {', '.join(values)}
                    ON CONFLICT(file_unique_id) DO NOTHING RETURNING file_unique_id
                """, *params)
                inserted_ids = {str(row["file_unique_id"]) for row in inserted}
                conflicts = [value for value in unique_ids if value not in inserted_ids]
                if conflicts:
                    raise _ClaimConflict(conflicts)
        except _ClaimConflict as exc:
            return exc.conflicts
        return []

    async def release_pending(self, file_unique_id, source_chat_id, source_message_id):
        async with self.database.transaction(write=True) as conn:
            row = await conn.fetchrow("""
                DELETE FROM received_media WHERE file_unique_id = $1
                  AND source_chat_id = $2 AND source_message_id = $3
                  AND status = 'pending' RETURNING file_unique_id
            """, str(file_unique_id), int(source_chat_id), int(source_message_id))
            return row is not None

    async def release_pending_many(self, file_unique_ids):
        unique_ids = list(dict.fromkeys(str(value).strip() for value in file_unique_ids if str(value).strip()))
        if not unique_ids:
            return 0
        async with self.database.transaction(write=True) as conn:
            rows = await conn.fetch(f"""
                DELETE FROM received_media WHERE status = 'pending'
                  AND file_unique_id IN ({_placeholders(1, len(unique_ids))})
                RETURNING file_unique_id
            """, *unique_ids)
            return len(rows)

    async def release_pending_batch(self, batch_id):
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            return 0
        async with self.database.transaction(write=True) as conn:
            rows = await conn.fetch("""
                DELETE FROM received_media WHERE status = 'pending' AND batch_id = $1
                RETURNING file_unique_id
            """, batch_id)
            return len(rows)

    async def accept_batch(self, file_unique_ids, batch_id):
        unique_ids = list(dict.fromkeys(str(value).strip() for value in file_unique_ids if str(value).strip()))
        if not unique_ids:
            return 0
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            raise ValueError("batch_id is required")
        if len(batch_id) > 64:
            raise ValueError("batch_id cannot exceed 64 characters")
        params = [int(time.time()), batch_id, *unique_ids]
        async with self.database.transaction(write=True) as conn:
            rows = await conn.fetch(f"""
                UPDATE received_media SET status = 'accepted', accepted_at = $1, batch_id = $2
                WHERE status = 'pending' AND batch_id = $2
                  AND file_unique_id IN ({_placeholders(3, len(unique_ids))})
                RETURNING file_unique_id
            """, *params)
            return len(rows)

    async def get_batch_ids(self, file_unique_ids):
        unique_ids = list(dict.fromkeys(str(value).strip() for value in file_unique_ids if str(value).strip()))
        if not unique_ids:
            return {}
        async with self.database.transaction() as conn:
            rows = await conn.fetch(f"""
                SELECT file_unique_id, batch_id FROM received_media
                WHERE file_unique_id IN ({_placeholders(1, len(unique_ids))})
            """, *unique_ids)
            return {str(row["file_unique_id"]): str(row["batch_id"]) if row["batch_id"] is not None else None
                    for row in rows}

    async def get_media_by_batch_id(self, batch_id):
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            return []
        async with self.database.transaction() as conn:
            rows = await conn.fetch("""
                SELECT file_id, file_unique_id, file_type, duration,
                       thumb_file_id, thumb_file_unique_id
                FROM received_media WHERE batch_id = $1
                  AND status IN ('pending', 'accepted') AND file_id <> ''
                ORDER BY source_message_id, file_unique_id
            """, batch_id)
            return [{
                "file_id": str(row["file_id"]), "file_unique_id": str(row["file_unique_id"]),
                "file_type": str(row["file_type"]), "duration": max(0, int(row["duration"] or 0)),
                "thumb_file_id": str(row["thumb_file_id"] or ""),
                "thumb_file_unique_id": str(row["thumb_file_unique_id"] or ""),
            } for row in rows]


class _SharedInviteLinkStore:
    def __init__(self, database):
        self.database = database

    async def get(self, link_key):
        async with self.database.transaction() as conn:
            return _invite(await conn.fetchrow(
                "SELECT * FROM shared_invite_link WHERE link_key = $1", str(link_key)
            ))

    async def save(self, link_key, chat_id, invite_link, name, created_at=None):
        now = int(time.time())
        async with self.database.transaction(write=True) as conn:
            return _invite(await conn.fetchrow("""
                INSERT INTO shared_invite_link (
                    link_key, chat_id, invite_link, name, created_at, validated_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT(link_key) DO UPDATE SET chat_id = excluded.chat_id,
                    invite_link = excluded.invite_link, name = excluded.name,
                    created_at = excluded.created_at, validated_at = excluded.validated_at
                RETURNING *
            """, str(link_key), int(chat_id), str(invite_link), str(name),
                 int(created_at or now), now))

    async def mark_validated(self, link_key):
        async with self.database.transaction(write=True) as conn:
            await conn.execute("""
                UPDATE shared_invite_link SET validated_at = $1 WHERE link_key = $2
            """, int(time.time()), str(link_key))

    async def set_chat_id(self, link_key, chat_id):
        now = int(time.time())
        async with self.database.transaction(write=True) as conn:
            row = await conn.fetchrow("""
                INSERT INTO shared_invite_link (
                    link_key, chat_id, invite_link, name, created_at, validated_at
                ) VALUES ($1, $2, '', $1, $3, $3)
                ON CONFLICT(link_key) DO UPDATE SET chat_id = excluded.chat_id,
                    validated_at = excluded.validated_at RETURNING *
            """, str(link_key), int(chat_id), now)
            result = _invite(row)
            if result is None:
                raise RuntimeError(f"failed to set chat_id for link_key={link_key!r}")
            return result

    async def delete(self, link_key):
        async with self.database.transaction(write=True) as conn:
            row = await conn.fetchrow(
                "DELETE FROM shared_invite_link WHERE link_key = $1 RETURNING link_key",
                str(link_key),
            )
            return row is not None


@dataclass
class AirportDatabaseStores(DatabaseStores):
    batches: BatchStore
    batch_views: BatchViewStore
    received_media: ReceivedMediaStore
    shared_invite_links: SharedInviteLinkStore

    async def _initialize_restored_schema(self, conn) -> None:
        await super()._initialize_restored_schema(conn)
        await _initialize_airport_schema_on_connection(conn, self.backend)


async def _ensure_columns(conn, backend, table, columns):
    if backend == "sqlite":
        existing = {row["name"] for row in await conn.fetch(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    else:
        for name, definition in columns.items():
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {definition}")


async def _initialize_airport_schema_on_connection(conn, backend):
    await conn.execute("""
            CREATE TABLE IF NOT EXISTS batch (
                batch_id TEXT PRIMARY KEY, channel_chat_id BIGINT NOT NULL,
                channel_message_id BIGINT NOT NULL, discussion_chat_id BIGINT,
                discussion_message_id BIGINT, batch_content TEXT, tag TEXT,
                uploader_user_id BIGINT NOT NULL DEFAULT 0,
                no_forward BIGINT NOT NULL DEFAULT 0, if_spoiler BIGINT NOT NULL DEFAULT 0,
                anonymous BIGINT NOT NULL DEFAULT 1, flash_seconds BIGINT NOT NULL DEFAULT 0,
                valid_until TEXT NOT NULL DEFAULT '99991231235959',
                created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
                UNIQUE(channel_chat_id, channel_message_id)
            )
        """)
    await _ensure_columns(conn, backend, "batch", {
        "batch_content": "TEXT", "tag": "TEXT",
        "uploader_user_id": "BIGINT NOT NULL DEFAULT 0",
        "no_forward": "BIGINT NOT NULL DEFAULT 0",
        "if_spoiler": "BIGINT NOT NULL DEFAULT 0",
        "anonymous": "BIGINT NOT NULL DEFAULT 1",
        "flash_seconds": "BIGINT NOT NULL DEFAULT 0",
        "valid_until": "TEXT NOT NULL DEFAULT '99991231235959'",
    })
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_batch_channel_message ON batch(channel_chat_id, channel_message_id)")
    await conn.execute("""
            CREATE TABLE IF NOT EXISTS batch_view (
                batch_id TEXT NOT NULL, user_id BIGINT NOT NULL, timestamp BIGINT NOT NULL,
                PRIMARY KEY(batch_id, user_id)
            )
        """)
    await conn.execute("""
            CREATE TABLE IF NOT EXISTS received_media (
                file_unique_id TEXT PRIMARY KEY, file_id TEXT NOT NULL DEFAULT '',
                file_type TEXT NOT NULL, duration BIGINT NOT NULL DEFAULT 0,
                first_user_id BIGINT NOT NULL, source_chat_id BIGINT NOT NULL,
                source_message_id BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'accepted')),
                batch_id TEXT, thumb_file_id TEXT, thumb_file_unique_id TEXT,
                thumb_phash TEXT, created_at BIGINT NOT NULL, accepted_at BIGINT
            )
        """)
    await _ensure_columns(conn, backend, "received_media", {
        "file_id": "TEXT NOT NULL DEFAULT ''", "duration": "BIGINT NOT NULL DEFAULT 0",
        "batch_id": "TEXT", "accepted_at": "BIGINT", "thumb_file_id": "TEXT",
        "thumb_file_unique_id": "TEXT", "thumb_phash": "TEXT",
    })
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_received_media_batch_id ON received_media(batch_id)")
    await conn.execute("""
            CREATE TABLE IF NOT EXISTS shared_invite_link (
                link_key TEXT PRIMARY KEY, chat_id BIGINT NOT NULL,
                invite_link TEXT NOT NULL, name TEXT NOT NULL,
                created_at BIGINT NOT NULL, validated_at BIGINT NOT NULL
            )
        """)


async def _initialize_airport_schema(database):
    async with database.transaction(write=True) as conn:
        await _initialize_airport_schema_on_connection(conn, database.backend)


async def create_airport_stores(*, backend=None, sqlite_path=None, database_url=None):
    """Create all six airport stores without changing ``main_airport.py``."""
    core = await create_stores(
        backend=backend, sqlite_path=sqlite_path, database_url=database_url
    )
    try:
        await _initialize_airport_schema(core._database)
        stores = AirportDatabaseStores(
            core.users, core.blacklist, core.backend, core._database,
            _BatchStore(core._database), _BatchViewStore(core._database),
            _ReceivedMediaStore(core._database), _SharedInviteLinkStore(core._database),
        )
        await stores.received_media.cleanup_stale_pending()
        return stores
    except BaseException:
        await core.close()
        raise
