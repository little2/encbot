import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from utils.control_panel import ControlPanel


class ControlPanelTests(unittest.IsolatedAsyncioTestCase):
	async def test_send_to_control_panel_uses_config_and_closes_session(self):
		bot = SimpleNamespace(
			send_message=AsyncMock(),
			session=SimpleNamespace(close=AsyncMock()),
		)
		with patch("utils.control_panel.Bot", return_value=bot) as bot_factory:
			await ControlPanel(
				{
					"control_channel": -100123,
					"board_id": 42,
					"switch_bot_token": "test-token",
				},
			).send("notice")

		bot_factory.assert_called_once()
		bot.send_message.assert_awaited_once_with(
			chat_id=-100123,
			message_thread_id=42,
			text="notice",
		)
		bot.session.close.assert_awaited_once()

	async def test_missing_control_channel_does_not_create_bot(self):
		with patch("utils.control_panel.Bot") as bot_factory:
			await ControlPanel(
				{"switch_bot_token": "test-token"},
			).send("notice")

		bot_factory.assert_not_called()

	async def test_group_permission_check_reports_status_and_sends_notice(self):
		bot = SimpleNamespace(
			id=123,
			get_chat_member=AsyncMock(
				return_value=SimpleNamespace(status="administrator")
			),
		)
		async def call(_name, operation):
			return await operation()

		telegram_call = AsyncMock(side_effect=call)
		panel = ControlPanel({"control_channel": -100123})
		with patch.object(panel, "send", new=AsyncMock()) as send:
			result = await panel.check_bot_group_admin_permissions(
				bot=bot,
				bot_name="example_bot",
				groups=(("CHAT_PUBLIC_GROUP_ID", -100456),),
				telegram_call=telegram_call,
			)

		self.assertIn("example_bot", result)
		self.assertIn("群組檢查成功", result)
		bot.get_chat_member.assert_awaited_once_with(
			chat_id=-100456,
			user_id=123,
		)
		telegram_call.assert_awaited_once()
		send.assert_awaited_once_with(result)
