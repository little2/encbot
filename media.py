"""Print Bot API and Telethon media identities for the same forwarded media."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message
from dotenv import load_dotenv
from pyrogram.file_id import FileId
from telethon import types

from shared_config import SharedConfig
from telethon_userbot import TelethonUserbot


PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env", override=True)

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
userbot: TelethonUserbot | None = None


def _bot_token() -> str:
	"""Use the same token precedence as main_airport.py."""
	SharedConfig.load(True)
	token = os.getenv("BOT_TOKEN", SharedConfig.get("my_bot_token", ""))
	if not token:
		raise RuntimeError("BOT_TOKEN or SharedConfig.my_bot_token is required")
	return token


def _media_id_from_file_id(file_id: str) -> tuple[int | None, str | None]:
	"""Decode a Bot API file_id with Pyrogram and return its media ID."""
	try:
		decoded = FileId.decode(file_id)
		media_id = decoded.media_id
		if media_id is None:
			return None, f"unsupported decoded type: {decoded.file_type}"
		print(f"Decoded file_id: {decoded}", flush=True)
		return int(media_id), None
	except Exception as exc:
		return None, f"{type(exc).__name__}: {exc}"


def _bot_media_info(message: Message) -> dict[str, Any]:
	media = (
		message.document
		or (message.photo[-1] if message.photo else None)
		or message.video
		or message.audio
		or message.voice
		or message.animation
		or message.sticker
	)
	if media is None:
		raise ValueError("Unsupported media")
	media_id, media_id_error = _media_id_from_file_id(media.file_id)
	return {
		"kind": (
			"document" if message.document else "photo" if message.photo else
			"video" if message.video else "audio" if message.audio else
			"voice" if message.voice else "animation" if message.animation else "sticker"
		),
		"file_id": media.file_id,
		"file_unique_id": media.file_unique_id,
		"file_size": getattr(media, "file_size", None),
		"media_id": media_id,
		"media_id_error": media_id_error,
	}


async def _send_to_userbot(bot: Bot, info: dict[str, Any], user_id: int) -> None:
	kind = info["kind"]
	file_id = info["file_id"]
	if kind == "document":
		await bot.send_document(user_id, file_id)
	elif kind == "photo":
		await bot.send_photo(user_id, file_id)
	elif kind == "video":
		await bot.send_video(user_id, file_id)
	elif kind == "audio":
		await bot.send_audio(user_id, file_id)
	elif kind == "voice":
		await bot.send_voice(user_id, file_id)
	elif kind == "animation":
		await bot.send_animation(user_id, file_id)
	elif kind == "sticker":
		await bot.send_sticker(user_id, file_id)
	else:
		raise ValueError(f"Unsupported media kind: {kind}")


async def on_userbot_message(event) -> None:
	"""Print MTProto fields after the user account receives the media."""
	media = event.message.media
	if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
		print(
			"[USERBOT_MEDIA] "
			f"kind=photo media_id={media.photo.id} access_hash={media.photo.access_hash} "
			f"chat_id={event.chat_id} message_id={event.id}",
			flush=True,
		)
	elif isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
		print(
			"[USERBOT_MEDIA] "
			f"kind=document media_id={media.document.id} "
			f"access_hash={media.document.access_hash} chat_id={event.chat_id} "
			f"message_id={event.id}",
			flush=True,
		)
	else:
		print(
			f"[USERBOT_MEDIA] unsupported media={type(media).__name__} "
			f"chat_id={event.chat_id} message_id={event.id}",
			flush=True,
		)


@dp.message(
	F.document | F.photo | F.video | F.audio | F.voice | F.animation | F.sticker,
)
async def on_bot_media(message: Message, bot: Bot) -> None:
	if userbot is None or userbot.user_id is None:
		await message.reply("Userbot is not connected yet; try again shortly.")
		return

	try:
		info = _bot_media_info(message)
	except ValueError as exc:
		await message.reply(str(exc))
		return
    
	print(
		"[BOT_MEDIA] "
		f"kind={info['kind']} file_id={info['file_id']} "
		f"file_unique_id={info['file_unique_id']} media_id={info['media_id']} "
		f"media_id_error={info['media_id_error']} file_size={info['file_size']} "
		f"chat_id={message.chat.id} message_id={message.message_id}",
		flush=True,
	)
	try:
		await _send_to_userbot(bot, info, int(userbot.user_id))
		print(f"[BOT_MEDIA] forwarded to userbot_id={userbot.user_id}", flush=True)
	except Exception as exc:
		print(f"[BOT_MEDIA] forward to userbot failed: {exc}", flush=True)
		await message.reply("Forwarding to the userbot failed; see the console log.")


async def main() -> None:
	global userbot
	bot = Bot(
		token=_bot_token(),
		default=DefaultBotProperties(link_preview_is_disabled=True),
	)
	userbot = TelethonUserbot.from_env(on_userbot_message)
	userbot_task = asyncio.create_task(userbot.run(), name="media-test-userbot")
	try:
		await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
	finally:
		userbot_task.cancel()
		await asyncio.gather(userbot_task, return_exceptions=True)
		await bot.session.close()


if __name__ == "__main__":
	asyncio.run(main())
