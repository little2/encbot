"""Airport application integration with real temporary stores and fake Telegram I/O."""
import __future__
import ast
import asyncio
import copy
from datetime import datetime, timezone
from html import escape
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from utils.airport_async_stores import create_airport_stores
from utils.database_admin import DatabaseAdminCommands
from utils.format_utils import FormatUtils
from utils.user_manager import AsyncUserManager

SOURCE = Path(__file__).resolve().parents[1] / "main_airport.py"
SOURCE_TREE = ast.parse(SOURCE.read_text())
NOW = 1_700_000_000


def load_functions(names, namespace):
    # Importing the whole entrypoint loads remote configuration and creates bots.
    nodes = [copy.deepcopy(n) for n in SOURCE_TREE.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    return namespace


class AirportDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "airport.sqlite3"
        self.stores = await create_airport_stores(backend="sqlite", sqlite_path=self.path)
        self.addAsyncCleanup(self.stores.close)
        clock = patch("utils.async_stores.time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.bot = SimpleNamespace(id=999, send_message=AsyncMock(),
                                   ban_chat_member=AsyncMock(), unban_chat_member=AsyncMock())

        async def telegram_call(label, fn, **kwargs):
            return await fn()

        self.telegram_call = telegram_call
        self.ns = dict(asyncio=asyncio, os=os, datetime=datetime, APP_TIMEZONE=timezone.utc,
                       app_now=lambda: datetime.fromtimestamp(NOW, timezone.utc),
                       database_stores=self.stores, user_expire_cache=self.stores.users,
                       blacklist_store=self.stores.blacklist, batch_store=self.stores.batches,
                       batch_view_store=self.stores.batch_views,
                       received_media_store=self.stores.received_media,
                       shared_invite_link_store=self.stores.shared_invite_links,
                       bot=self.bot, bot_name="airport_test", ADMIN_USER_IDS={99},
                       MESSAGE_REWARD_LOCKS={}, TAKEOFF_USER_LOCKS={},
                       MESSAGE_REWARD_MINUTES=60, MEDIA_VIEW_CONSUMPTION_MINUTES=60,
                       IGNORED_TEXT_SUBSTRINGS=[], FormatUtils=FormatUtils, escape=escape)

    async def test_initialize_binds_six_stores_and_atomic_manager(self):
        states = {name: {1: "pending"} for name in (
            "PENDING_AIRPORT_JOIN_INVITES", "AIRPORT_QUIZ_PROGRESS",
            "AIRPORT_QUIZ_RETRY_AT", "AIRPORT_QUIZ_PASSED_UNTIL")}
        factory = AsyncMock(return_value=self.stores)
        admin = SimpleNamespace(database_stores=None, register=Mock())
        self.ns.update(states, create_airport_stores=factory, AsyncUserManager=AsyncUserManager,
                       user_expire_db_path=self.path, database_admin_commands=admin,
                       _telegram_call_with_retry=self.telegram_call,
                       AIRPORT_LOBBY_GROUP_ID=0, TERMINAL_CHANNEL_ID=0,
                       AIRPORT_DUTY_FREE_GROUP_ID=0, AIRPORT_FLIGHT_BOARD_CHANNEL_ID=0)
        load_functions({"_initialize_database"}, self.ns)
        with patch.dict(os.environ, {"AIRPORT_DATABASE_BACKEND": "sqlite", "AIRPORT_DATABASE_URL": ""}):
            await self.ns["_initialize_database"]()
        factory.assert_awaited_once_with(backend="sqlite", sqlite_path=self.path, database_url="")
        for name, store in (("user_expire_cache", self.stores.users),
                            ("blacklist_store", self.stores.blacklist),
                            ("batch_store", self.stores.batches),
                            ("batch_view_store", self.stores.batch_views),
                            ("received_media_store", self.stores.received_media),
                            ("shared_invite_link_store", self.stores.shared_invite_links)):
            self.assertIs(self.ns[name], store)
        self.assertIs(admin.database_stores, self.stores)
        admin.register.assert_not_called()
        await self.stores.users.update(1, NOW + 600)
        await self.ns["_ban_user"](1, "test", 99, target_chats=[])
        self.assertTrue(await self.stores.blacklist.is_blocked(1))
        self.assertIsNone(await self.stores.users.get(1))
        await self.ns["_delete_inactive_user_data"](1)
        self.assertTrue(all(1 not in state for state in states.values()))

    async def test_concurrent_messages_only_reward_once(self):
        load_functions({"on_reward_group_message"}, self.ns)
        message = SimpleNamespace(from_user=SimpleNamespace(id=1, is_bot=False), text="有效发言")
        await asyncio.gather(*(self.ns["on_reward_group_message"](message) for _ in range(5)))
        user = await self.stores.users.get(1)
        self.assertEqual(user.expire_timestamp, NOW + 3600)
        self.assertEqual(user.group_message_timestamp, NOW)

    async def test_inactive_snapshot_excludes_admin_bot_and_blacklist(self):
        self.ns["INACTIVE_EXPIRE_DAYS"] = 0
        load_functions({"_inactive_cutoff_timestamp", "_is_inactive_candidate", "_get_inactive_candidates"}, self.ns)
        for user_id in (1, 2, 3, 99, 999):
            await self.stores.users.update(user_id, NOW - user_id)
        await self.stores.blacklist.ban(3, "blocked", 99)
        self.assertEqual([uid for uid, _ in await self.ns["_get_inactive_candidates"](NOW)], [2, 1])

    async def test_hot_command_reads_current_stores(self):
        await self.stores.batches.upsert_channel_location("batch", -100, 1, "A&B")
        await self.stores.batch_views.record("batch", 1)
        load_functions({"_build_hot_message", "cmd_hot"}, self.ns)
        message = SimpleNamespace(reply=AsyncMock())
        await self.ns["cmd_hot"](message)
        text = message.reply.await_args.args[0]
        self.assertIn("A&amp;B", text)
        self.assertIn("https://t.me/airport_test?start=batch", text)
        self.assertIn("1 次", text)

    async def test_postgres_manual_and_scheduled_maintenance_never_use_sqlite(self):
        postgres = SimpleNamespace(backend="postgresql")
        admin = DatabaseAdminCommands(bot=self.bot, backup_lock=asyncio.Lock(),
                                     database_path=self.path, key_man_id=99, admin_user_ids={99},
                                     telegram_call=self.telegram_call, app_now=self.ns["app_now"],
                                     database_stores=postgres)
        message = SimpleNamespace(from_user=SimpleNamespace(id=99), reply=AsyncMock())
        with patch("sqlite3.connect", side_effect=AssertionError("must not open SQLite")):
            for command in (admin.backup, admin.restore, admin.clear_media):
                await command(message)
            self.ns.update(database_stores=postgres, _send_daily_maintenance_notice=AsyncMock())
            load_functions({"_run_scheduled_backup"}, self.ns)
            self.assertFalse(await self.ns["_run_scheduled_backup"]())
        self.assertEqual(message.reply.await_count, 3)
        self.ns["_send_daily_maintenance_notice"].assert_awaited_once()

    async def prepare_takeoff(self, *, quantity=2):
        await self.stores.users.update(1, NOW + 36000, group_message_timestamp=NOW)
        callback = SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=10),
                                   from_user=SimpleNamespace(id=1), answer=AsyncMock())
        parsed = dict(items=[{}] * quantity, user_id=2, valid_until="99991231235959")
        self.ns.update(_extract_takeoff_code=lambda msg: "token", _ceil_div=lambda a, b: (a+b-1)//b,
                       UtfConverter=SimpleNamespace(unicode_cjk_to_telegram=lambda value: value,
                                                    parse_file_token=lambda value: parsed),
                       _get_first_takeoff_batch_id=AsyncMock(return_value=None),
                       _increment_takeoff_count=AsyncMock(return_value=0), MEDIA_UPLOAD_EXTEND_MINUTES=60)
        load_functions({"on_takeoff"}, self.ns)
        return callback

    async def test_failed_delivery_refunds_without_overwriting_new_rewards(self):
        callback = await self.prepare_takeoff()

        async def fail(*args):
            await self.stores.users.extend_minutes(1, 30)
            raise RuntimeError("delivery failed")

        self.ns["extract_encode"] = fail
        await self.ns["on_takeoff"](callback)
        self.assertEqual((await self.stores.users.get(1)).expire_timestamp, NOW + 37800)
        self.bot.send_message.assert_not_awaited()

    async def test_rejected_token_refunds_once_even_if_callback_answer_fails(self):
        callback = await self.prepare_takeoff()
        self.ns["extract_encode"] = AsyncMock(return_value=dict(ok=False, reason="flash_used"))
        callback.answer.side_effect = RuntimeError("callback expired")
        with self.assertRaisesRegex(RuntimeError, "callback expired"):
            await self.ns["on_takeoff"](callback)
        self.assertEqual((await self.stores.users.get(1)).expire_timestamp, NOW + 36000)

    async def test_partial_refund_followed_by_notice_failure_does_not_refund_twice(self):
        callback = await self.prepare_takeoff()
        self.ns["extract_encode"] = AsyncMock(return_value=dict(ok=True, skipped_count=1))
        self.bot.send_message.side_effect = RuntimeError("notice failed")
        await self.ns["on_takeoff"](callback)
        self.assertEqual((await self.stores.users.get(1)).expire_timestamp, NOW + 36000)

    async def test_partial_delivery_charges_only_delivered_media(self):
        callback = await self.prepare_takeoff()
        self.ns["extract_encode"] = AsyncMock(return_value=dict(ok=True, skipped_count=1))
        await self.ns["on_takeoff"](callback)
        self.assertEqual((await self.stores.users.get(1)).expire_timestamp, NOW + 32400)
        self.bot.send_message.assert_awaited_once()


class AirportConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_does_not_inherit_taohua_postgres_configuration(self):
        factory = AsyncMock(side_effect=RuntimeError("factory reached"))
        ns = load_functions({"_initialize_database"}, dict(
            os=os, create_airport_stores=factory, user_expire_db_path="airport.sqlite3"))
        with patch.dict(os.environ, {"DATABASE_BACKEND": "postgresql",
                                     "DATABASE_URL": "postgresql://taohua.invalid/taohua"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "factory reached"):
                await ns["_initialize_database"]()
        factory.assert_awaited_once_with(backend="sqlite", sqlite_path="airport.sqlite3", database_url="")

    async def test_missing_airport_dsn_fails_instead_of_using_taohua_database(self):
        ns = load_functions({"_initialize_database"}, dict(
            os=os, create_airport_stores=create_airport_stores, user_expire_db_path="unused.sqlite3"))
        with patch.dict(os.environ, {"AIRPORT_DATABASE_BACKEND": "postgresql",
                                     "DATABASE_URL": "postgresql://taohua.invalid/taohua"}, clear=True):
            with self.assertRaisesRegex(ValueError, "DATABASE_URL is required"):
                await ns["_initialize_database"]()


class AirportLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def namespace(self):
        async def worker(*args):
            await asyncio.Event().wait()

        ns = dict(asyncio=asyncio, os=os, bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="airport_test")),
            session=SimpleNamespace(close=AsyncMock())),
            database_stores=SimpleNamespace(close=AsyncMock()), _initialize_database=AsyncMock(),
            say_hello_to_x_man=AsyncMock(), reload_config=AsyncMock(),
            dp=SimpleNamespace(start_polling=AsyncMock()), BACKUP_BOT_TOKEN="", MEDIA_WORKER_COUNT=1,
            _media_worker=worker, _media_forward_worker=worker, _daily_maintenance_worker=worker,
            _daily_hot_worker=worker, ENCODED_SEND_TASKS=set())
        return load_functions({"main", "_report_backup_bot_exit"}, ns)

    async def test_database_initialization_failure_closes_session(self):
        ns = self.namespace()
        ns["database_stores"] = None
        ns["_initialize_database"].side_effect = RuntimeError("database unavailable")
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            await ns["main"]()
        ns["bot"].get_me.assert_not_awaited()
        ns["bot"].session.close.assert_awaited_once()

    async def test_telegram_startup_failure_closes_database(self):
        ns = self.namespace()
        ns["bot"].get_me.side_effect = RuntimeError("Telegram unavailable")
        await ns["main"]()
        ns["database_stores"].close.assert_awaited_once()
        ns["bot"].session.close.assert_awaited_once()

    async def test_polling_exit_stops_background_writes_before_closing_database(self):
        ns = self.namespace()
        stopped = asyncio.Event()

        async def background_write():
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        task = asyncio.create_task(background_write())
        await asyncio.sleep(0)
        ns["ENCODED_SEND_TASKS"].add(task)

        async def close():
            self.assertTrue(stopped.is_set())

        ns["database_stores"].close.side_effect = close
        with patch.dict(os.environ, {"VIDEO_BOT_ENABLED": "false"}):
            await ns["main"]()
        ns["database_stores"].close.assert_awaited_once()
        ns["bot"].session.close.assert_awaited_once()

    async def test_polling_cancellation_closes_database_and_session(self):
        ns = self.namespace()
        ns["dp"].start_polling.side_effect = asyncio.CancelledError()
        with patch.dict(os.environ, {"VIDEO_BOT_ENABLED": "false"}):
            with self.assertRaises(asyncio.CancelledError):
                await ns["main"]()
        ns["database_stores"].close.assert_awaited_once()
        ns["bot"].session.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
