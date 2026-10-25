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
		notice_text = f"🔔 機器人名稱: @{bot_name}\n"

		for setting_name, chat_id in groups:
			if chat_id == 0:
				print(
					f"❌[群組設定錯誤] {setting_name} 尚未配置。",
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
						f"❌[群組設定錯誤] {setting_name}={chat_id} 找不到群組。"
						"請確認群組 ID 正確，並將機器人加入群組及設為管理員。\n"
					)
					continue

				notice_text += (
					f"❌[群組檢查失敗] 無法檢查 {setting_name}={chat_id}: {exc}\n"
				)
				continue
			except Exception as exc:
				notice_text += (
					f"❌[群組檢查失敗] 無法檢查 {setting_name}={chat_id}: {exc}\n"
				)
				continue

			if bot_status.status not in ("administrator", "creator"):
				notice_text += (
					f"❌[群組权限不足] 機器人在 {setting_name} = {chat_id} 中的身分為 "
					f"{bot_status.status}，請將機器人設為管理員。\n"
				)
				continue
			notice_text += (
				f"✅[群組檢查成功] 機器人已在 {setting_name} = {chat_id} 中並具有管理員權限。\n"
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
						f"⚠️ [群組檢查通知未送出] ControlPanel={control_panel_chat_id}: {exc}",
						flush=True,
					)
					if "chat not found" in str(exc).lower():
						print(
							"請確認 control_panel_chat_id 是正確"
							f" @{bot_name} 私訊，按下 Start 或傳送 /start。",
							flush=True,
						)
				except Exception as exc:
					print(
						f"⚠️ [群組檢查通知未送出] control_panel_chat_id={control_panel_chat_id}: {exc}",
						flush=True,
					)
			else:
				print(
					"⚠️ [群組檢查通知未送出] control_panel_chat_id 尚未配置。",
					flush=True,
				)
		return notice_text

	async def send(self, message_text: str) -> None:
		control_panel_chat_id = self.config.get("control_channel")
		board_id = self.config.get("board_id")
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
