"""Async user/blacklist stores. Existing synchronous stores remain compatible.

Use create_stores() once during application startup and close the returned bundle
on shutdown. PostgreSQL uses the process-wide PGPool; one bundle owns its lifetime.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Protocol, runtime_checkable

from config import MAX_HP_CAPACITY_MINUTES
from utils.blacklist_utils import BlacklistEntry
from utils.user_utils import UserExpire


@runtime_checkable
class UserExpireStore(Protocol):
    async def get(self, user_id: int) -> UserExpire | None: ...
    async def update(self, user_id: int, expire_timestamp: int,
                     group_message_timestamp: int | None = None) -> UserExpire: ...
    async def remove(self, user_id: int) -> None: ...
    async def is_valid(self, user_id: int) -> bool: ...
    async def count(self) -> int: ...
    async def list_users(self) -> dict[int, UserExpire]: ...
    async def extend_minutes(self, user_id: int, minutes: int,
                             group_message_timestamp: int | None = None) -> UserExpire: ...
    async def consume_minutes(self, user_id: int, minutes: int) -> UserExpire | None: ...
    async def refund_minutes(self, user_id: int, minutes: int) -> UserExpire | None: ...


@runtime_checkable
class BlacklistStore(Protocol):
    async def is_blocked(self, user_id: int) -> bool: ...
    async def ban(self, user_id: int, reason: str, created_by: int,
                  expires_at: int = 0) -> BlacklistEntry: ...
    async def unban(self, user_id: int) -> bool: ...
    async def get(self, user_id: int) -> BlacklistEntry | None: ...
    async def list_page(self, page: int = 1,
                        page_size: int = 10) -> tuple[list[BlacklistEntry], int]: ...
    async def list_by_reason_prefix(self, reason_prefix: str) -> list[BlacklistEntry]: ...


class _SQLiteConnection:
    """All connection access stays on its owning worker, including cancellation."""

    def __init__(self, path: Path):
        self.path = path
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="user-stores-sqlite")
        self.connection: sqlite3.Connection | None = None

    async def _run(self, fn):
        future = asyncio.get_running_loop().run_in_executor(self.executor, fn)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Do not release the transaction lock until this statement finishes.
            try:
                await asyncio.shield(future)
            except Exception:
                pass
            raise

    async def open(self):
        def work():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, isolation_level=None)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.execute("PRAGMA busy_timeout=5000")
        await self._run(work)

    async def _query(self, sql, params, fetch):
        # Only internal SQL templates use this adapter; never interpolate values.
        sql = re.sub(r"\$(\d+)", r"?\1", sql)

        def work():
            cursor = self.connection.execute(sql, params)
            try:
                if fetch:
                    return [dict(row) for row in cursor.fetchall()]
                return None
            finally:
                cursor.close()
        return await self._run(work)

    async def execute(self, sql, *params):
        return await self._query(sql, params, False)

    async def fetch(self, sql, *params):
        return await self._query(sql, params, True)

    async def fetchrow(self, sql, *params):
        rows = await self.fetch(sql, *params)
        return rows[0] if rows else None

    async def close(self):
        try:
            if self.connection is not None:
                await self._run(self.connection.close)
        finally:
            self.executor.shutdown(wait=False)

    async def rollback(self):
        # sqlite3 rollback is a no-op when BEGIN failed or COMMIT completed.
        await self._run(self.connection.rollback)


class _SQLiteDatabase:
    backend = "sqlite"

    def __init__(self, path: Path):
        self.conn = _SQLiteConnection(path)
        self.lock = asyncio.Lock()
        self.closed = False

    @asynccontextmanager
    async def transaction(self, *, write=False):
        async with self.lock:
            if self.closed:
                raise RuntimeError("Database stores are closed")
            try:
                await self.conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield self.conn
                await self.conn.execute("COMMIT")
            except BaseException:
                await self.conn.rollback()
                raise

    async def close(self):
        async with self.lock:
            if not self.closed:
                self.closed = True
                await self.conn.close()


class _PostgresDatabase:
    backend = "postgresql"

    def __init__(self, pool):
        self.pool = pool
        self.closed = False

    @asynccontextmanager
    async def transaction(self, *, write=False):
        if self.closed:
            raise RuntimeError("Database stores are closed")
        pool = await self.pool.ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    async def close(self):
        if not self.closed:
            self.closed = True
            await self.pool.close()


def _user(row) -> UserExpire | None:
    if row is None:
        return None
    return UserExpire(int(row["expire_timestamp"]), int(row["update_timestamp"]),
                      int(row["group_message_timestamp"]))


def _blacklist_entry(row) -> BlacklistEntry:
    return BlacklistEntry(int(row["user_id"]), str(row["reason"]),
                          int(row["created_by"]), int(row["created_at"]),
                          int(row["expires_at"]))


class _UserExpireStore:
    def __init__(self, database):
        self.database = database

    async def get(self, user_id: int) -> UserExpire | None:
        async with self.database.transaction() as conn:
            return _user(await conn.fetchrow("SELECT * FROM user_expire WHERE user_id = $1", int(user_id)))

    async def _update(self, conn, user_id, expire_timestamp, group_message_timestamp):
        return _user(await conn.fetchrow("""
            INSERT INTO user_expire (user_id, expire_timestamp, update_timestamp, group_message_timestamp)
            VALUES ($1, $2, $3, COALESCE(CAST($4 AS BIGINT), 0))
            ON CONFLICT(user_id) DO UPDATE SET
                expire_timestamp = excluded.expire_timestamp,
                update_timestamp = excluded.update_timestamp,
                group_message_timestamp = COALESCE(CAST($4 AS BIGINT), user_expire.group_message_timestamp)
            RETURNING *
        """, int(user_id), int(expire_timestamp), int(time.time()), group_message_timestamp))

    async def update(self, user_id: int, expire_timestamp: int,
                     group_message_timestamp: int | None = None) -> UserExpire:
        async with self.database.transaction(write=True) as conn:
            return await self._update(conn, user_id, expire_timestamp, group_message_timestamp)

    async def remove(self, user_id: int) -> None:
        async with self.database.transaction(write=True) as conn:
            await conn.execute("DELETE FROM user_expire WHERE user_id = $1", int(user_id))

    async def is_valid(self, user_id: int) -> bool:
        user = await self.get(user_id)
        return user is not None and user.expire_timestamp > int(time.time())

    async def count(self) -> int:
        async with self.database.transaction() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) AS total FROM user_expire")
            return int(row["total"])

    async def list_users(self) -> dict[int, UserExpire]:
        """Return a detached snapshot, replacing direct access to .users."""
        async with self.database.transaction() as conn:
            rows = await conn.fetch("SELECT * FROM user_expire ORDER BY user_id")
            return {int(row["user_id"]): _user(row) for row in rows}

    async def extend_minutes(self, user_id: int, minutes: int,
                             group_message_timestamp: int | None = None) -> UserExpire:
        now = int(time.time())
        async with self.database.transaction(write=True) as conn:
            # The no-op upsert locks existing/new rows and returns the latest
            # state, including when another transaction concurrently creates it.
            row = await conn.fetchrow("""
                INSERT INTO user_expire (user_id, expire_timestamp, update_timestamp, group_message_timestamp)
                VALUES ($1, $2, $2, 0)
                ON CONFLICT(user_id) DO UPDATE SET user_id = excluded.user_id
                RETURNING *
            """, int(user_id), now)
            base = max(now, int(row["expire_timestamp"]))
            expiration = max(base, min(base + max(0, int(minutes)) * 60,
                                       now + MAX_HP_CAPACITY_MINUTES * 60))
            return await self._update(conn, user_id, expiration, group_message_timestamp)

    async def consume_minutes(self, user_id: int, minutes: int) -> UserExpire | None:
        async with self.database.transaction(write=True) as conn:
            return _user(await conn.fetchrow("""
                UPDATE user_expire SET expire_timestamp = expire_timestamp - $2,
                                       update_timestamp = $3
                WHERE user_id = $1 AND expire_timestamp - $3 >= $2
                RETURNING *
            """, int(user_id), max(0, int(minutes)) * 60, int(time.time())))

    async def refund_minutes(self, user_id: int, minutes: int) -> UserExpire | None:
        """Restore a failed delivery's debit without overwriting newer rewards.

        Does not recreate removed users or cap balances. Caller owns deduplication.
        """
        async with self.database.transaction(write=True) as conn:
            return _user(await conn.fetchrow("""
                UPDATE user_expire SET expire_timestamp = expire_timestamp + $2,
                                       update_timestamp = $3
                WHERE user_id = $1 RETURNING *
            """, int(user_id), max(0, int(minutes)) * 60, int(time.time())))


class _BlacklistStore:
    def __init__(self, database):
        self.database = database

    async def is_blocked(self, user_id: int) -> bool:
        return await self.get(user_id) is not None

    async def ban(self, user_id: int, reason: str, created_by: int,
                  expires_at: int = 0) -> BlacklistEntry:
        async with self.database.transaction(write=True) as conn:
            return await self._ban(conn, user_id, reason, created_by, expires_at)

    async def _ban(self, conn, user_id, reason, created_by, expires_at):
        row = await conn.fetchrow("""
                INSERT INTO user_blacklist (user_id, reason, created_by, created_at, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT(user_id) DO UPDATE SET
                    reason = excluded.reason, created_by = excluded.created_by,
                    created_at = excluded.created_at, expires_at = excluded.expires_at
                RETURNING *
            """, int(user_id), str(reason).strip(), int(created_by), int(time.time()), max(0, int(expires_at)))
        return _blacklist_entry(row)

    async def unban(self, user_id: int) -> bool:
        async with self.database.transaction(write=True) as conn:
            row = await conn.fetchrow("DELETE FROM user_blacklist WHERE user_id = $1 RETURNING user_id", int(user_id))
            return row is not None

    async def get(self, user_id: int) -> BlacklistEntry | None:
        async with self.database.transaction() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM user_blacklist WHERE user_id = $1
                    AND (expires_at = 0 OR expires_at > $2)
            """, int(user_id), int(time.time()))
            return _blacklist_entry(row) if row else None

    async def list_page(self, page: int = 1,
                        page_size: int = 10) -> tuple[list[BlacklistEntry], int]:
        page, page_size = max(1, int(page)), max(1, int(page_size))
        async with self.database.transaction() as conn:
            # One statement keeps the count and page in the same PG snapshot,
            # including an empty page beyond the end of the result set.
            rows = await conn.fetch("""
                WITH active AS (
                    SELECT * FROM user_blacklist WHERE expires_at = 0 OR expires_at > $1
                ), page_rows AS (
                    SELECT * FROM active ORDER BY created_at DESC, user_id ASC LIMIT $2 OFFSET $3
                )
                SELECT page_rows.*, totals.total FROM (SELECT COUNT(*) AS total FROM active) AS totals
                LEFT JOIN page_rows ON 1 = 1 ORDER BY page_rows.created_at DESC, page_rows.user_id ASC
            """, int(time.time()), page_size, (page - 1) * page_size)
            return ([_blacklist_entry(row) for row in rows if row["user_id"] is not None], int(rows[0]["total"]))

    async def list_by_reason_prefix(self, reason_prefix: str) -> list[BlacklistEntry]:
        prefix = str(reason_prefix).strip()
        if not prefix:
            return []
        # Literal, case-sensitive prefix on both backends (%, _ are ordinary text).
        async with self.database.transaction() as conn:
            rows = await conn.fetch("""
                SELECT * FROM user_blacklist
                WHERE substr(reason, 1, $1) = $2 AND (expires_at = 0 OR expires_at > $3)
                ORDER BY created_at ASC, user_id ASC
            """, len(prefix), prefix, int(time.time()))
            return [_blacklist_entry(row) for row in rows]


