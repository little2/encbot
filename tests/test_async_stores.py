import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from config import MAX_HP_CAPACITY_MINUTES
from utils.async_stores import BlacklistStore, UserExpireStore, create_stores
from utils.blacklist_utils import BlacklistStore as LegacyBlacklistStore
from utils.user_utils import UserExpireCache

try:
    import asyncpg
except ImportError:
    asyncpg = None


NOW = 1_700_000_000
TEST_POSTGRES_DSN = os.getenv("ASYNC_STORES_TEST_POSTGRES_DSN", "")


class StoreContract:
    async def test_interfaces_and_missing_records(self):
        self.assertIsInstance(self.stores.users, UserExpireStore)
        self.assertIsInstance(self.stores.blacklist, BlacklistStore)
        self.assertIsNone(await self.stores.users.get(1))
        self.assertFalse(await self.stores.users.is_valid(1))
        self.assertEqual(await self.stores.users.count(), 0)
        self.assertEqual(await self.stores.users.list_users(), {})
        self.assertIsNone(await self.stores.users.consume_minutes(1, 1))
        self.assertFalse(await self.stores.blacklist.is_blocked(1))
        self.assertFalse(await self.stores.blacklist.unban(1))
        self.assertEqual(await self.stores.blacklist.list_page(), ([], 0))

    async def test_updates_keep_group_timestamp_and_return_snapshots(self):
        users = self.stores.users
        user_id = 9_000_000_001
        first = await users.update(user_id, NOW + 600, group_message_timestamp=123)
        second = await users.update(user_id, NOW + 1200)
        self.assertEqual(first.expire_timestamp, NOW + 600)
        self.assertEqual(second.group_message_timestamp, 123)
        self.assertEqual(second.update_timestamp, NOW)
        snapshot = await users.list_users()
        snapshot[user_id].expire_timestamp = 0
        self.assertEqual((await users.get(user_id)).expire_timestamp, NOW + 1200)
        self.assertTrue(await users.is_valid(user_id))
        self.assertEqual(await users.count(), 1)
        await users.remove(user_id)
        await users.remove(user_id)
        self.assertIsNone(await users.get(user_id))

    async def test_remove_if_expired_preserves_renewed_users(self):
        users = self.stores.users
        cutoff = NOW - 86400
        await users.update(1, cutoff)
        self.assertTrue(await users.remove_if_expired(1, cutoff))
        self.assertFalse(await users.remove_if_expired(1, cutoff))
        await users.update(2, cutoff + 1)
        self.assertFalse(await users.remove_if_expired(2, cutoff))
        await users.update(3, cutoff - 1)
        await users.update(3, NOW + 600)
        self.assertFalse(await users.remove_if_expired(3, cutoff))
        self.assertEqual((await users.get(3)).expire_timestamp, NOW + 600)

    async def test_reward_cap_and_existing_over_cap_balance(self):
        users = self.stores.users
        limit = NOW + MAX_HP_CAPACITY_MINUTES * 60
        self.assertEqual((await users.extend_minutes(1, 1)).expire_timestamp, NOW + 60)
        self.assertEqual((await users.extend_minutes(1, -5)).expire_timestamp, NOW + 60)
        self.assertEqual((await users.extend_minutes(1, MAX_HP_CAPACITY_MINUTES * 2)).expire_timestamp, limit)
        await users.update(1, limit + 60)
        self.assertEqual((await users.extend_minutes(1, 1)).expire_timestamp, limit + 60)
        await users.update(1, NOW - 600)
        self.assertEqual((await users.extend_minutes(1, 2, 456)).expire_timestamp, NOW + 120)
        self.assertEqual((await users.get(1)).group_message_timestamp, 456)

    async def test_consume_boundaries(self):
        users = self.stores.users
        await users.update(1, NOW + 60)
        self.assertIsNone(await users.consume_minutes(1, 2))
        self.assertEqual((await users.consume_minutes(1, -2)).expire_timestamp, NOW + 60)
        self.assertEqual((await users.consume_minutes(1, 1)).expire_timestamp, NOW)
        self.assertFalse(await users.is_valid(1))
        self.assertIsNone(await users.consume_minutes(1, 1))
        await users.update(1, NOW - 1)
        self.assertIsNone(await users.consume_minutes(1, 0))

    async def test_refund_keeps_rewards_and_does_not_recreate_deleted_users(self):
        users = self.stores.users
        await users.update(1, NOW + 120)
        await users.consume_minutes(1, 1)
        await users.extend_minutes(1, 10, group_message_timestamp=123)
        refunded = await users.refund_minutes(1, 1)
        self.assertEqual(refunded.expire_timestamp, NOW + 720)
        self.assertEqual(refunded.group_message_timestamp, 123)
        await users.remove(1)
        self.assertIsNone(await users.refund_minutes(1, 1))
        self.assertIsNone(await users.get(1))

    async def test_atomic_ban_revokes_pass(self):
        await self.stores.users.update(1, NOW + 120)
        entry = await self.stores.ban_user(1, "ban", 123)
        self.assertEqual(entry.created_by, 123)
        self.assertIsNone(await self.stores.users.get(1))
        self.assertTrue(await self.stores.blacklist.is_blocked(1))

    async def test_atomic_ban_rolls_back_when_revocation_fails(self):
        await self.stores.users.update(1, NOW + 120)
        blacklist = self.stores.blacklist
        original = blacklist._ban

        async def fail_after_ban(*args):
            await original(*args)
            raise ValueError("revocation failed")

        with patch.object(blacklist, "_ban", side_effect=fail_after_ban):
            with self.assertRaisesRegex(ValueError, "revocation failed"):
                await self.stores.ban_user(1, "ban", 123)
        self.assertIsNotNone(await self.stores.users.get(1))
        self.assertFalse(await self.stores.blacklist.is_blocked(1))

    async def test_concurrent_rewards_and_debits(self):
        users = self.stores.users
        await asyncio.gather(*(users.extend_minutes(1, 1) for _ in range(20)))
        self.assertEqual((await users.get(1)).expire_timestamp, NOW + 1200)
        results = await asyncio.gather(*(users.consume_minutes(1, 1) for _ in range(40)))
        self.assertEqual(sum(result is not None for result in results), 20)
        self.assertEqual((await users.get(1)).expire_timestamp, NOW)

    async def test_blacklist_overwrite_and_expiration(self):
        blacklist = self.stores.blacklist
        entry = await blacklist.ban(9_000_000_001, " reason ", 8_000_000_001)
        self.assertEqual(entry.reason, "reason")
        self.assertEqual(entry.created_by, 8_000_000_001)
        self.assertEqual(await blacklist.get(entry.user_id), entry)
        self.assertTrue(await blacklist.is_blocked(entry.user_id))
        await blacklist.ban(entry.user_id, "replacement", 2, expires_at=NOW + 1)
        self.assertEqual((await blacklist.get(entry.user_id)).reason, "replacement")
        with patch("utils.async_stores.time.time", return_value=NOW + 1):
            self.assertFalse(await blacklist.is_blocked(entry.user_id))
            self.assertEqual(await blacklist.list_page(), ([], 0))
        await blacklist.ban(entry.user_id, "permanent", 2, expires_at=-1)
        self.assertEqual((await blacklist.get(entry.user_id)).expires_at, 0)
        self.assertTrue(await blacklist.unban(entry.user_id))
        self.assertFalse(await blacklist.unban(entry.user_id))

    async def test_blacklist_paging_and_literal_prefix(self):
        blacklist = self.stores.blacklist
        await blacklist.ban(3, "a_% one", 1)
        await blacklist.ban(2, "a_% two", 1)
        await blacklist.ban(1, "A_% three", 1)
        await blacklist.ban(4, "a_% expired", 1, expires_at=NOW)
        entries, total = await blacklist.list_page(page=1, page_size=2)
        self.assertEqual([entry.user_id for entry in entries], [1, 2])
        self.assertEqual(total, 3)
        self.assertEqual(await blacklist.list_page(page=10), ([], 3))
        self.assertEqual([entry.user_id for entry in await blacklist.list_by_reason_prefix("a_%")], [2, 3])
        self.assertEqual(await blacklist.list_by_reason_prefix("  "), [])

    async def test_transaction_failure_rolls_back(self):
        with self.assertRaisesRegex(ValueError, "abort"):
            async with self.stores._database.transaction() as conn:
                await conn.execute("INSERT INTO user_expire VALUES ($1, $2, $3, $4)", 1, NOW, NOW, 0)
                raise ValueError("abort")
        self.assertIsNone(await self.stores.users.get(1))

    async def test_cancelled_transaction_rolls_back(self):
        entered = asyncio.Event()

        async def work():
            async with self.stores._database.transaction() as conn:
                await conn.execute("INSERT INTO user_expire VALUES ($1, $2, $3, $4)", 1, NOW, NOW, 0)
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(await self.stores.users.get(1))

    async def test_closed_stores_reject_operations(self):
        await self.stores.close()
        await self.stores.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await self.stores.users.get(1)


