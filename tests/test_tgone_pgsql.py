import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, Mock, patch

import asyncpg

from utils.tgone_pgsql import PGPool


class PGPoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 独立的子类防止连接池与 asyncio.Lock 泄漏到其他测试/事件循环。
        class TestPool(PGPool):
            _pool = None
            _lock = asyncio.Lock()
            DSN = "postgresql://test"
            CLOSE_TIMEOUT = 0.01

        self.pg = TestPool
        self.conn = Mock()
        self.pool = Mock()
        self.pool.close = AsyncMock()

        @asynccontextmanager
        async def acquire():
            yield self.conn

        self.pool.acquire = Mock(side_effect=acquire)

    async def test_rebuild_does_not_deadlock(self):
        self.pg._pool = self.pool
        replacement = Mock()
        with patch("utils.tgone_pgsql.asyncpg.create_pool", new_callable=AsyncMock) as create:
            create.return_value = replacement
            result = await asyncio.wait_for(self.pg._rebuild_pool(), timeout=0.5)
        self.assertIs(result, replacement)
        self.assertIs(self.pg._pool, replacement)
        self.pool.close.assert_awaited_once()
        self.pool.terminate.assert_not_called()

    async def test_concurrent_initialization_creates_one_pool(self):
        with patch("utils.tgone_pgsql.asyncpg.create_pool", new_callable=AsyncMock) as create:
            create.return_value = self.pool
            results = await asyncio.gather(*(self.pg.ensure_pool() for _ in range(5)))
        create.assert_awaited_once()
        self.assertTrue(all(pool is self.pool for pool in results))

    async def test_failed_rebuild_can_be_initialized_again(self):
        self.pg._pool = self.pool
        with patch("utils.tgone_pgsql.asyncpg.create_pool", new_callable=AsyncMock) as create:
            create.side_effect = [OSError("offline"), self.pool]
            with self.assertRaises(OSError):
                await self.pg._rebuild_pool()
            self.assertIsNone(self.pg._pool)
            result = await asyncio.wait_for(self.pg.init_pool(), timeout=0.5)
        self.assertIs(result, self.pool)

    async def test_sql_helpers_do_not_retry_by_default(self):
        self.pg._pool = self.pool
        for method in ("execute", "fetchrow", "fetch", "fetchval"):
            with self.subTest(method=method):
                query = AsyncMock(side_effect=asyncpg.exceptions.ConnectionDoesNotExistError("lost response"))
                setattr(self.conn, method, query)
                with self.assertRaises(asyncpg.exceptions.ConnectionDoesNotExistError):
                    await getattr(self.pg, method)("UPDATE balance SET amount = amount - $1 RETURNING amount", 1)
                query.assert_awaited_once()

    async def test_explicit_retry_reacquires_without_rebuilding_pool(self):
        self.pg._pool = self.pool
        self.conn.fetchval = AsyncMock(side_effect=[OSError("connection lost"), 42])
        result = await self.pg.fetchval("SELECT $1::bigint", 42, retry_safe=True)
        self.assertEqual(result, 42)
        self.assertEqual(self.pool.acquire.call_count, 2)
        self.pool.close.assert_not_awaited()

    async def test_explicit_retry_stops_after_second_failure(self):
        self.pg._pool = self.pool
        self.conn.execute = AsyncMock(side_effect=OSError("offline"))
        with self.assertRaises(OSError):
            await self.pg.execute("SELECT 1", retry_safe=True)
        self.assertEqual(self.conn.execute.await_count, 2)

    async def test_interface_and_sql_errors_are_not_retried(self):
        self.pg._pool = self.pool
        for error in (asyncpg.exceptions.InterfaceError("invalid use"),
                      asyncpg.exceptions.PostgresSyntaxError("bad SQL")):
            with self.subTest(error=type(error).__name__):
                self.conn.fetch = AsyncMock(side_effect=error)
                with self.assertRaises(type(error)):
                    await self.pg.fetch("invalid SQL", retry_safe=True)
                self.conn.fetch.assert_awaited_once()

    async def test_graceful_close_is_idempotent(self):
        self.pg._pool = self.pool
        await self.pg.close()
        await self.pg.close()
        self.pool.close.assert_awaited_once()
        self.pool.terminate.assert_not_called()
        self.assertIsNone(self.pg._pool)

    async def test_close_timeout_terminates_pool(self):
        async def hanging_close():
            await asyncio.Event().wait()

        self.pg._pool = self.pool
        self.pool.close.side_effect = hanging_close
        await asyncio.wait_for(self.pg.close(), timeout=0.5)
        self.pool.terminate.assert_called_once()
        self.assertIsNone(self.pg._pool)

    async def test_close_error_terminates_pool(self):
        self.pg._pool = self.pool
        self.pool.close.side_effect = OSError("close failed")
        await self.pg.close()
        self.pool.terminate.assert_called_once()
        self.assertIsNone(self.pg._pool)

    async def test_close_cancellation_terminates_and_propagates(self):
        self.pg._pool = self.pool
        self.pool.close.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.pg.close()
        self.pool.terminate.assert_called_once()
        self.assertIsNone(self.pg._pool)
        self.assertFalse(self.pg._lock.locked())

    async def test_rebuild_replaces_pool_even_if_old_close_times_out(self):
        async def hanging_close():
            await asyncio.Event().wait()

        self.pg._pool = self.pool
        self.pool.close.side_effect = hanging_close
        replacement = Mock()
        with patch("utils.tgone_pgsql.asyncpg.create_pool", new_callable=AsyncMock) as create:
            create.return_value = replacement
            await asyncio.wait_for(self.pg._rebuild_pool(), timeout=0.5)
        self.pool.terminate.assert_called_once()
        self.assertIs(self.pg._pool, replacement)

    async def test_transaction_uses_same_connection_and_does_not_retry(self):
        self.pg._pool = self.pool
        transaction = Mock()
        transaction.__aenter__ = AsyncMock()
        transaction.__aexit__ = AsyncMock(return_value=False)
        self.conn.transaction = Mock(return_value=transaction)
        error = OSError("commit outcome unknown")
        work = AsyncMock(side_effect=error)
        with self.assertRaises(OSError):
            await self.pg.in_tx(work, 123)
        work.assert_awaited_once_with(self.conn, 123)
        self.pool.acquire.assert_called_once()
        self.assertIs(transaction.__aexit__.await_args.args[1], error)


if __name__ == "__main__":
    unittest.main()
