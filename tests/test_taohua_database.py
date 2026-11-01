import ast
import asyncio
import copy
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
import __future__
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from utils.async_stores import create_stores
from utils.database_admin import DatabaseAdminCommands
from utils.format_utils import FormatUtils
from utils.user_manager import AsyncUserManager, UserManager
from utils.user_utils import UserExpireCache
from utils.blacklist_utils import BlacklistStore


MAIN_PATH = Path(__file__).resolve().parents[1] / "main_taohua.py"
NOW = 1_700_000_000


def load_functions(names, namespace):
    # Importing main_taohua contacts remote config and constructs real bots.
    # Exercise its actual functions with isolated database/Telegram dependencies.
    tree = ast.parse(MAIN_PATH.read_text())
    functions = [copy.deepcopy(node) for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in functions:
        node.decorator_list = []
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(MAIN_PATH), "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    return namespace


class TaohuaDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "users.sqlite3"
        self.stores = await create_stores(backend="sqlite", sqlite_path=self.path)
        self.addAsyncCleanup(self.stores.close)
        self.bot = SimpleNamespace(ban_chat_member=AsyncMock(), unban_chat_member=AsyncMock())

        async def telegram_call(label, fn, **kwargs):
            return await fn()

        self.telegram_call = telegram_call
        self.namespace = {
            "asyncio": asyncio,
            "user_expire_cache": self.stores.users,
            "blacklist_store": self.stores.blacklist,
            "app_now": lambda: datetime.fromtimestamp(NOW, timezone.utc),
            "MESSAGE_REWARD_MINUTES": 60,
            "MEDIA_VIEW_CONSUMPTION_MINUTES": 60,
            "MAX_HP_CAPACITY_QUANTITY": 120,
            "MESSAGE_REWARD_LOCKS": {},
            "ADMIN_USER_IDS": {99},
            "INACTIVE_EXPIRE_DAYS": 0,
            "censor": SimpleNamespace(check_content=lambda text: "allowed"),
            "FormatUtils": FormatUtils,
            "bot": SimpleNamespace(id=999),
        }
        self.clock = patch("utils.async_stores.time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def test_initialize_binds_stores_manager_and_existing_admin_handlers(self):
        factory = AsyncMock(return_value=self.stores)
        admin = SimpleNamespace(database_stores=None, register=Mock())
        namespace = {
            "asyncio": asyncio, "create_stores": factory,
            "user_expire_db_path": self.path, "database_admin_commands": admin,
            "AsyncUserManager": AsyncUserManager, "bot": self.bot,
            "_telegram_call_with_retry": self.telegram_call,
            "CHAT_SCHOOL_GROUP_ID": 100, "CHAT_PUBLIC_GROUP_ID": 200,
        }
        load_functions({"_initialize_database"}, namespace)
        await namespace["_initialize_database"]()
        factory.assert_awaited_once_with(sqlite_path=self.path)
        self.assertIs(namespace["user_expire_cache"], self.stores.users)
        self.assertIs(namespace["blacklist_store"], self.stores.blacklist)
        self.assertIs(admin.database_stores, self.stores)
        # Keep original registration order; do not register a second copy.
        admin.register.assert_not_called()
        await self.stores.users.update(1, NOW + 600)
        await namespace["_ban_user"](1, "test", 99, target_chats=[])
        self.assertTrue(await self.stores.blacklist.is_blocked(1))
        self.assertIsNone(await self.stores.users.get(1))

    async def test_concurrent_group_messages_reward_once(self):
        load_functions({"on_reward_group_message"}, self.namespace)
        message = SimpleNamespace(from_user=SimpleNamespace(id=1, is_bot=False), text="有效发言")
        await asyncio.gather(*(self.namespace["on_reward_group_message"](message) for _ in range(5)))
        user = await self.stores.users.get(1)
        self.assertEqual(user.expire_timestamp, NOW + 3600)
        self.assertEqual(user.group_message_timestamp, NOW)

    async def test_me_reads_database_and_blocks_blacklisted_users(self):
        load_functions({"cmd_me", "get_user_status"}, self.namespace)
        message = SimpleNamespace(from_user=SimpleNamespace(id=1), reply=AsyncMock())
        await self.stores.users.update(1, NOW + 7200)
        await self.namespace["cmd_me"](message)
        message.reply.assert_awaited_once()
        self.assertIn("桃气值", message.reply.await_args.args[0])
        message.reply.reset_mock()
        await self.stores.blacklist.ban(1, "blocked", 99)
        await self.namespace["cmd_me"](message)
        message.reply.assert_not_awaited()

    async def test_candidates_use_async_snapshot_and_exclude_admin_bot_and_banned(self):
        load_functions({"_inactive_cutoff_timestamp", "_is_inactive_candidate", "_get_inactive_candidates"}, self.namespace)
        for user_id in (1, 2, 99, 999):
            await self.stores.users.update(user_id, NOW + 60)
        await self.stores.blacklist.ban(2, "blocked", 99)
        candidates = await self.namespace["_get_inactive_candidates"](NOW)
        self.assertEqual([user_id for user_id, _ in candidates], [1])

    async def test_async_manager_deletes_user_state_after_persistence(self):
        state = {1: "state"}
        manager = AsyncUserManager(stores=self.stores, bot=self.bot,
                                   telegram_call=self.telegram_call,
                                   lobby_group_id=0, terminal_channel_id=0,
                                   duty_free_group_id=0, flight_board_channel_id=0,
                                   user_state_stores=(state,))
        await self.stores.users.update(1, NOW + 60)
        await manager.delete_inactive_user_data(1)
        self.assertIsNone(await self.stores.users.get(1))
        self.assertEqual(state, {})

    async def test_legacy_manager_sync_deletion_and_ban_remain_compatible(self):
        path = Path(self.directory.name) / "legacy.sqlite3"
        users, blacklist = UserExpireCache(path), BlacklistStore(path)
        try:
            manager = UserManager(bot=self.bot, user_expire_cache=users, blacklist_store=blacklist,
                                  telegram_call=self.telegram_call,
                                  lobby_group_id=0, terminal_channel_id=0,
                                  duty_free_group_id=0, flight_board_channel_id=0)
            users.update(1, NOW + 60)
            entry, _ = await manager.ban_user(1, "legacy", 99)
            self.assertEqual(entry.user_id, 1)
            self.assertTrue(blacklist.is_blocked(1))
            self.assertIsNone(users.get(1))
            users.update(2, NOW + 60)
            self.assertIsNone(manager.delete_inactive_user_data(2))
            self.assertIsNone(users.get(2))
        finally:
            users.close()
            blacklist.close()

    def test_all_async_store_calls_are_awaited_and_admin_handlers_register_early(self):
        tree = ast.parse(MAIN_PATH.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                if isinstance(receiver, ast.Name) and receiver.id in {"user_expire_cache", "blacklist_store"}:
                    self.assertIsInstance(parents[node], ast.Await, f"Missing await at line {node.lineno}")
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                self.assertFalse(node.value.id == "user_expire_cache" and node.attr == "users")
        registration = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "database_admin_commands" and node.func.attr == "register")
        catchall = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "on_private_text_message")
        self.assertLess(registration.lineno, catchall.lineno)

    def admin(self, stores=None):
        return DatabaseAdminCommands(bot=self.bot, backup_lock=asyncio.Lock(), database_path=self.path,
                                     key_man_id=99, admin_user_ids={99}, telegram_call=self.telegram_call,
                                     app_now=lambda: datetime.fromtimestamp(NOW, timezone.utc),
                                     database_stores=self.stores if stores is None else stores)

    async def test_postgres_admin_commands_never_touch_sqlite(self):
        admin = self.admin(SimpleNamespace(backend="postgresql"))
        for command in (admin.backup, admin.restore, admin.clear_media):
            with self.subTest(command=command.__name__):
                message = SimpleNamespace(from_user=SimpleNamespace(id=99), reply=AsyncMock())
                with patch.object(admin, "_create_sqlite_backup") as backup:
                    await command(message)
                backup.assert_not_called()
                self.assertIn("PostgreSQL", message.reply.await_args.args[0])

    async def test_sqlite_admin_restore_updates_live_stores(self):
        await self.stores.users.update(1, NOW + 60)
        snapshot = Path(self.directory.name) / "restore.sqlite3"
        with closing(sqlite3.connect(snapshot)) as conn, conn:
            conn.execute("CREATE TABLE user_expire (user_id BIGINT PRIMARY KEY, expire_timestamp BIGINT NOT NULL, update_timestamp BIGINT NOT NULL, group_message_timestamp BIGINT NOT NULL DEFAULT 0)")
            conn.execute("INSERT INTO user_expire VALUES (2, ?, ?, 123)", (NOW + 120, NOW))
        self.bot.download = AsyncMock(side_effect=lambda document, destination: shutil.copyfile(snapshot, destination))
        self.bot.send_document = AsyncMock()
        status = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(from_user=SimpleNamespace(id=99), reply=AsyncMock(return_value=status),
                                  reply_to_message=SimpleNamespace(document=SimpleNamespace(file_name="restore.sqlite3")))
        await self.admin().restore(message)
        self.bot.send_document.assert_awaited_once()
        self.assertIn("恢复完成", status.edit_text.await_args.args[0])
        self.assertIsNone(await self.stores.users.get(1))
        self.assertEqual((await self.stores.users.get(2)).group_message_timestamp, 123)

    async def test_failed_restore_rolls_back_including_writes_during_backup_upload(self):
        await self.stores.users.update(1, NOW + 60)
        snapshot = Path(self.directory.name) / "restore.sqlite3"
        with closing(sqlite3.connect(snapshot)) as conn, conn:
            conn.execute("CREATE TABLE user_expire (user_id BIGINT PRIMARY KEY, expire_timestamp BIGINT NOT NULL, update_timestamp BIGINT NOT NULL, group_message_timestamp BIGINT NOT NULL DEFAULT 0)")
            conn.execute("INSERT INTO user_expire VALUES (2, ?, ?, 123)", (NOW + 120, NOW))
        self.bot.download = AsyncMock(side_effect=lambda document, destination: shutil.copyfile(snapshot, destination))

        async def upload(**kwargs):
            await self.stores.users.update(3, NOW + 180)

        self.bot.send_document = AsyncMock(side_effect=upload)
        original_session = self.stores.sqlite_restore_session
        applied = []

        @asynccontextmanager
        async def failing_session():
            async with original_session() as apply:
                async def restore(path):
                    await apply(path)
                    applied.append(path)
                    if len(applied) == 1:
                        raise RuntimeError("injected restore failure")
                yield restore

        status = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(from_user=SimpleNamespace(id=99), reply=AsyncMock(return_value=status),
                                  reply_to_message=SimpleNamespace(document=SimpleNamespace(file_name="restore.sqlite3")))
        with patch.object(self.stores, "sqlite_restore_session", failing_session):
            await self.admin().restore(message)
        self.assertEqual(len(applied), 2)
        self.assertIn("已自动还原", status.edit_text.await_args.args[0])
        self.assertIsNotNone(await self.stores.users.get(1))
        self.assertIsNone(await self.stores.users.get(2))
        self.assertIsNotNone(await self.stores.users.get(3))


if __name__ == "__main__":
    unittest.main()
