"""Airport application integration with real temporary stores and fake Telegram I/O."""
import __future__
import ast
import asyncio
import copy
from contextlib import closing
from datetime import datetime, timezone
from html import escape
import os
import secrets
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from utils.airport_async_stores import create_airport_stores
from utils.postgres_backup import get_backup_directory
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
        with patch.dict(os.environ, {"DATABASE_BACKEND": "sqlite", "DATABASE_URL": ""}):
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

    async def test_low_upload_snapshot_filters_expiry_upload_count_and_exempt_users(self):
        self.ns["LOW_UPLOAD_CLEANUP_LIMIT"] = 30
        load_functions({"_get_low_upload_candidates"}, self.ns)
        await self.stores.users.update(1, NOW + 100)
        await self.stores.users.update(2, NOW - 86400)
        await self.stores.users.update(3, NOW - 86401)
        await self.stores.users.update(4, NOW + 500)
        await self.stores.users.update(7, NOW + 600)
        await self.stores.users.update(99, NOW + 700)
        await self.stores.users.update(999, NOW + 800)
        await self.stores.blacklist.ban(7, "blocked", 99)
        for index in range(30):
            await self.stores.batches.upsert_channel_location(
                f"batch-{index}", -100, index, uploader_user_id=4
            )

        candidates = await self.ns["_get_low_upload_candidates"](NOW)

        self.assertEqual(
            [(user_id, count) for user_id, _, count in candidates],
            [(2, 0), (1, 0)],
        )

    async def test_low_upload_cleanup_removes_candidates_and_reports_counts(self):
        await self.stores.users.update(1, NOW + 3600)
        await self.stores.batches.upsert_channel_location(
            "low-upload", -100, 1, uploader_user_id=1
        )
        removed_chats = []

        async def remove(chat_id, user_id, chat_name):
            removed_chats.append((chat_id, user_id, chat_name))
            return True, f"{chat_name}已移出", False

        async def delete(user_id):
            await self.stores.users.remove(user_id)

        self.ns.update(
            LOW_UPLOAD_CLEANUP_LIMIT=30,
            INACTIVE_CLEANUP_LOCK=asyncio.Lock(),
            TERMINAL_CHANNEL_ID=-101,
            AIRPORT_LOBBY_GROUP_ID=-102,
            AIRPORT_DUTY_FREE_GROUP_ID=-103,
            _remove_inactive_user_from_chat=AsyncMock(side_effect=remove),
            _delete_inactive_user_data=AsyncMock(side_effect=delete),
        )
        load_functions({
            "_get_low_upload_candidates",
            "_execute_low_upload_cleanup",
        }, self.ns)
        message = SimpleNamespace(reply=AsyncMock())

        result = await self.ns["_execute_low_upload_cleanup"](message)

        self.assertTrue(result)
        self.assertEqual(
            removed_chats,
            [(-101, 1, "飞机场"), (-102, 1, "航站大厅"), (-103, 1, "免税店")],
        )
        self.ns["_delete_inactive_user_data"].assert_awaited_once_with(1)
        self.assertIsNone(await self.stores.users.get(1))
        report = message.reply.await_args.args[0]
        self.assertIn("已移除：1 人", report)
        self.assertIn("1｜1｜已移除", report)

    async def run_cleanup_with_results(self, results, *, duty_free_id=-103, delete_error=None):
        await self.stores.users.update(1, NOW - 86401)
        calls = []
        async def remove(chat_id, user_id, name):
            calls.append(name)
            return results[len(calls) - 1]
        async def delete(user_id):
            self.assertEqual(len(calls), 3 if duty_free_id else 2)
            if delete_error:
                raise delete_error
            await self.stores.users.remove(user_id)
        self.ns.update(
            INACTIVE_EXPIRE_DAYS=1, INACTIVE_CLEANUP_LOCK=asyncio.Lock(), KEY_MAN_ID=0,
            TERMINAL_CHANNEL_ID=-101, AIRPORT_LOBBY_GROUP_ID=-102,
            AIRPORT_DUTY_FREE_GROUP_ID=duty_free_id,
            _remove_inactive_user_from_chat=AsyncMock(side_effect=remove),
            _delete_inactive_user_data=AsyncMock(side_effect=delete),
            _telegram_call_with_retry=self.telegram_call,
        )
        load_functions({'_execute_inactive_cleanup', '_inactive_cutoff_timestamp',
                        '_is_inactive_candidate', '_get_inactive_candidates'}, self.ns)
        message = SimpleNamespace(reply=AsyncMock())
        await self.ns['_execute_inactive_cleanup'](message)
        return calls, message.reply.await_args.args[0]

    async def test_cleanup_checks_all_chats_before_deletion_despite_participant_errors(self):
        for error_index in (None, 0, 1, 2, 'all'):
            with self.subTest(error_index=error_index):
                results = [(False, 'PARTICIPANT_ID', True)
                           if error_index == 'all' or i == error_index else (True, '已移出', False)
                           for i in range(3)]
                calls, summary = await self.run_cleanup_with_results(results)
                self.assertEqual(calls, ['飞机场', '航站大厅', '免税店'])
                self.ns['_delete_inactive_user_data'].assert_awaited_once_with(1)
                self.assertIsNone(await self.stores.users.get(1))
                self.assertIn('成功：1 人', summary)

    async def test_cleanup_keeps_record_if_any_chat_fails_and_still_checks_duty_free(self):
        for failure_index in range(3):
            with self.subTest(failure_index=failure_index):
                results = [(False, '权限不足', False) if i == failure_index
                           else (False, 'PARTICIPANT_ID', True) for i in range(3)]
                calls, summary = await self.run_cleanup_with_results(results)
                self.assertEqual(calls, ['飞机场', '航站大厅', '免税店'])
                self.ns['_delete_inactive_user_data'].assert_not_awaited()
                self.assertIsNotNone(await self.stores.users.get(1))
                self.assertIn('失败：1 人', summary)

    async def test_cleanup_allows_unconfigured_duty_free_after_checking_required_chats(self):
        calls, _ = await self.run_cleanup_with_results(
            [(False, 'PARTICIPANT_ID', True), (True, '原本不在群内', False)], duty_free_id=0)
        self.assertEqual(calls, ['飞机场', '航站大厅'])
        self.assertIsNone(await self.stores.users.get(1))

    async def test_cleanup_database_failure_retains_record_after_all_chats_checked(self):
        calls, summary = await self.run_cleanup_with_results(
            [(False, 'PARTICIPANT_ID', True)] * 3, delete_error=RuntimeError('database offline'))
        self.assertEqual(calls, ['飞机场', '航站大厅', '免税店'])
        self.assertIsNotNone(await self.stores.users.get(1))
        self.assertIn('失败：1 人', summary)
        self.assertIn('成功：0 人', summary)

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
        with patch("sqlite3.connect", side_effect=AssertionError("must not open SQLite")), \
             patch.dict(os.environ, {"POSTGRES_BACKUP_ENABLED": "false"}):
            for command in (admin.backup, admin.restore, admin.clear_media):
                await command(message)
            self.ns.update(database_stores=postgres, _send_daily_maintenance_notice=AsyncMock())
            load_functions({"_run_scheduled_backup"}, self.ns)
            self.assertFalse(await self.ns["_run_scheduled_backup"]())
        self.assertEqual(message.reply.await_count, 3)
        self.ns["_send_daily_maintenance_notice"].assert_awaited_once()

    def prepare_scheduled_postgres_backup(self):
        async def dump(dsn, destination, **kwargs):
            self.assertEqual(dsn, "postgresql://airport.invalid/airport")
            destination.write_bytes(b"test archive")
            destination.chmod(0o600)
            return {"sha256": "a" * 64}

        self.backup_dir = self.path.parent / 'retained-backups'
        settings = patch.dict(os.environ, {'BACKUP_DIR': str(self.backup_dir)})
        settings.start()
        self.addCleanup(settings.stop)
        self.bot.send_document = AsyncMock()
        self.ns.update(database_stores=SimpleNamespace(backend="postgresql"), KEY_MAN_ID=99,
                       BACKUP_LOCK=asyncio.Lock(), tempfile=tempfile, Path=Path, secrets=secrets,
                       FSInputFile=FSInputFile, get_backup_directory=get_backup_directory,
                       create_postgres_backup=AsyncMock(side_effect=dump),
                       _telegram_call_with_retry=self.telegram_call, _format_file_size=str,
                       _send_daily_maintenance_notice=AsyncMock())
        load_functions({"_run_scheduled_backup"}, self.ns)

    async def test_enabled_postgres_daily_backup_uploads_archive(self):
        self.prepare_scheduled_postgres_backup()

        async def upload(**kwargs):
            self.assertEqual(Path(kwargs['document'].path).read_bytes(), b'test archive')
            self.assertEqual(Path(kwargs['document'].path).stat().st_mode & 0o777, 0o600)

        self.bot.send_document.side_effect = upload
        with patch.dict(os.environ, {"POSTGRES_BACKUP_ENABLED": "true",
                                     "DATABASE_URL": "postgresql://airport.invalid/airport"}), \
             patch("sqlite3.connect", side_effect=AssertionError("must not open SQLite")):
            self.assertTrue(await self.ns["_run_scheduled_backup"]())
        self.bot.send_document.assert_awaited_once()
        sent = self.bot.send_document.await_args.kwargs
        self.assertEqual(sent['chat_id'], 99)
        self.assertTrue(sent['document'].filename.endswith('.dump'))
        self.assertIn('PostgreSQL', sent['caption'])
        self.assertIn('a' * 64, sent['caption'])
        self.assertTrue(Path(sent['document'].path).is_file())

    async def test_postgres_backup_upload_failure_returns_false_and_redacts_details(self):
        self.prepare_scheduled_postgres_backup()
        self.bot.send_document.side_effect = RuntimeError('secret-connection-details')
        with patch.dict(os.environ, {"POSTGRES_BACKUP_ENABLED": "true",
                                     "DATABASE_URL": "postgresql://airport.invalid/airport"}):
            self.assertFalse(await self.ns["_run_scheduled_backup"]())
        notice = self.ns['_send_daily_maintenance_notice'].await_args.args[0]
        self.assertNotIn('secret-connection-details', notice)
        self.assertIn('定时清理仍会执行', notice)
        self.assertIn('本地备份已保留', notice)
        self.assertEqual(len(list(self.backup_dir.glob('*.dump'))), 1)

    async def test_backup_is_sent_directly_to_key_man(self):
        self.prepare_scheduled_postgres_backup()
        with patch.dict(os.environ, {'POSTGRES_BACKUP_ENABLED': 'true',
                                     'DATABASE_URL': 'postgresql://airport.invalid/airport'}):
            self.assertTrue(await self.ns['_run_scheduled_backup']())
        self.assertEqual(self.bot.send_document.await_args.kwargs['chat_id'], 99)

    async def test_invalid_key_man_cancels_backup_and_upload(self):
        self.prepare_scheduled_postgres_backup()
        for value in (None, '', 0, -100, '@someone', 'invalid', 2**63):
            self.ns['KEY_MAN_ID'] = value
            with self.subTest(value=value), patch.dict(os.environ, {'POSTGRES_BACKUP_ENABLED': 'true'}):
                self.assertFalse(await self.ns['_run_scheduled_backup']())
        self.ns['create_postgres_backup'].assert_not_awaited()
        self.bot.send_document.assert_not_awaited()

    async def test_same_second_backups_use_different_local_filenames(self):
        self.prepare_scheduled_postgres_backup()
        with patch.dict(os.environ, {'POSTGRES_BACKUP_ENABLED': 'true',
                                     'DATABASE_URL': 'postgresql://airport.invalid/airport'}):
            self.assertTrue(await self.ns['_run_scheduled_backup']())
            self.assertTrue(await self.ns['_run_scheduled_backup']())
        self.assertEqual(len(list(self.backup_dir.glob('*.dump'))), 2)

    async def test_local_backup_failure_never_uploads(self):
        self.prepare_scheduled_postgres_backup()
        self.ns['create_postgres_backup'].side_effect = RuntimeError('backup failed')
        with patch.dict(os.environ, {'POSTGRES_BACKUP_ENABLED': 'true'}):
            self.assertFalse(await self.ns['_run_scheduled_backup']())
        self.bot.send_document.assert_not_awaited()

    async def test_sqlite_backup_is_private_and_retained_after_upload(self):
        self.prepare_scheduled_postgres_backup()
        self.ns.update(database_stores=self.stores, user_expire_db_path=self.path,
                       _create_sqlite_backup=DatabaseAdminCommands._create_sqlite_backup)
        self.assertTrue(await self.ns['_run_scheduled_backup']())
        path = Path(self.bot.send_document.await_args.kwargs['document'].path)
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone(), ('ok',))

    async def test_daily_cleanup_runs_regardless_of_backup_result_or_exception(self):
        self.ns.update(_send_daily_maintenance_notice=AsyncMock(), _run_scheduled_backup=AsyncMock(),
                       _execute_inactive_cleanup=AsyncMock(), DAILY_MAINTENANCE_HOUR=4,
                       DAILY_MAINTENANCE_MINUTE=0, timedelta=__import__('datetime').timedelta)
        load_functions({'_daily_maintenance_worker', '_next_daily_maintenance_time'}, self.ns)
        for outcome in (False, True, RuntimeError('backup failed')):
            self.ns['_run_scheduled_backup'].side_effect = outcome if isinstance(outcome, Exception) else None
            self.ns['_run_scheduled_backup'].return_value = outcome
            self.ns['_execute_inactive_cleanup'].reset_mock()
            with patch('asyncio.sleep', new=AsyncMock(side_effect=[None, asyncio.CancelledError()])):
                with self.assertRaises(asyncio.CancelledError):
                    await self.ns['_daily_maintenance_worker']()
            self.ns['_execute_inactive_cleanup'].assert_awaited_once()

    async def test_daily_cleanup_does_not_run_when_worker_is_cancelled_during_backup(self):
        self.ns.update(_send_daily_maintenance_notice=AsyncMock(),
                       _run_scheduled_backup=AsyncMock(side_effect=asyncio.CancelledError()),
                       _execute_inactive_cleanup=AsyncMock(), DAILY_MAINTENANCE_HOUR=4,
                       DAILY_MAINTENANCE_MINUTE=0, timedelta=__import__('datetime').timedelta)
        load_functions({'_daily_maintenance_worker', '_next_daily_maintenance_time'}, self.ns)
        with patch('asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                await self.ns['_daily_maintenance_worker']()
        self.ns['_execute_inactive_cleanup'].assert_not_awaited()

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
    def test_keyboard_omits_only_tower_button_when_username_is_missing(self):
        ns = load_functions({'_build_keyboard'}, dict(
            bot_name=' @airport_test ', InlineKeyboardButton=InlineKeyboardButton,
            InlineKeyboardMarkup=InlineKeyboardMarkup))
        self.assertEqual(ns['_build_keyboard']().inline_keyboard[0][1].url,
                         'https://t.me/airport_test')
        for invalid in ('', None, ' @ '):
            ns['bot_name'] = invalid
            buttons = ns['_build_keyboard']().inline_keyboard[0]
            self.assertEqual([button.callback_data for button in buttons],
                             ['takeoff:ban', 'takeoff:fly'])
            self.assertTrue(all(button.url is None for button in buttons))

    async def test_generic_database_settings_are_passed_to_factory(self):
        factory = AsyncMock(side_effect=RuntimeError("factory reached"))
        ns = load_functions({"_initialize_database"}, dict(
            os=os, create_airport_stores=factory, user_expire_db_path="airport.sqlite3"))
        with patch.dict(os.environ, {"DATABASE_BACKEND": "postgresql",
                                     "DATABASE_URL": "postgresql://airport.invalid/airport"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "factory reached"):
                await ns["_initialize_database"]()
        factory.assert_awaited_once_with(backend="postgresql", sqlite_path="airport.sqlite3",
                                         database_url="postgresql://airport.invalid/airport")

    async def test_sqlite_is_default_without_database_configuration(self):
        factory = AsyncMock(side_effect=RuntimeError("factory reached"))
        ns = load_functions({"_initialize_database"}, dict(
            os=os, create_airport_stores=factory, user_expire_db_path="airport.sqlite3"))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "factory reached"):
                await ns["_initialize_database"]()
        factory.assert_awaited_once_with(backend="sqlite", sqlite_path="airport.sqlite3", database_url="")

    async def test_postgres_requires_database_url(self):
        ns = load_functions({"_initialize_database"}, dict(
            os=os, create_airport_stores=create_airport_stores, user_expire_db_path="unused.sqlite3"))
        with patch.dict(os.environ, {"DATABASE_BACKEND": "postgresql"}, clear=True):
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
        ns.update(AIRPORT_LOBBY_GROUP_ID=0, TERMINAL_CHANNEL_ID=0, AIRPORT_DUTY_FREE_GROUP_ID=0,
                  AIRPORT_FLIGHT_BOARD_CHANNEL_ID=0, APRON_CHANNEL_IDS=[],
                  ControlPanel=Mock(return_value=SimpleNamespace(check_bot_group_admin_permissions=AsyncMock())),
                  SharedConfig=SimpleNamespace(get_all=lambda: {}), _telegram_call_with_retry=AsyncMock(),
                  BotCommand=Mock(), BotCommandScopeAllPrivateChats=Mock())
        ns['bot'].set_my_commands = AsyncMock()
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

    async def test_missing_telegram_username_stops_before_workers_and_polling(self):
        for username in (None, '', '   '):
            with self.subTest(username=username):
                ns = self.namespace()
                ns['bot'].get_me.return_value = SimpleNamespace(username=username)
                await ns['main']()
                ns['say_hello_to_x_man'].assert_not_awaited()
                ns['dp'].start_polling.assert_not_awaited()
                ns['database_stores'].close.assert_awaited_once()
                ns['bot'].session.close.assert_awaited_once()

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
