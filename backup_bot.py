from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import timedelta
from inspect import isawaitable
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
	BotCommand,
	InlineKeyboardButton,
	InlineKeyboardMarkup,
	Message,
)
from dotenv import load_dotenv

from utils.time_utils import app_now
from utils.telegram_gate import TelegramRateLimitMiddleware, telegram_call
from utils.video_store import VideoStore

backup_bot = None
PROJECT_ROOT = Path(__file__).resolve().parent
VIDEO_ID_PATTERN = r"[0-9a-fA-F]{16}"
BACKUP_FORWARD_SEND_LOCK = asyncio.Lock()
def _safe_print(message: str) -> None:
	encoding = str(getattr(sys.stdout, "encoding", "") or "utf-8")
	printable_message = str(message).encode(
		encoding,
		errors="replace",
	).decode(encoding)
	print(printable_message, flush=True)


def _build_dispatcher(
	bot_name: str,
	x_man_bot_id: int | None = None,
) -> Dispatcher:
	dispatcher = Dispatcher()


	@dispatcher.message(F.chat.type == "private")
	async def copy_message(message: Message) -> None:
		if x_man_bot_id is not None:
			# result = await message.copy_to(chat_id=x_man_bot_id)
			# print(f"🎒 Message copied to x_man_bot_id={x_man_bot_id}: {result}", flush=True)
			pass

	return dispatcher




async def start_backup_bot(
	on_ready: Callable[[str, Bot], Awaitable[None] | None] | None = None,
	x_man_bot_id: int | None = None,
) -> None:
  

	token = str(os.getenv("BACKUP_BOT_TOKEN", "") or "").strip()
	if not token:
		raise RuntimeError("缺少 BACKUP_BOT_TOKEN")

	backup_bot = Bot(
		token=token,
		default=DefaultBotProperties(parse_mode=ParseMode.HTML),
	)


	username = ""
	try:
		backup_bot.session.middleware(TelegramRateLimitMiddleware())
		me = await telegram_call(
			"backup bot get_me", backup_bot.get_me, retry_network_errors=True
		)
		username = str(getattr(me, "username", "") or "")
		if on_ready is not None:
			ready_result = on_ready(username,backup_bot)
			if isawaitable(ready_result):
				await ready_result

		bot_name = username
		dispatcher = _build_dispatcher(bot_name, x_man_bot_id)

		await telegram_call("backup bot set_my_commands", lambda: backup_bot.set_my_commands([
			BotCommand(command="check", description="查看資源分享情況"),
			BotCommand(command="board", description="班机时刻表"),
		]))
		_safe_print(f"✅ [BACKUP_BOT] 已启动：@{username}")
		await dispatcher.start_polling(backup_bot, handle_signals=False)
	except Exception as e:
		_safe_print(f"❌ [BACKUP_BOT] 启动失败：@{username} :{e}")
		raise
	finally:
		await backup_bot.session.close()


if __name__ == "__main__":
	asyncio.run(start_backup_bot())
