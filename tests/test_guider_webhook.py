import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer
from aiogram import Bot

from shared_config import SharedConfig

with patch.object(SharedConfig, "load"):
    import guider


class WebhookSettingsTests(unittest.TestCase):
    def test_root_webhook_path(self):
        with patch.dict("os.environ", {
            "BOT_MODE": "webhook",
            "WEBHOOK_HOST": "https://example.com/",
            "WEBHOOK_PATH": "/",
        }, clear=True):
            self.assertEqual(guider.webhook_settings(), (
                "https://example.com/", "/", "0.0.0.0", 8080,
            ))

    def test_default_polling_and_valid_webhook(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(guider.webhook_settings())
        with patch.dict("os.environ", {
            "WEBHOOK_HOST": "https://example.com",
            "WEBHOOK_PATH": "/telegram/guider",
            "PORT": "9000",
        }, clear=True):
            self.assertEqual(guider.webhook_settings(), (
                "https://example.com/telegram/guider",
                "/telegram/guider", "0.0.0.0", 9000,
            ))

    def test_render_style_webhook_and_explicit_polling(self):
        config = {
            "BOT_MODE": "webhook",
            "WEBHOOK_HOST": "https://example.onrender.com/",
            "WEBHOOK_PATH": "/telegram/guider",
            "PORT": "10000",
        }
        with patch.dict("os.environ", config, clear=True):
            self.assertEqual(guider.webhook_settings(), (
                "https://example.onrender.com/telegram/guider",
                "/telegram/guider", "0.0.0.0", 10000,
            ))
        with patch.dict("os.environ", {**config, "BOT_MODE": "polling"}, clear=True):
            self.assertIsNone(guider.webhook_settings())

    def test_invalid_configuration_fails(self):
        cases = [
            {"BOT_MODE": "unknown"},
            {"BOT_MODE": "webhook"},
            {"WEBHOOK_HOST": "https://example.com"},
            {"WEBHOOK_HOST": "http://example.com", "WEBHOOK_PATH": "/hook"},
            {"WEBHOOK_HOST": "https://example.com/base", "WEBHOOK_PATH": "/hook"},
            {"WEBHOOK_HOST": "https://example.com?x=1", "WEBHOOK_PATH": "/hook"},
            {"WEBHOOK_HOST": "https://example.com", "WEBHOOK_PATH": "//"},
            {"WEBHOOK_HOST": "https://example.com", "WEBHOOK_PATH": "/hook", "PORT": "0"},
        ]
        for values in cases:
            with self.subTest(values=values), patch.dict("os.environ", values, clear=True):
                with self.assertRaises(ValueError):
                    guider.webhook_settings()


class WebhookRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_accepts_health_get_and_webhook_post(self):
        bot = Bot(token="123456:TEST_TOKEN")
        app = guider.webhook_app(bot, "/", MagicMock(), MagicMock())
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        response = await client.get("/")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "OK")
        # An update with no event is acknowledged without calling Telegram.
        response = await client.post("/", json={"update_id": 2})
        self.assertEqual(response.status, 200)

    async def test_update_dispatch_without_secret(self):
        bot = Bot(token="123456:TEST_TOKEN")
        request = AsyncMock(return_value=True)
        blacklist = MagicMock(is_blocked=AsyncMock(return_value=False))
        batches = MagicMock(count_by_uploader=AsyncMock(return_value=7))
        app = guider.webhook_app(bot, "/telegram/guider", blacklist, batches)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        payload = {
            "update_id": 1,
            "message": {
                "message_id": 10, "date": 1700000000,
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 456, "is_bot": False, "first_name": "Test"},
                "text": "hello",
            },
        }
        with patch.object(bot.session, "make_request", request):
            response = await client.get("/")
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.text(), "OK")
            response = await client.post("/telegram/guider", json=payload)
            self.assertEqual(response.status, 200)
            blacklist.is_blocked.assert_awaited_once_with(456)
            batches.count_by_uploader.assert_not_awaited()
            request.assert_awaited_once()


class WebhookLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_after_listening_and_cleans_up_on_cancel(self):
        ready = asyncio.Event()
        async def register(**kwargs):
            ready.set()
            return True

        bot = MagicMock(set_webhook=AsyncMock(side_effect=register))
        runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        site = SimpleNamespace(start=AsyncMock())
        settings = ("https://example.com/hook", "/hook", "127.0.0.1", 9000)
        with (
            patch.object(guider, "webhook_app", return_value=MagicMock()),
            patch.object(guider.web, "AppRunner", return_value=runner),
            patch.object(guider.web, "TCPSite", return_value=site),
        ):
            task = asyncio.create_task(guider.run_webhook(bot, settings, MagicMock(), MagicMock()))
            await asyncio.wait_for(ready.wait(), timeout=1)
            site.start.assert_awaited_once()
            bot.set_webhook.assert_awaited_once_with(
                url=settings[0], allowed_updates=["message"],
                drop_pending_updates=False,
            )
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        runner.cleanup.assert_awaited_once()

    async def test_main_selects_webhook_and_closes_resources(self):
        db = SimpleNamespace(fetchval=AsyncMock(return_value=1), close=AsyncMock())
        bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()), delete_webhook=AsyncMock())
        blacklist = MagicMock()
        batches = MagicMock()
        with (
            patch.dict("os.environ", {
                "GUIDER_BOT_TOKEN": "token", "DATABASE_URL": "postgresql://test",
                "BOT_MODE": "webhook", "WEBHOOK_HOST": "https://example.com",
                "WEBHOOK_PATH": "/hook",
            }, clear=True),
            patch.object(guider.SharedConfig, "get", return_value=""),
            patch.object(guider, "Bot", return_value=bot),
            patch.object(guider.asyncpg, "create_pool", new=AsyncMock(return_value=db)),
            patch.object(guider, "blacklist_store_from_pool", return_value=blacklist),
            patch.object(guider, "batch_store_from_pool", return_value=batches),
            patch.object(guider, "run_webhook", new=AsyncMock()) as run,
            patch.object(guider.dp, "start_polling", new=AsyncMock()) as poll,
        ):
            await guider.main()
        run.assert_awaited_once_with(
            bot, ("https://example.com/hook", "/hook", "0.0.0.0", 8080),
            blacklist, batches,
        )
        poll.assert_not_awaited()
        bot.delete_webhook.assert_not_awaited()
        db.close.assert_awaited_once()
        bot.session.close.assert_awaited_once()

    async def test_registration_failure_cleans_up(self):
        bot = MagicMock(set_webhook=AsyncMock(side_effect=RuntimeError("telegram offline")))
        runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        site = SimpleNamespace(start=AsyncMock())
        with (
            patch.object(guider, "webhook_app", return_value=MagicMock()),
            patch.object(guider.web, "AppRunner", return_value=runner),
            patch.object(guider.web, "TCPSite", return_value=site),
            self.assertRaisesRegex(RuntimeError, "telegram offline"),
        ):
            await guider.run_webhook(
                bot, ("https://example.com/hook", "/hook", "127.0.0.1", 9000),
                MagicMock(), MagicMock(),
            )
        runner.cleanup.assert_awaited_once()

    async def test_rejected_registration_cleans_up(self):
        bot = MagicMock(set_webhook=AsyncMock(return_value=False))
        runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        site = SimpleNamespace(start=AsyncMock())
        with (
            patch.object(guider, "webhook_app", return_value=MagicMock()),
            patch.object(guider.web, "AppRunner", return_value=runner),
            patch.object(guider.web, "TCPSite", return_value=site),
            self.assertRaisesRegex(RuntimeError, "未确认 webhook 注册"),
        ):
            await guider.run_webhook(
                bot, ("https://example.com/hook", "/hook", "127.0.0.1", 9000),
                MagicMock(), MagicMock(),
            )
        runner.cleanup.assert_awaited_once()