@dataclass
class DatabaseStores:
    users: UserExpireStore
    blacklist: BlacklistStore
    backend: str
    _database: Any

    async def ban_user(self, user_id: int, reason: str, created_by: int,
                       expires_at: int = 0) -> BlacklistEntry:
        """Persist the ban and revoke the pass in one database transaction."""
        async with self._database.transaction(write=True) as conn:
            entry = await self.blacklist._ban(conn, user_id, reason, created_by, expires_at)
            await conn.execute("DELETE FROM user_expire WHERE user_id = $1", int(user_id))
            return entry

    async def _initialize_restored_schema(self, conn) -> None:
        """Repair backend-specific optional columns after a SQLite restore."""
        await _initialize_schema(conn, self.backend)

    @asynccontextmanager
    async def sqlite_restore_session(self):
        """Serialize restore and any rollback against this process's store calls.

        The yielded async function applies one snapshot on the owning SQLite
        worker and restores optional schema fields before allowing new reads.
        """
        if self.backend != "sqlite":
            raise RuntimeError("SQLite restore is unavailable for PostgreSQL")
        database = self._database
        async with database.lock:
            if database.closed:
                raise RuntimeError("Database stores are closed")

            async def restore(source_path):
                def work():
                    source = sqlite3.connect(Path(source_path).resolve().as_uri() + "?mode=ro", uri=True)
                    try:
                        source.backup(database.conn.connection)
                        result = database.conn.connection.execute("PRAGMA quick_check").fetchone()
                        if not result or str(result[0]).lower() != "ok":
                            raise RuntimeError("恢复后的 SQLite 完整性检查未通过")
                    finally:
                        source.close()
                await database.conn._run(work)
                try:
                    await database.conn.execute("BEGIN IMMEDIATE")
                    await self._initialize_restored_schema(database.conn)
                    await database.conn.execute("COMMIT")
                except BaseException:
                    await database.conn.rollback()
                    raise

            yield restore

    async def close(self) -> None:
        """Close both stores' shared database. Repeated calls are safe."""
        await self._database.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


