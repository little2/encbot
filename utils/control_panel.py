from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest


class ControlPanel:
	def __init__(self, config: dict) -> None:
		self.config = config

	async def check_bot_group_admin_permissions(
		self,
		bot: Bot,
		bot_name: str,
		groups: Sequence[tuple[str, int]],
		telegram_call: Callable[
			[str, Callable[[], Awaitable[Any]]],
			Awaitable[Any],
		],
	) -> str:
		control_panel_chat_id = self.config.get("control_channel")
		notice_text = f"🔔 机器人名称: @{bot_name}\n"

		for setting_name, chat_id in groups:
			if chat_id == 0:
				print(
					f"❌[群组设置错误] {setting_name} 尚未配置。",
					flush=True,
				)
				continue

			try:
				bot_status = await bot.get_chat_member(
					chat_id=chat_id,
					user_id=bot.id,
				)
			except TelegramBadRequest as exc:
				if "chat not found" in str(exc).lower():
					notice_text += (
						f"❌[群组设置错误] {setting_name}={chat_id} 找不到群组。"
						"请确认群组 ID 正确，并将机器人加入群组及设为管理员。\n"
					)
					continue

				notice_text += (
					f"❌[群组检查失败] 无法检查 {setting_name}={chat_id}: {exc}\n"
				)
				continue
			except Exception as exc:
				notice_text += (
					f"❌[群组检查失败] 无法检查 {setting_name}={chat_id}: {exc}\n"
				)
				continue

			if bot_status.status not in ("administrator", "creator"):
				notice_text += (
					f"❌[群组权限不足] 机器人在 {setting_name} = {chat_id} 中的身份为 "
					f"{bot_status.status}，请将机器人设为管理员。\n"
				)
				continue
			notice_text += (
				f"✅[群组检查成功] 机器人已在 {setting_name} = {chat_id} 中并具有管理员权限。\n"
			)

		if notice_text:
			print(notice_text, flush=True)
			if int(control_panel_chat_id or 0) < 0:
				try:
					await telegram_call(
						"send startup permission report",
						lambda: self.send(notice_text),
					)
				except TelegramBadRequest as exc:
					print(
						f"⚠️ [群组检查通知未发送] ControlPanel={control_panel_chat_id}: {exc}",
						flush=True,
					)
					if "chat not found" in str(exc).lower():
						print(
							"请确认 control_panel_chat_id 正确，并向"
							f" @{bot_name} 发送私聊消息，按下 Start 或发送 /start。",
							flush=True,
						)
				except Exception as exc:
					print(
						f"⚠️ [群组检查通知未发送] control_panel_chat_id={control_panel_chat_id}: {exc}",
						flush=True,
					)
			else:
				print(
					"⚠️ [群组检查通知未发送] control_panel_chat_id 尚未配置。",
					flush=True,
				)
		return notice_text

	async def send(self, message_text: str) -> None:
		control_panel_chat_id = self.config.get("control_channel")

		raw_board_id = self.config.get("board_id")
		board_id = int(raw_board_id) if str(raw_board_id or "").strip() else None

		
		switch_bot_token = self.config.get("switch_bot_token")

		if not switch_bot_token:
			print("❌ No SWITCHBOT_TOKEN found, skipping sending message to control panel.", flush=True)
			return

		if not control_panel_chat_id:
			print("❌ No control panel chat ID found.", flush=True)
			return

		switchbot = Bot(
			token=switch_bot_token,
			default=DefaultBotProperties(parse_mode=ParseMode.HTML),
		)
		try:
			await switchbot.send_message(
				chat_id=control_panel_chat_id,
				message_thread_id=board_id,
				text=message_text,
			)
		except Exception as exc:
			print(f"❌ Failed to send message to control panel: {exc}", flush=True)
		finally:
			await switchbot.session.close()
