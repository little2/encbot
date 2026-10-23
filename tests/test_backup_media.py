import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / "main_taohua.py"


def load_functions(names, namespace):
    tree = ast.parse(SOURCE.read_text())
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]
    exec(
        compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"),
        namespace,
    )
    return namespace


class BackupMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_is_deduplicated_during_cooldown(self):
        send_notification = AsyncMock()
        clock = Mock(return_value=100.0)
        namespace = load_functions(
            {"_schedule_operational_notification"},
            {
                "asyncio": asyncio,
                "time": SimpleNamespace(monotonic=clock),
                "OPERATIONAL_NOTIFICATION_COOLDOWN_SECONDS": 600.0,
                "OPERATIONAL_NOTIFICATION_LAST_SENT": {},
                "OPERATIONAL_NOTIFICATION_TASKS": set(),
                "_send_operational_notification": send_notification,
            },
        )

        first = namespace["_schedule_operational_notification"](
            "first", cooldown_key="backup-media:error"
        )
        second = namespace["_schedule_operational_notification"](
            "second", cooldown_key="backup-media:error"
        )
        await asyncio.gather(*tuple(namespace["OPERATIONAL_NOTIFICATION_TASKS"]))
        await asyncio.sleep(0)

        self.assertTrue(first)
        self.assertFalse(second)
        send_notification.assert_awaited_once_with("first")
        self.assertEqual(namespace["OPERATIONAL_NOTIFICATION_TASKS"], set())

    async def test_notification_failure_is_contained(self):
        async def fail_in_thread(*_args):
            raise RuntimeError("notification unavailable")

        namespace = load_functions(
            {"_send_operational_notification"},
            {
                "asyncio": SimpleNamespace(
                    to_thread=fail_in_thread,
                    CancelledError=asyncio.CancelledError,
                ),
                "SharedConfig": SimpleNamespace(_download_from_api=Mock()),
            },
        )

        with patch("builtins.print") as print_mock:
            await namespace["_send_operational_notification"]("failure")

        print_mock.assert_called_once()

    async def test_concurrent_media_reuses_backup_name_lookup(self):
        backup_client = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="backup_test"))
        )

        class BackupClientContext:
            async def __aenter__(self):
                return backup_client

            async def __aexit__(self, *_args):
                return False

        bot_factory = Mock(return_value=BackupClientContext())

        async def telegram_call(_label, operation, **_kwargs):
            return await operation()

        schedule_notification = Mock()
        namespace = load_functions(
            {"_send_media_to_backup_bot"},
            {
                "asyncio": asyncio,
                "Message": object,
                "BACKUP_BOT_TOKEN": "token",
                "BACKUP_BOT_NAME": "",
                "BACKUP_BOT_NAME_LOCK": asyncio.Lock(),
                "Bot": bot_factory,
                "_telegram_call_with_retry": telegram_call,
                "_schedule_operational_notification": schedule_notification,
            },
        )
        messages = [
            SimpleNamespace(
                chat=SimpleNamespace(id=1),
                message_id=message_id,
                copy_to=AsyncMock(),
            )
            for message_id in (10, 11)
        ]

        await asyncio.gather(
            *(namespace["_send_media_to_backup_bot"](message) for message in messages)
        )

        bot_factory.assert_called_once_with(token="token")
        backup_client.get_me.assert_awaited_once()
        for message in messages:
            message.copy_to.assert_awaited_once_with(chat_id="@backup_test")
        schedule_notification.assert_not_called()

    async def test_disabled_error_schedules_one_background_notification(self):
        async def fail_copy(*_args, **_kwargs):
            raise RuntimeError("USER_BOT_TO_BOT_DISABLED")

        schedule_notification = Mock()
        namespace = load_functions(
            {"_send_media_to_backup_bot"},
            {
                "asyncio": asyncio,
                "Message": object,
                "BACKUP_BOT_TOKEN": "token",
                "BACKUP_BOT_NAME": "backup_test",
                "BACKUP_BOT_NAME_LOCK": asyncio.Lock(),
                "Bot": Mock(),
                "_telegram_call_with_retry": fail_copy,
                "_schedule_operational_notification": schedule_notification,
            },
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_id=10,
            copy_to=AsyncMock(),
        )

        await namespace["_send_media_to_backup_bot"](message)

        schedule_notification.assert_called_once_with(
            "Bot backup_test - media copy failed: USER_BOT_TO_BOT_DISABLED",
            cooldown_key="backup-media:user-bot-to-bot-disabled",
        )

    async def test_media_copy_cancellation_propagates(self):
        async def cancel_copy(*_args, **_kwargs):
            raise asyncio.CancelledError

        schedule_notification = Mock()
        namespace = load_functions(
            {"_send_media_to_backup_bot"},
            {
                "asyncio": asyncio,
                "Message": object,
                "BACKUP_BOT_TOKEN": "token",
                "BACKUP_BOT_NAME": "backup_test",
                "BACKUP_BOT_NAME_LOCK": asyncio.Lock(),
                "Bot": Mock(),
                "_telegram_call_with_retry": cancel_copy,
                "_schedule_operational_notification": schedule_notification,
            },
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_id=10,
            copy_to=AsyncMock(),
        )

        with self.assertRaises(asyncio.CancelledError):
            await namespace["_send_media_to_backup_bot"](message)
        schedule_notification.assert_not_called()
