import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe

import backup_bot
from utils.telegram_gate import TelegramGate


class BotStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_retries_network_failure_and_closes_session(self):
        bot = SimpleNamespace(
            get_me=AsyncMock(side_effect=[
                TelegramNetworkError(GetMe(), "connection unavailable"),
                SimpleNamespace(username="backup_test"),
            ]),
            set_my_commands=AsyncMock(),
            session=SimpleNamespace(close=AsyncMock(), middleware=Mock()),
        )
        dispatcher = SimpleNamespace(start_polling=AsyncMock())
        ready = AsyncMock()
        gate = TelegramGate(send_min_interval=0)
        # Keep the real retry loop; skip only its backoff delay.
        with (
            patch.dict("os.environ", {"BACKUP_BOT_TOKEN": "test"}),
            patch.object(backup_bot, "Bot", return_value=bot),
            patch.object(backup_bot, "_build_dispatcher", return_value=dispatcher),
            patch.object(backup_bot, "telegram_call", side_effect=gate.call),
            patch("utils.telegram_gate.asyncio.sleep", new=AsyncMock()),
        ):
            await backup_bot.start_backup_bot(on_ready=ready)
        self.assertEqual(bot.get_me.await_count, 2)
        ready.assert_awaited_once_with("backup_test")
        dispatcher.start_polling.assert_awaited_once_with(bot, handle_signals=False)
        bot.session.close.assert_awaited_once()
        bot.session.middleware.assert_called_once()

    async def test_backup_closes_session_when_startup_fails(self):
        bot = SimpleNamespace(
            get_me=AsyncMock(side_effect=RuntimeError("invalid credentials")),
            session=SimpleNamespace(close=AsyncMock(), middleware=Mock()),
        )
        with (
            patch.dict("os.environ", {"BACKUP_BOT_TOKEN": "test"}),
            patch.object(backup_bot, "Bot", return_value=bot),
            self.assertRaisesRegex(RuntimeError, "invalid credentials"),
        ):
            await backup_bot.start_backup_bot()
        bot.session.close.assert_awaited_once()

    def _main_namespace(self):
        # Importing main_taohua loads remote config and persistent stores.
        # Execute only its lifecycle functions with isolated dependencies.
        source = Path(__file__).resolve().parents[1] / "main_taohua.py"
        tree = ast.parse(source.read_text())
        functions = [node for node in tree.body if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name in {"main", "_report_backup_bot_exit"}]
        namespace = {
            "asyncio": asyncio,
            "bot": SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(username="main_test")),
                set_my_commands=AsyncMock(),
                session=SimpleNamespace(close=AsyncMock()),
            ),
            "dp": SimpleNamespace(start_polling=AsyncMock()),
            "_telegram_call_with_retry": TelegramGate(send_min_interval=0).call,
            "say_hello_to_x_man": AsyncMock(),
            "_check_bot_group_admin_permissions": AsyncMock(),
            "BotCommand": Mock(),
            "BotCommandScopeAllPrivateChats": Mock(),
            "BACKUP_BOT_TOKEN": "test",
            "bkbot": SimpleNamespace(session=SimpleNamespace(close=AsyncMock())),
            "X_MAN_BOT_ID": 1,
            "SharedConfig": SimpleNamespace(_download_from_api=Mock(return_value={})),
            "OPERATIONAL_NOTIFICATION_TASKS": set(),
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
        return namespace

    async def test_main_cancels_backup_on_polling_exit(self):
        namespace = self._main_namespace()
        stopped = asyncio.Event()

        async def backup(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def poll(bot):
            await asyncio.sleep(0)

        namespace["dp"].start_polling.side_effect = poll
        with patch.object(backup_bot, "start_backup_bot", side_effect=backup):
            await namespace["main"]()
        self.assertTrue(stopped.is_set())
        namespace["SharedConfig"]._download_from_api.assert_called_once_with(
            {"text": "Bot started"}
        )
        namespace["bot"].session.close.assert_awaited_once()
        namespace["bkbot"].session.close.assert_awaited_once()

    async def test_main_continues_when_startup_notification_fails(self):
        namespace = self._main_namespace()
        namespace["BACKUP_BOT_TOKEN"] = ""
        namespace["SharedConfig"]._download_from_api.side_effect = RuntimeError(
            "notification unavailable"
        )

        await namespace["main"]()

        namespace["dp"].start_polling.assert_awaited_once_with(namespace["bot"])
        namespace["bot"].session.close.assert_awaited_once()

    async def test_main_closes_session_when_initialization_fails(self):
        namespace = self._main_namespace()
        namespace["bot"].get_me.side_effect = RuntimeError("startup failed")
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            await namespace["main"]()
        namespace["bot"].session.close.assert_awaited_once()
        namespace["dp"].start_polling.assert_not_awaited()