async def _initialize(database):
    async with database.transaction(write=True) as conn:
        await _initialize_schema(conn, database.backend)


async def _initialize_schema(conn, backend):
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_expire (
            user_id BIGINT PRIMARY KEY, expire_timestamp BIGINT NOT NULL,
            update_timestamp BIGINT NOT NULL, group_message_timestamp BIGINT NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_blacklist (
            user_id BIGINT PRIMARY KEY, reason TEXT NOT NULL DEFAULT '',
            created_by BIGINT NOT NULL, created_at BIGINT NOT NULL,
            expires_at BIGINT NOT NULL DEFAULT 0
        )
    """)
    # Compatibility with the legacy SQLite schema, without changing old callers.
    for table, column in (("user_expire", "group_message_timestamp"), ("user_blacklist", "expires_at")):
        if backend == "sqlite":
            columns = await conn.fetch(f"PRAGMA table_info({table})")
            if column in {row["name"] for row in columns}:
                continue
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} BIGINT NOT NULL DEFAULT 0")
        else:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} BIGINT NOT NULL DEFAULT 0")


async def create_stores(*, backend: str | None = None,
                        sqlite_path: str | Path | None = None,
                        database_url: str | None = None) -> DatabaseStores:
    """Select DATABASE_BACKEND=sqlite|postgresql (default sqlite).

    SQLite honors USER_EXPIRE_DB_PATH and RAILWAY_VOLUME_MOUNT_PATH.
    PostgreSQL requires DATABASE_URL. PGPool is process-wide: create one bundle
    per app and share it. This factory creates schema, not a data import.
    """
    backend = (backend if backend is not None else os.getenv("DATABASE_BACKEND", "sqlite")).strip().lower()
    if backend == "sqlite":
        if sqlite3.sqlite_version_info < (3, 35, 0):
            raise RuntimeError("Async stores require SQLite >= 3.35 for RETURNING")
        volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        default_dir = Path(volume) if volume else Path(__file__).resolve().parent.parent / "data"
        path = Path(sqlite_path if sqlite_path is not None else os.getenv("USER_EXPIRE_DB_PATH", str(default_dir / "user_expire.sqlite3")))
        database = _SQLiteDatabase(path)
    elif backend == "postgresql":
        dsn = database_url if database_url is not None else os.getenv("DATABASE_URL", "")
        if not dsn.strip():
            raise ValueError("DATABASE_URL is required for PostgreSQL")
        from utils.tgone_pgsql import PGPool
        if PGPool._pool is not None:
            raise RuntimeError("PGPool is already initialized; share the existing DatabaseStores bundle")
        database = _PostgresDatabase(PGPool)
    else:
        raise ValueError(f"Unsupported DATABASE_BACKEND: {backend!r}; use sqlite or postgresql")
    try:
        if backend == "sqlite":
            await database.conn.open()
        else:
            await database.pool.init_pool(dsn)
        await _initialize(database)
    except BaseException:
        await database.close()
        raise
    return DatabaseStores(_UserExpireStore(database), _BlacklistStore(database), backend, database)