class SQLiteStoreTests(StoreContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = patch("utils.async_stores.time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.path = Path(self.directory.name) / "users.sqlite3"
        self.stores = await create_stores(backend="sqlite", sqlite_path=self.path)
        self.addAsyncCleanup(self.stores.close)

    async def test_reopen_preserves_data(self):
        await self.stores.users.update(1, NOW + 60, 123)
        await self.stores.blacklist.ban(2, "persist", 1)
        await self.stores.close()
        async with await create_stores(backend="sqlite", sqlite_path=self.path) as reopened:
            self.assertEqual((await reopened.users.get(1)).group_message_timestamp, 123)
            self.assertTrue(await reopened.blacklist.is_blocked(2))

    async def test_restore_updates_live_connection_and_repairs_optional_schema(self):
        await self.stores.users.update(1, NOW + 60)
        snapshot = Path(self.directory.name) / "snapshot.sqlite3"
        with closing(sqlite3.connect(snapshot)) as conn, conn:
            conn.execute("CREATE TABLE user_expire (user_id INTEGER PRIMARY KEY, expire_timestamp INTEGER NOT NULL, update_timestamp INTEGER NOT NULL)")
            conn.execute("INSERT INTO user_expire VALUES (2, ?, ?)", (NOW + 120, NOW))
        async with self.stores.sqlite_restore_session() as restore:
            await restore(snapshot)
            waiting = asyncio.create_task(self.stores.users.get(2))
            await asyncio.sleep(0)
            self.assertFalse(waiting.done())
        self.assertEqual((await waiting).group_message_timestamp, 0)
        self.assertIsNone(await self.stores.users.get(1))
        self.assertFalse(await self.stores.blacklist.is_blocked(2))
        await self.stores.blacklist.ban(2, "works after restore", 123)
        self.assertTrue(await self.stores.blacklist.is_blocked(2))

    async def test_two_handles_see_updates_and_serialize_debits(self):
        async with await create_stores(backend="sqlite", sqlite_path=self.path) as other:
            await self.stores.users.update(1, NOW + 60)
            self.assertTrue(await other.users.is_valid(1))
            results = await asyncio.gather(self.stores.users.consume_minutes(1, 1),
                                           other.users.consume_minutes(1, 1))
            self.assertEqual(sum(result is not None for result in results), 1)
            await other.blacklist.ban(1, "shared", 1)
            self.assertTrue(await self.stores.blacklist.is_blocked(1))

    async def test_legacy_schema_upgrade_and_sync_compatibility(self):
        await self.stores.close()
        legacy_path = Path(self.directory.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as conn, conn:
            conn.execute("CREATE TABLE user_expire (user_id INTEGER PRIMARY KEY, expire_timestamp INTEGER NOT NULL, update_timestamp INTEGER NOT NULL)")
            conn.execute("INSERT INTO user_expire VALUES (1, ?, ?)", (NOW + 60, NOW))
            conn.execute("CREATE TABLE user_blacklist (user_id INTEGER PRIMARY KEY, reason TEXT NOT NULL, created_by INTEGER NOT NULL, created_at INTEGER NOT NULL)")
            conn.execute("INSERT INTO user_blacklist VALUES (2, 'legacy', 1, ?)", (NOW,))
        async with await create_stores(backend="sqlite", sqlite_path=legacy_path) as stores:
            self.assertEqual((await stores.users.get(1)).group_message_timestamp, 0)
            self.assertTrue(await stores.blacklist.is_blocked(2))
            await stores.users.update(1, NOW + 120, 123)
        legacy_users = UserExpireCache(legacy_path)
        legacy_blacklist = LegacyBlacklistStore(legacy_path)
        try:
            self.assertEqual(legacy_users.get(1).group_message_timestamp, 123)
            self.assertTrue(legacy_blacklist.is_blocked(2))
        finally:
            legacy_users.close()
            legacy_blacklist.close()

    async def test_sqlite_work_does_not_block_event_loop(self):
        entered, release = threading.Event(), threading.Event()

        def blocking_work():
            entered.set()
            release.wait(timeout=2)

        task = asyncio.create_task(self.stores._database.conn._run(blocking_work))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(entered.is_set())
            self.assertFalse(task.done())
        finally:
            release.set()
            await task

    async def test_cancellation_waits_for_worker_before_rollback(self):
        entered, release = threading.Event(), threading.Event()
        database = self.stores._database

        def slow_insert():
            database.conn.connection.execute("INSERT INTO user_expire VALUES (1, 1, 1, 0)")
            entered.set()
            release.wait(timeout=2)

        async def work():
            async with database.transaction(write=True):
                await database.conn._run(slow_insert)

        task = asyncio.create_task(work())
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(await self.stores.users.get(1))

    async def test_backend_config_and_validation(self):
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "USER_EXPIRE_DB_PATH": str(self.path)}):
            async with await create_stores() as stores:
                self.assertEqual(stores.backend, "sqlite")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            await create_stores(backend="typo")
        with self.assertRaisesRegex(ValueError, "DATABASE_URL"):
            await create_stores(backend="postgresql", database_url="")

    async def test_worker_error_rolls_back_without_losing_connection(self):
        with self.assertRaises(sqlite3.OperationalError):
            async with self.stores._database.transaction(write=True) as conn:
                await conn.execute("INSERT INTO user_expire VALUES ($1, $2, $3, $4)", 1, NOW, NOW, 0)
                await conn.execute("invalid SQL")
        self.assertIsNone(await self.stores.users.get(1))
        await self.stores.users.update(2, NOW + 60)
        self.assertTrue(await self.stores.users.is_valid(2))


@unittest.skipIf(asyncpg is None, "Install asyncpg for PostgreSQL adapter unit tests")
class PostgresAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = Mock()
        self.conn.execute = AsyncMock()
        self.conn.fetchrow = AsyncMock(return_value=None)
        self.tx = Mock()
        self.tx.__aenter__ = AsyncMock()
        self.tx.__aexit__ = AsyncMock(return_value=False)
        self.conn.transaction = Mock(return_value=self.tx)
        self.acquire = Mock()
        self.acquire.__aenter__ = AsyncMock(return_value=self.conn)
        self.acquire.__aexit__ = AsyncMock(return_value=False)
        self.pool = Mock()
        self.pool.acquire = Mock(return_value=self.acquire)
        self.pg = Mock(_pool=None)
        self.pg.init_pool = AsyncMock(return_value=self.pool)
        self.pg.ensure_pool = AsyncMock(return_value=self.pool)
        self.pg.close = AsyncMock()
        self.patch = patch("utils.tgone_pgsql.PGPool", self.pg)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_factory_initializes_and_closes_shared_pool_once(self):
        stores = await create_stores(backend="postgresql", database_url="postgresql://test")
        self.pg.init_pool.assert_awaited_once_with("postgresql://test")
        self.assertEqual(stores.backend, "postgresql")
        self.assertIs(stores.users.database, stores.blacklist.database)
        self.assertIsNone(await stores.users.get(1))
        self.conn.fetchrow.assert_awaited_once_with("SELECT * FROM user_expire WHERE user_id = $1", 1)
        await stores.close()
        await stores.close()
        self.pg.close.assert_awaited_once()

    async def test_schema_failure_closes_pool_and_preserves_error(self):
        self.conn.execute.side_effect = RuntimeError("schema failure")
        with self.assertRaisesRegex(RuntimeError, "schema failure"):
            await create_stores(backend="postgresql", database_url="postgresql://test")
        self.pg.close.assert_awaited_once()
        self.assertIs(self.tx.__aexit__.await_args.args[0], RuntimeError)

    async def test_operation_errors_propagate_without_retry(self):
        async with await create_stores(backend="postgresql", database_url="postgresql://test") as stores:
            error = OSError("unknown commit outcome")
            self.conn.fetchrow.side_effect = error
            with self.assertRaises(OSError):
                await stores.users.consume_minutes(1, 1)
            self.conn.fetchrow.assert_awaited_once()
            self.assertIs(self.tx.__aexit__.await_args.args[1], error)

    async def test_existing_pool_is_not_replaced_or_closed(self):
        self.pg._pool = self.pool
        with self.assertRaisesRegex(RuntimeError, "already initialized"):
            await create_stores(backend="postgresql", database_url="postgresql://test")
        self.pg.init_pool.assert_not_awaited()
        self.pg.close.assert_not_awaited()


@unittest.skipUnless(TEST_POSTGRES_DSN, "Set ASYNC_STORES_TEST_POSTGRES_DSN to run PostgreSQL contract tests")
class PostgresStoreTests(StoreContract, unittest.IsolatedAsyncioTestCase):
    """Opt-in tests use only a unique temporary schema, never existing app tables."""

    async def asyncSetUp(self):
        import asyncpg

        self.clock = patch("utils.async_stores.time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        schema = "async_stores_test_" + uuid4().hex
        admin = await asyncpg.connect(TEST_POSTGRES_DSN)
        self.addAsyncCleanup(admin.close)

        async def drop_schema():
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')

        await admin.execute(f'CREATE SCHEMA "{schema}"')
        self.addAsyncCleanup(drop_schema)
        url = urlsplit(TEST_POSTGRES_DSN)
        query = [(key, value) for key, value in parse_qsl(url.query) if key != "search_path"]
        query.append(("search_path", schema))
        dsn = urlunsplit(url._replace(query=urlencode(query)))
        self.stores = await create_stores(backend="postgresql", database_url=dsn)
        self.addAsyncCleanup(self.stores.close)


if __name__ == "__main__":
    unittest.main()
