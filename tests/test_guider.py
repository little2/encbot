import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot
from aiogram.types import Update

from shared_config import SharedConfig
from utils.airport_async_stores import batch_store_from_pool
from utils.async_stores import blacklist_store_from_pool, create_stores

with patch.object(SharedConfig, "load"):
    import guider


class GuiderMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot(token="123456:TEST_TOKEN")
        self.blacklist = SimpleNamespace(is_blocked=AsyncMock(return_value=False))
        self.batches = SimpleNamespace(count_by_uploader=AsyncMock(return_value=7))
        self.request = AsyncMock(return_value=True)
        self.request_patch = patch.object(self.bot.session, "make_request", self.request)
        self.request_patch.start()

    async def asyncTearDown(self):
        self.request_patch.stop()
        await self.bot.session.close()

    async def feed_message(self, content, *, sender=True):
        update = Update.model_validate({
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1700000000,
                "chat": {"id": 123, "type": "private"},
                **({"from": {"id": 456, "is_bot": False, "first_name": "Test"}} if sender else {}),
                **content,
            },
        }, context={"bot": self.bot})
        await guider.dp.feed_update(
            self.bot, update, blacklist_store=self.blacklist, batch_store=self.batches,
        )

    async def test_service_command_replies_with_service_station(self):
        for text in ("/service", "/service 连络地勤大叔"):
            with self.subTest(text=text):
                self.request.reset_mock()
                await self.feed_message({"text": text})
                self.assertEqual(self.request.await_count, 1)
                self.assertEqual(
                    self.request.await_args.args[1].text, "服务站: @ztTowerServiceBOT",
                )
        self.batches.count_by_uploader.assert_not_awaited()

    async def test_blocked_service_command_gets_no_reply(self):
        self.blacklist.is_blocked.return_value = True
        await self.feed_message({"text": "/service"})
        self.blacklist.is_blocked.assert_awaited_once_with(456)
        self.request.assert_not_awaited()

    async def test_text_gets_text_reply(self):
        await self.feed_message({"text": "你好"})
        self.blacklist.is_blocked.assert_awaited_once_with(456)
        self.batches.count_by_uploader.assert_not_awaited()
        self.assertEqual(self.request.await_count, 1)
        text = self.request.await_args.args[1].text
        self.assertIn("请先提供一份资源", text)

    async def test_text_does_not_depend_on_upload_count(self):
        self.batches.count_by_uploader.return_value = 0
        await self.feed_message({"text": "你好"})
        self.batches.count_by_uploader.assert_not_awaited()
        self.assertIn("请先提供一份资源", self.request.await_args.args[1].text)

    async def test_blocked_text_gets_no_reply(self):
        self.blacklist.is_blocked.return_value = True
        await self.feed_message({"text": "你好"})
        self.blacklist.is_blocked.assert_awaited_once_with(456)
        self.request.assert_not_awaited()
        self.batches.count_by_uploader.assert_not_awaited()

    async def test_text_without_sender_is_not_processed(self):
        with self.assertLogs(level="WARNING"):
            await self.feed_message({"text": "你好"}, sender=False)
        self.blacklist.is_blocked.assert_not_awaited()
        self.request.assert_not_awaited()

    async def test_database_failure_does_not_reply(self):
        self.blacklist.is_blocked.side_effect = RuntimeError("database offline")
        with self.assertRaisesRegex(RuntimeError, "database offline"):
            await self.feed_message({"text": "你好"})
        self.request.assert_not_awaited()

    async def test_blacklist_expiration_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            stores = await create_stores(backend="sqlite", sqlite_path=Path(directory) / "test.sqlite3")
            try:
                self.blacklist = stores.blacklist
                with patch("utils.async_stores.time.time", return_value=100):
                    for expires_at, should_reply in ((0, False), (101, False), (100, True), (99, True)):
                        with self.subTest(expires_at=expires_at):
                            await self.blacklist.ban(456, "", 1, expires_at=expires_at)
                            self.request.reset_mock()
                            await self.feed_message({"text": "你好"})
                            self.assertEqual(self.request.await_count, int(should_reply))
            finally:
                await stores.close()

    async def test_media_with_caption_gets_media_reply(self):
        await self.feed_message({
            "photo": [{"file_id": "id", "file_unique_id": "unique", "width": 1, "height": 1}],
            "caption": "说明文字",
        })
        self.assertEqual(self.request.await_count, 1)
        self.assertIn("@zttower18bot", self.request.await_args.args[1].text)
        self.assertTrue(self.request.await_args.args[1].protect_content)
        self.blacklist.is_blocked.assert_awaited_once_with(456)
        self.batches.count_by_uploader.assert_awaited_once_with(456)

    async def test_blocked_media_does_not_query_upload_count(self):
        self.blacklist.is_blocked.return_value = True
        await self.feed_message({
            "photo": [{"file_id": "id", "file_unique_id": "unique", "width": 1, "height": 1}],
        })
        self.batches.count_by_uploader.assert_not_awaited()
        self.request.assert_not_awaited()

    async def test_video_note_gets_media_reply(self):
        await self.feed_message({
            "video_note": {
                "file_id": "id", "file_unique_id": "unique", "length": 1, "duration": 1,
            },
        })
        self.assertEqual(self.request.await_count, 1)
        self.assertIn("@zttower18bot", self.request.await_args.args[1].text)


class GuiderStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config_patch = patch.object(guider.SharedConfig, "get", return_value="")
        config_patch.start()
        self.addCleanup(config_patch.stop)

    async def test_connects_passes_and_closes_database(self):
        db = SimpleNamespace(fetchval=AsyncMock(return_value=1), close=AsyncMock())
        bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()), delete_webhook=AsyncMock())
        blacklist = SimpleNamespace(is_blocked=AsyncMock())
        batches = SimpleNamespace(count_by_uploader=AsyncMock())
        with (
            patch.dict("os.environ", {"GUIDER_BOT_TOKEN": "token", "DATABASE_URL": "postgresql://test"}, clear=True),
            patch.object(guider, "Bot", return_value=bot),
            patch.object(guider.asyncpg, "create_pool", new=AsyncMock(return_value=db)) as create_pool,
            patch.object(guider, "blacklist_store_from_pool", return_value=blacklist) as make_blacklist,
            patch.object(guider, "batch_store_from_pool", return_value=batches) as make_batches,
            patch.object(guider.dp, "start_polling", new=AsyncMock()) as poll,
        ):
            await guider.main()
        create_pool.assert_awaited_once_with(dsn="postgresql://test", min_size=1, max_size=5)
        db.fetchval.assert_awaited_once_with("SELECT 1")
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        make_blacklist.assert_called_once_with(db)
        make_batches.assert_called_once_with(db)
        poll.assert_awaited_once_with(
            bot, blacklist_store=blacklist, batch_store=batches, close_bot_session=False,
        )
        db.close.assert_awaited_once()
        bot.session.close.assert_awaited_once()

    async def test_database_failure_closes_resources_without_polling(self):
        db = SimpleNamespace(fetchval=AsyncMock(side_effect=RuntimeError("database offline")), close=AsyncMock())
        bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
        with (
            patch.dict("os.environ", {"GUIDER_BOT_TOKEN": "token", "DATABASE_URL": "postgresql://test"}, clear=True),
            patch.object(guider, "Bot", return_value=bot),
            patch.object(guider.asyncpg, "create_pool", new=AsyncMock(return_value=db)),
            patch.object(guider.dp, "start_polling", new=AsyncMock()) as poll,
            self.assertRaisesRegex(RuntimeError, "database offline"),
        ):
            await guider.main()
        poll.assert_not_awaited()
        db.close.assert_awaited_once()
        bot.session.close.assert_awaited_once()

    async def test_missing_configuration_fails_explicitly(self):
        with (
            patch.object(guider.SharedConfig, "get", return_value=""),
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "GUIDER_BOT_TOKEN"),
        ):
            await guider.main()
        with (
            patch.dict("os.environ", {"GUIDER_BOT_TOKEN": "token"}, clear=True),
            self.assertRaisesRegex(RuntimeError, "DATABASE_URL"),
        ):
            await guider.main()


class BorrowedBlacklistStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_existing_pool_and_does_not_close_it(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={
            "user_id": 456, "reason": "", "created_by": 1,
            "created_at": 1, "expires_at": 0,
        })
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        store = blacklist_store_from_pool(pool)

        with patch("utils.async_stores.time.time", return_value=100):
            self.assertTrue(await store.is_blocked(456))

        args = conn.fetchrow.await_args
        assert args is not None
        self.assertEqual(args.args[1:], (456, 100))
        conn.transaction.assert_called_once()
        pool.acquire.assert_called_once()
        pool.close.assert_not_called()


class BorrowedBatchStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_uploads_for_sender_using_existing_pool(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={"total": 7})
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        store = batch_store_from_pool(pool)

        self.assertEqual(await store.count_by_uploader(456), 7)
        conn.fetchrow.assert_awaited_once_with(
            "SELECT COUNT(*) AS total FROM batch WHERE uploader_user_id = $1", 456,
        )
        pool.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
