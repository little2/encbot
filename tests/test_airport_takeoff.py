import ast
import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock


class AirportTakeoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / "main_airport.py"
        tree = ast.parse(source.read_text())
        nodes = [
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name in {"on_takeoff", "on_takeoff_fly", "on_text"}
        ]
        for node in nodes:
            node.decorator_list = []
        self.now = datetime(2026, 11, 17, tzinfo=timezone.utc)
        self.parsed = {"valid_until": "99991231235959", "user_id": 456}
        self.expire = SimpleNamespace(
            group_message_timestamp=int(self.now.timestamp()),
            expire_timestamp=int(self.now.timestamp()) + 36000,
        )
        self.cache = SimpleNamespace(
            get=AsyncMock(return_value=self.expire),
            consume_minutes=AsyncMock(return_value=self.expire),
            refund_minutes=AsyncMock(),
        )
        self.message = SimpleNamespace(chat=SimpleNamespace(id=-100123), message_id=7)
        self.ns = {
            "Any": Any, "Message": object, "CallbackQuery": object,
            "asyncio": asyncio, "datetime": datetime, "APP_TIMEZONE": timezone.utc,
            "ADMIN_USER_IDS": set(), "app_now": lambda: self.now,
            "UtfConverter": SimpleNamespace(
                unicode_cjk_to_telegram=lambda text: text,
                parse_file_token=lambda token: self.parsed,
            ),
            "MEDIA_VIEW_CONSUMPTION_MINUTES": 10,
            "MESSAGE_REWARD_MINUTES": 1, "MEDIA_UPLOAD_EXTEND_MINUTES": 10,
            "TAKEOFF_USER_LOCKS": {}, "user_expire_cache": self.cache,
            "extract_encode": AsyncMock(return_value={"ok": True, "sent_media_messages": [object()]}),
            "_get_first_takeoff_batch_id": AsyncMock(return_value=None),
            "_increment_takeoff_count": AsyncMock(return_value=1),
            "bot": SimpleNamespace(send_message=AsyncMock()), "bot_name": "testbot",
            "FormatUtils": SimpleNamespace(
                format_timestamp_utc8=str,
                minutes_to_day_hour=lambda minutes: (str(minutes), minutes // 10),
                format_duration=str,
            ),
            "_ceil_div": lambda value, divisor: (value + divisor - 1) // divisor,
            "_extract_takeoff_code": lambda message: "code",
            "re": re,
            "ENCODER_CONTENT_INPUT_STATE": {},
            "AIRPORT_EXTRA_CODE_MAINTENANCE": False,
            "_extract_takeoff_batch_id": lambda text: None,
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), self.ns)

    async def fly(self):
        return await self.ns["on_takeoff_fly"](123, "code", message=self.message)

    async def test_handler_answers_after_function_returns(self):
        for result in (
            {"ok": False, "answer": {"text": "failed", "show_alert": True}},
            {"ok": True, "answer": {"url": "https://t.me/testbot", "cache_time": 0}},
        ):
            callback = SimpleNamespace(
                message=self.message, from_user=SimpleNamespace(id=123), answer=AsyncMock()
            )

            async def fly(*args, **kwargs):
                callback.answer.assert_not_awaited()
                self.assertEqual(args, (123, "code"))
                self.assertEqual(kwargs, {"message": self.message})
                return result

            self.ns["on_takeoff_fly"] = fly
            await self.ns["on_takeoff"](callback)
            callback.answer.assert_awaited_once_with(**result["answer"])

    async def test_parse_error_returns_result(self):
        self.parsed.clear()
        result = await self.fly()
        self.assertFalse(result["ok"])
        self.assertIn("无法解析", result["answer"]["text"])
        self.cache.consume_minutes.assert_not_awaited()

    async def test_expired_returns_result_without_charge(self):
        self.parsed["valid_until"] = "20200101000000"
        result = await self.fly()
        self.assertFalse(result["ok"])
        self.assertIn("已过期", result["answer"]["text"])
        self.cache.consume_minutes.assert_not_awaited()

    async def test_missing_activity_returns_result_without_charge(self):
        self.cache.get.return_value = None
        result = await self.fly()
        self.assertFalse(result["ok"])
        self.assertTrue(result["answer"]["show_alert"])
        self.assertNotIn("parse_mode", result["answer"])
        self.cache.consume_minutes.assert_not_awaited()

    async def test_insufficient_balance_returns_result(self):
        self.expire.expire_timestamp = int(self.now.timestamp())
        result = await self.fly()
        self.assertFalse(result["ok"])
        self.assertIn("还差", result["answer"]["text"])
        self.cache.consume_minutes.assert_not_awaited()

    async def test_failed_charge_does_not_send_media(self):
        self.cache.consume_minutes.return_value = None
        self.assertFalse((await self.fly())["ok"])
        self.ns["extract_encode"].assert_not_awaited()

    async def test_delivery_exception_refunds_and_returns_result(self):
        self.ns["extract_encode"].side_effect = RuntimeError("offline")
        self.assertFalse((await self.fly())["ok"])
        self.cache.refund_minutes.assert_awaited_once_with(123, 10)

    async def test_delivery_rejection_refunds_and_returns_result(self):
        self.ns["extract_encode"].return_value = {"ok": False, "reason": "flash_used"}
        result = await self.fly()
        self.assertFalse(result["ok"])
        self.assertIn("仅可读取一次", result["answer"]["text"])
        self.cache.refund_minutes.assert_awaited_once_with(123, 10)

    async def test_success_uses_message_and_returns_redirect(self):
        result = await self.fly()
        self.assertEqual(result, {
            "ok": True,
            "answer": {"url": "https://t.me/testbot?start=fly_-100123_7", "cache_time": 0},
        })
        self.ns["extract_encode"].assert_awaited_once_with("code", self.message, 123)
        self.ns["_increment_takeoff_count"].assert_awaited_once_with(self.message)
        self.cache.refund_minutes.assert_not_awaited()

    async def test_charge_counts_acknowledged_media_and_survives_notice_failure(self):
        self.parsed["items"] = [{}, {}, {}]
        self.ns["extract_encode"].return_value = {"ok": True, "sent_media_messages": [object()], "skipped_count": 0}
        self.ns["bot"].send_message.side_effect = RuntimeError("notice failed")
        self.assertTrue((await self.fly())["ok"])
        self.cache.refund_minutes.assert_awaited_once_with(123, 20)

    async def test_zero_acknowledgements_refund_everything(self):
        self.ns["extract_encode"].return_value = {"ok": True, "sent_media_messages": []}
        self.assertFalse((await self.fly())["ok"])
        self.cache.refund_minutes.assert_awaited_once_with(123, 10)

    def private_message(self):
        return SimpleNamespace(
            text="⟦[example-takeoff-code]⟧",
            chat=SimpleNamespace(id=123), from_user=SimpleNamespace(id=123),
            message_id=7, reply=AsyncMock(),
        )

    async def test_private_text_reports_expired_result(self):
        self.parsed["valid_until"] = "20200101000000"
        message = self.private_message()
        await self.ns["on_text"](message)
        message.reply.assert_awaited_once_with(
            "❌ 此航班已过期 ( 超过有效时间 )", parse_mode=None
        )
        self.cache.consume_minutes.assert_not_awaited()

    async def test_private_text_success_does_not_duplicate_notification(self):
        message = self.private_message()
        await self.ns["on_text"](message)
        self.ns["extract_encode"].assert_awaited_once_with(
            "example-takeoff-code", message, 123
        )
        self.ns["bot"].send_message.assert_awaited_once()
        message.reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
