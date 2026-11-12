import ast
import os
import unittest
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from tests.test_airport_database import SOURCE, SOURCE_TREE, load_functions


class DutyFreeConfigurationTests(unittest.TestCase):
    def test_environment_switch_default_and_enabled_values(self):
        assignment = next(
            node for node in SOURCE_TREE.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "AIRPORT_DUTY_FREE_MAINTENANCE"
                for target in node.targets
            )
        )
        for value, expected in ((None, False), ("true", True), (" ON ", True), ("false", False)):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {} if value is None else {"AIRPORT_DUTY_FREE_MAINTENANCE": value},
                clear=True,
            ):
                namespace = {"os": os}
                exec(compile(ast.Module(body=[assignment], type_ignores=[]), str(SOURCE), "exec"), namespace)
                self.assertEqual(namespace["AIRPORT_DUTY_FREE_MAINTENANCE"], expected)


class DutyFreeMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(
            decline_chat_join_request=AsyncMock(),
            send_message=AsyncMock(),
        )
        self.ns = {
            "bot": self.bot,
            "AIRPORT_DUTY_FREE_MAINTENANCE": True,
            "AIRPORT_DUTY_FREE_GROUP_ID": -100,
            "AIRPORT_LOBBY_GROUP_ID": -200,
            "TERMINAL_CHANNEL_ID": -300,
            "_build_join_context": Mock(return_value=SimpleNamespace(is_paid_invite=False)),
            "_process_join_request": AsyncMock(),
            "_get_or_create_airport_invite_link": AsyncMock(return_value="https://t.me/airport"),
            "_get_or_create_chat_invite_link": AsyncMock(return_value="https://t.me/dutyfree"),
            "bot_name": "tower_bot",
            "TRADE_IMAGE_PATHS": (SimpleNamespace(is_file=Mock(return_value=False)),),
            "InlineKeyboardButton": InlineKeyboardButton,
            "InlineKeyboardMarkup": InlineKeyboardMarkup,
            "escape": escape,
        }
        load_functions({"on_airport_join_request", "_send_lobby_welcome"}, self.ns)

    def request(self, chat_id):
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=SimpleNamespace(id=123))

    async def test_maintenance_declines_duty_free_before_checking_eligibility(self):
        await self.ns["on_airport_join_request"](self.request(-100))
        self.bot.decline_chat_join_request.assert_awaited_once_with(chat_id=-100, user_id=123)
        self.ns["_build_join_context"].assert_not_called()
        self.ns["_process_join_request"].assert_not_awaited()

    async def test_decline_failure_is_logged(self):
        self.bot.decline_chat_join_request.side_effect = RuntimeError("no permission")
        with patch("builtins.print") as output:
            await self.ns["on_airport_join_request"](self.request(-100))
        self.assertIn("no permission", output.call_args.args[0])
        self.ns["_process_join_request"].assert_not_awaited()

    async def test_other_groups_continue_normally_during_maintenance(self):
        for chat_id in (-200, -300):
            with self.subTest(chat_id=chat_id):
                await self.ns["on_airport_join_request"](self.request(chat_id))
        self.bot.decline_chat_join_request.assert_not_awaited()
        self.assertEqual(self.ns["_process_join_request"].await_count, 2)

    async def test_disabled_switch_preserves_normal_duty_free_review(self):
        self.ns["AIRPORT_DUTY_FREE_MAINTENANCE"] = False
        await self.ns["on_airport_join_request"](self.request(-100))
        self.bot.decline_chat_join_request.assert_not_awaited()
        self.ns["_process_join_request"].assert_awaited_once()

    async def test_welcome_button_and_link_follow_switch(self):
        user = SimpleNamespace(id=123, full_name="旅客")
        for enabled in (True, False):
            with self.subTest(maintenance=enabled):
                self.ns["AIRPORT_DUTY_FREE_MAINTENANCE"] = enabled
                self.bot.send_message.reset_mock()
                self.ns["_get_or_create_chat_invite_link"].reset_mock()
                await self.ns["_send_lobby_welcome"](user)
                kwargs = self.bot.send_message.await_args.kwargs
                labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
                self.assertEqual("🛍️ 免税店" in labels, not enabled)
                self.assertEqual("(整修中)" in kwargs["text"], enabled)
                self.assertEqual(self.ns["_get_or_create_chat_invite_link"].await_count, int(not enabled))
                self.assertIn("🛫 飞机场", labels)
                self.assertIn("🗼 塔台", labels)
                self.assertIn("🪧 指路牌", labels)

    async def test_photo_welcome_hides_duty_free_button(self):
        self.ns["TRADE_IMAGE_PATHS"] = (SimpleNamespace(is_file=Mock(return_value=True)),)
        self.ns["FSInputFile"] = Mock(spec=FSInputFile)
        self.bot.send_photo = AsyncMock()
        await self.ns["_send_lobby_welcome"](SimpleNamespace(id=123, full_name="旅客"))
        kwargs = self.bot.send_photo.await_args.kwargs
        labels = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertNotIn("🛍️ 免税店", labels)
        self.ns["_get_or_create_chat_invite_link"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
