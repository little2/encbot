"""
使用 aiogram 的 Bot API 实现一个 Telegram Bot：

1) 接收用户发送的文件，获取 file_unique_id，
	先通过 build_file_token 生成 token，再用 telegram_to_unicode_cjk 转成 CJK 字符串。

2) 接收用户粘贴的 CJK 字符串，
	先用 unicode_cjk_to_telegram 还原 token，再用 parse_file_token 解析字段。
"""

from __future__ import annotations
import re
import asyncio
import os
from collections import OrderedDict
from io import BytesIO
from datetime import datetime, timedelta
from typing import Any

from PIL import Image, ImageDraw, ImageOps
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BufferedInputFile, CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

from utils.utf_utils import UtfConverter
from utils.user_utils import UserExpireCache, UserExpire
from dotenv import load_dotenv
load_dotenv(dotenv_path='.env')
BOT_TOKEN = os.getenv("BOT_TOKEN")
MEDIA_FORWARD_USER_ID = int(os.getenv("MEDIA_FORWARD_USER_ID", "0") or 0)
ENCODED_FORWARD_CHAT_ID = int(os.getenv("ENCODED_FORWARD_CHAT_ID", "0") or 0)
ENCODED_FORWARD_THREAD_ID = int(os.getenv("ENCODED_FORWARD_THREAD_ID", "0") or 0)

user_expire_cache = UserExpireCache()

def _parse_whitelist_ids(raw: str) -> set[int]:
	ids: set[int] = set()
	for item in str(raw or "").split(","):
		text = item.strip()
		if not text:
			continue
		if text.lstrip("-").isdigit():
			ids.add(int(text))
	return ids


ENCODED_FORWARD_WHITELIST_USER_IDS = _parse_whitelist_ids(os.getenv("ENCODED_FORWARD_WHITELIST", ""))

if not BOT_TOKEN:
	raise RuntimeError("Missing bot token. Please set ENCBOT_TOKEN or BOT_TOKEN.")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ENCODER_UI_STATE: dict[tuple[int, int], dict[str, Any]] = {}
UPLOAD_SESSIONS: dict[tuple[int, int], dict[str, Any]] = {}
USER_MEDIA_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
USER_MEDIA_PENDING: dict[tuple[int, int], int] = {}
OVERFLOW_NOTICE_TIME: dict[tuple[int, int], float] = {}
MEDIA_QUEUE: asyncio.Queue[Message] = asyncio.Queue(maxsize=100)
MEDIA_WORKER_COUNT = 3
MAX_BATCH_MEDIA = 10
MAX_USER_PENDING = 15
PREVIEW_DOWNLOAD_LIMIT = asyncio.Semaphore(4)
PREVIEW_CACHE_LIMIT = 500
PREVIEW_STYLE_ORIGINAL = "original-v1"
PREVIEW_STYLE_VIDEO = "video-play-v1"
PLAY_ICON_SIZES = (48, 64, 80, 96, 128)
PREVIEW_CACHE: OrderedDict[tuple[str, str], bytes] = OrderedDict()
bot_name = ""
USED_FLASH_NONCES: dict[str, datetime] = {}
PERM_FLASH_NONCE_RETENTION_DAYS = 30


def _create_play_icon(size: int) -> Image.Image:
	icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
	draw = ImageDraw.Draw(icon)
	border_width = max(1, size // 32)
	draw.ellipse(
		(border_width, border_width, size - border_width - 1, size - border_width - 1),
		fill=(0, 0, 0, 120),
		outline=(255, 255, 255, 110),
		width=border_width,
	)
	center_x = size // 2 + size // 24
	center_y = size // 2
	half_height = size * 23 // 100
	half_width = size * 17 // 100
	draw.polygon(
		[
			(center_x - half_width, center_y - half_height),
			(center_x - half_width, center_y + half_height),
			(center_x + half_width, center_y),
		],
		fill=(255, 255, 255, 235),
	)
	return icon


PLAY_ICON_CACHE = {size: _create_play_icon(size) for size in PLAY_ICON_SIZES}


def _make_fallback_preview(video: bool = False) -> bytes:
	image = Image.new("RGB", (320, 180), (245, 245, 245))
	if video:
		icon = PLAY_ICON_CACHE[64]
		position = ((image.width - icon.width) // 2, (image.height - icon.height) // 2)
		image.paste(icon, position, icon)
	output = BytesIO()
	image.save(output, format="JPEG", quality=80, subsampling=2, optimize=False, progressive=False)
	return output.getvalue()


FALLBACK_PREVIEW_BYTES = _make_fallback_preview()
VIDEO_FALLBACK_PREVIEW_BYTES = _make_fallback_preview(video=True)


def _cleanup_used_flash_nonces(now: datetime) -> None:
	expired_keys = [key for key, expires_at in USED_FLASH_NONCES.items() if now >= expires_at]
	for key in expired_keys:
		USED_FLASH_NONCES.pop(key, None)


def _extract_media_info(message: Message) -> tuple[str, str]:
	"""
	从消息中提取 (file_type, file_id)。
	若不是支持的媒体类型，抛出 ValueError。
	"""
	if message.document:
		return "document", message.document.file_id
	if message.photo:
		# photo 为多个尺寸，取最大尺寸通常在最后一个
		return "photo", message.photo[-1].file_id
	if message.video:
		return "video", message.video.file_id
	if message.audio:
		return "audio", message.audio.file_id
	if message.voice:
		return "voice", message.voice.file_id
	if message.animation:
		return "animation", message.animation.file_id
	if message.sticker:
		return "sticker", message.sticker.file_id

	raise ValueError("Unsupported media type")


def _extract_preview_info(message: Message, file_type: str, file_id: str) -> dict[str, str]:
	"""提取转发预览所需的缩略图标识，不把它写入取件码。"""
	preview = None

	if file_type == "photo" and message.photo:
		candidates = [
			photo for photo in message.photo
			if max(int(photo.width or 0), int(photo.height or 0)) > 100
		]
		preview = min(
			candidates or list(message.photo),
			key=lambda photo: max(int(photo.width or 0), int(photo.height or 0)),
		)
	elif file_type == "video" and message.video:
		cover = getattr(message.video, "cover", None)
		if isinstance(cover, list) and cover:
			preview = cover[0]
		elif cover:
			preview = cover
		if not preview:
			preview = message.video.thumbnail
	elif file_type == "document" and message.document:
		preview = message.document.thumbnail
	elif file_type == "animation" and message.animation:
		preview = message.animation.thumbnail
	elif file_type == "audio" and message.audio:
		preview = message.audio.thumbnail
	elif file_type == "sticker" and message.sticker:
		preview = message.sticker.thumbnail

	return {
		"preview_file_id": str(getattr(preview, "file_id", "") or (file_id if file_type == "photo" else "")),
		"preview_unique_id": str(getattr(preview, "file_unique_id", "") or file_id),
	}


def _build_display(data: dict[str, Any], token: str, encoded: str) -> str:
	valid_until = str(data.get("valid_until", ""))
	if valid_until == "99991231235959":
		valid_until_display = "永久有效"
	elif len(valid_until) == 14 and valid_until.isdigit():
		valid_until_display = (
			f"{valid_until[0:4]}-{valid_until[4:6]}-{valid_until[6:8]} "
			f"{valid_until[8:10]}:{valid_until[10:12]}:{valid_until[12:14]}"
		)
	else:
		valid_until_display = valid_until


	bot_name_lack = bot_name[:-1] if bot_name else ""
	start_char = "⟦["
	end_char = "]⟧"

	return_text = ""
	media_count = len(data.get("items", []))
	if media_count > 1:
		return_text += f"📦 媒体数量: {media_count}\n"

	if(data['no_forward']==True):
		return_text += f"🚫 禁止转发: 是\n"
	
	if(data['flash_seconds']>0):
		return_text += f"⚡ 闪照时间: {data['flash_seconds']} 秒\n"

	if(data['valid_until']!="99991231235959"):
		return_text += f"⏳ 有效时间: {valid_until_display}\n\n"

	

	return_text += (	
		f"\n将取件码👇传给 🤖 <a href=\"https://b.oy/{encoded}\">🤖</a><code>{bot_name_lack}</code><code> t</code> (去空格) \n\n{start_char}<code>{encoded}</code>{end_char}"
	)
	if len(encoded) > 256:
		return_text += "\n\nℹ️ 批量取件码较长，请长按上方密文复制。"

	return return_text

def _build_keyboard(data: dict[str, Any], token: str, encoded: str) -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(
		inline_keyboard=[[
			InlineKeyboardButton(
				text="🛫 请求起飞",
				callback_data=f"takeoff:fly",
			)
		]]
	)


def _resolve_valid_until(mode: str) -> str:
	if mode == "perm":
		return "99991231235959"
	if mode == "10m":
		return (datetime.now() + timedelta(minutes=10)).strftime("%Y%m%d%H%M%S")
	if mode == "30m":
		return (datetime.now() + timedelta(minutes=30)).strftime("%Y%m%d%H%M%S")
	if mode == "1h":
		return (datetime.now() + timedelta(hours=1)).strftime("%Y%m%d%H%M%S")
	raise ValueError(f"Unsupported valid mode: {mode}")


def _choice(label: str, selected: bool) -> str:
	return f"✅ {label}" if selected else f"{label}"


def _build_controls_keyboard(state: dict[str, Any], encoded: str) -> InlineKeyboardMarkup:
	no_forward = bool(state.get("no_forward", False))
	flash_seconds = int(state.get("flash_seconds", 0))
	valid_mode = str(state.get("valid_mode", "perm"))
	long_flash_seconds = int(state.get("video_flash_seconds", 60))
	long_flash_label = f"{long_flash_seconds}秒" if bool(state.get("has_video", False)) else "60秒"

	rows = [
			[
				InlineKeyboardButton(
					text="🚫 目前限制转发" if no_forward else "🆗 目前可以转发",
					callback_data=f"enc:fw:{0 if no_forward else 1}",
				),
			],
			[
				InlineKeyboardButton(
					text=_choice("不闪", flash_seconds == 0),
					callback_data="enc:fl:0",
				),
				InlineKeyboardButton(
					text=_choice("20秒", flash_seconds == 20),
					callback_data="enc:fl:20",
				),
				InlineKeyboardButton(
					text=_choice(long_flash_label, flash_seconds == long_flash_seconds),
					callback_data=f"enc:fl:{long_flash_seconds}",
				),
			],
			[
				InlineKeyboardButton(
					text=_choice("永久", valid_mode == "perm"),
					callback_data="enc:vu:perm",
				),
				InlineKeyboardButton(
					text=_choice("10分钟", valid_mode == "10m"),
					callback_data="enc:vu:10m",
				),
				InlineKeyboardButton(
					text=_choice("60分钟", valid_mode == "30m"),
					callback_data="enc:vu:30m",
				)
			],
		]
	if len(encoded) <= 256:
		rows.append([
			InlineKeyboardButton(
				text="📋 复制密文",
				copy_text=CopyTextButton(text=encoded),
			)
		])
	return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_token_and_encoded(state: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
	valid_until = _resolve_valid_until(str(state.get("valid_mode", "perm")))
	items = list(state.get("items", []))
	if not items:
		items = [{"file_id": str(state["file_id"]), "file_type": str(state["file_type"])}]
	token = UtfConverter.build_media_token(
		user_id=int(state["user_id"]),
		items=items,
		no_forward=bool(state.get("no_forward", False)),
		flash_seconds=int(state.get("flash_seconds", 0)),
		valid_until=valid_until,
	)
	encoded = UtfConverter.telegram_to_unicode_cjk(token)
	parsed = UtfConverter.parse_file_token(token)
	return token, encoded, parsed


def _format_duration(seconds: int) -> str:
	seconds = max(0, int(seconds))
	days, rem = divmod(seconds, 86400)
	hours, rem = divmod(rem, 3600)
	minutes, secs = divmod(rem, 60)

	parts: list[str] = []
	if days:
		parts.append(f"{days}天")
	if hours:
		parts.append(f"{hours}小时")
	if minutes:
		parts.append(f"{minutes}分钟")
	if secs or not parts:
		parts.append(f"{secs}秒")

	return "".join(parts)


async def _send_media_by_type(message: Message, data: dict[str, Any], receiver_id: int = None) -> Message:
	file_type = str(data["file_type"])
	file_id = str(data["file_id"])
	no_forward = bool(data.get("no_forward", False))
	chat_id = receiver_id or message.from_user.id

	if file_type == "document":
		return await bot.send_document(chat_id=chat_id, document=file_id, protect_content=no_forward)
	if file_type == "photo":
		return await bot.send_photo(chat_id=chat_id, photo=file_id, protect_content=no_forward)
	if file_type == "video":
		return await bot.send_video(chat_id=chat_id, video=file_id, protect_content=no_forward)
	if file_type == "audio":
		return await bot.send_audio(chat_id=chat_id, audio=file_id, protect_content=no_forward)
	if file_type == "voice":
		return await bot.send_voice(chat_id=chat_id, voice=file_id, protect_content=no_forward)
	if file_type == "animation":
		return await bot.send_animation(chat_id=chat_id, animation=file_id, protect_content=no_forward)
	if file_type == "sticker":
		return await bot.send_sticker(chat_id=chat_id, sticker=file_id, protect_content=no_forward)

	raise ValueError(f"Unsupported file_type: {file_type}")


def _album_kind(file_type: str) -> str | None:
    if file_type in {"photo", "video"}:
        return "visual"
    if file_type == "document":
        return "document"
    if file_type == "audio":
        return "audio"
    return None

def _build_input_media(item: dict[str, Any]):
    file_id = str(item["file_id"])
    file_type = str(item["file_type"])

    if file_type == "photo":
        return InputMediaPhoto(media=file_id)
    if file_type == "video":
        return InputMediaVideo(media=file_id)
    if file_type == "document":
        return InputMediaDocument(media=file_id)
    if file_type == "audio":
        return InputMediaAudio(media=file_id)

    raise ValueError(f"Unsupported album type: {file_type}")

async def _send_all_media(
    message: Message,
    data: dict[str, Any],
    receiver_id: int = None,
) -> list[Message]:
    items = data.get("items") or [{
        "file_id": data["file_id"],
        "file_type": data["file_type"],
    }]

    no_forward = bool(data.get("no_forward", False))
    sent_messages: list[Message] = []
    pending_group: list[dict[str, Any]] = []
    pending_kind: str | None = None

    async def flush_group() -> None:
        nonlocal pending_group, pending_kind


        if not pending_group:
            return

        if len(pending_group) >= 2:
            media = [
                _build_input_media(item)
                for item in pending_group
            ]

            result = await bot.send_media_group(
                chat_id=receiver_id or message.from_user.id,
                media=media,
                protect_content=no_forward,
            )
            sent_messages.extend(result)
        else:
            item_data = dict(data)
            item_data.update(pending_group[0])

            sent = await _send_media_by_type(
                message,
                item_data,
                receiver_id=receiver_id,
            )
            sent_messages.append(sent)

        pending_group = []
        pending_kind = None

    for item in items:
        kind = _album_kind(str(item["file_type"]))

        # 不支持相簿的类型
        if kind is None:
            await flush_group()

            item_data = dict(data)
            item_data.update(item)

            sent = await _send_media_by_type(
                message,
                item_data,
            )
            sent_messages.append(sent)
            continue

        # 类型不兼容或者已经达到 10 个
        if pending_group and (
            kind != pending_kind
            or len(pending_group) >= 10
        ):
            await flush_group()

        pending_kind = kind
        pending_group.append(item)

    await flush_group()
    return sent_messages

async def _send_all_media_old(message: Message, data: dict[str, Any]) -> list[Message]:
	sent_messages: list[Message] = []
	for item in data.get("items", [{"file_id": data["file_id"], "file_type": data["file_type"]}]):
		item_data = dict(data)
		item_data.update(item)
		sent_messages.append(await _send_media_by_type(message, item_data))
	return sent_messages


async def _delete_message_later(sent_message: Message, delay_seconds: int) -> None:
	await asyncio.sleep(delay_seconds)
	try:
		await sent_message.delete()
	except Exception:
		# 可能因权限/消息状态无法删除，忽略即可
		pass


async def _forward_media_in_background(message: Message) -> None:
	if MEDIA_FORWARD_USER_ID <= 0:
		print("[MEDIA_FORWARD] MEDIA_FORWARD_USER_ID not set, skip forwarding", flush=True)
		return

	try:
		result = await bot.copy_message(
			chat_id=MEDIA_FORWARD_USER_ID,
			from_chat_id=message.chat.id,
			message_id=message.message_id,
		)
		print(f"[MEDIA_FORWARD] forward result: {result}", flush=True)
	except Exception as exc:
		print(f"[MEDIA_FORWARD] forward failed: {exc}", flush=True)


def _preview_cache_get(key: tuple[str, str]) -> bytes | None:
	content = PREVIEW_CACHE.get(key)
	if content is not None:
		PREVIEW_CACHE.move_to_end(key)
	return content


def _preview_cache_set(key: tuple[str, str], content: bytes) -> None:
	PREVIEW_CACHE[key] = content
	PREVIEW_CACHE.move_to_end(key)
	while len(PREVIEW_CACHE) > PREVIEW_CACHE_LIMIT:
		PREVIEW_CACHE.popitem(last=False)


def _get_play_icon(short_edge: int) -> Image.Image:
	wanted_size = max(48, min(128, short_edge // 4))
	size = min(PLAY_ICON_SIZES, key=lambda value: abs(value - wanted_size))
	return PLAY_ICON_CACHE[size]


def _overlay_video_play_icon(image_bytes: bytes) -> bytes:
	with Image.open(BytesIO(image_bytes)) as source:
		image = ImageOps.exif_transpose(source).convert("RGBA")
	if max(image.size) > 480:
		image.thumbnail((480, 480), Image.Resampling.BILINEAR)

	icon = _get_play_icon(min(image.size))
	position = ((image.width - icon.width) // 2, (image.height - icon.height) // 2)
	image.paste(icon, position, icon)

	output = BytesIO()
	image.convert("RGB").save(
		output,
		format="JPEG",
		quality=80,
		subsampling=2,
		optimize=False,
		progressive=False,
	)
	return output.getvalue()


def _process_preview_batch(
	jobs: list[tuple[tuple[str, str], bytes | None, bool]],
) -> dict[tuple[str, str], tuple[bytes, bool]]:
	processed: dict[tuple[str, str], tuple[bytes, bool]] = {}
	for cache_key, content, is_video in jobs:
		if content is None:
			processed[cache_key] = (
				VIDEO_FALLBACK_PREVIEW_BYTES if is_video else FALLBACK_PREVIEW_BYTES,
				False,
			)
			continue

		if not is_video:
			processed[cache_key] = (content, True)
			continue

		try:
			processed[cache_key] = (_overlay_video_play_icon(content), True)
		except Exception as exc:
			print(f"[ENCODED_FORWARD] video preview processing failed: {exc}", flush=True)
			processed[cache_key] = (VIDEO_FALLBACK_PREVIEW_BYTES, False)
	return processed


async def _download_preview(cache_key: tuple[str, str], file_id: str) -> tuple[tuple[str, str], bytes | None]:
	try:
		async with PREVIEW_DOWNLOAD_LIMIT:
			buffer = BytesIO()
			await bot.download(file_id, destination=buffer)
			return cache_key, buffer.getvalue()
	except Exception as exc:
		print(f"[ENCODED_FORWARD] preview download failed: {exc}", flush=True)
		return cache_key, None


async def _forward_encoded_if_whitelisted(
	message: Message,
	encoded: str,
	items: list[dict[str, Any]],
) -> None:
	if ENCODED_FORWARD_CHAT_ID == 0:
		return

	from_user_id = int(message.from_user.id) if message.from_user else 0
	if from_user_id <= 0 or from_user_id not in ENCODED_FORWARD_WHITELIST_USER_IDS:
		# print(f"[ENCODED_FORWARD] user {from_user_id} not in whitelist, skip forwarding", flush=True)
		return

	display_keyboard = FileNotFoundError
	display_text = encoded
	try:
		token = UtfConverter.unicode_cjk_to_telegram(encoded)
		parsed = UtfConverter.parse_file_token(token)
		parsed_items = list(parsed.get("items", []))
		if not parsed_items:
			raise ValueError("encoded 中没有媒体")
		display_text = _build_display(parsed, token, encoded)
		display_keyboard = _build_keyboard(parsed, token, encoded)

		preview_entries: list[tuple[tuple[str, str], str]] = []
		download_requests: dict[tuple[str, str], str] = {}
		job_video_types: dict[tuple[str, str], bool] = {}
		for index, parsed_item in enumerate(parsed_items):
			preview_file_id = ""
			preview_unique_id = str(parsed_item.get("file_id", index))
			file_type = str(parsed_item.get("file_type", ""))
			if index < len(items):
				source_item = items[index]
				if str(source_item.get("file_id", "")) == str(parsed_item.get("file_id", "")):
					preview_file_id = str(source_item.get("preview_file_id", ""))
					preview_unique_id = str(source_item.get("preview_unique_id", "") or preview_unique_id)

			style = PREVIEW_STYLE_VIDEO if file_type == "video" else PREVIEW_STYLE_ORIGINAL
			cache_key = (preview_unique_id, style)
			preview_entries.append((cache_key, f"preview_{index + 1}.jpg"))
			job_video_types.setdefault(cache_key, file_type == "video")
			if _preview_cache_get(cache_key) is None and preview_file_id:
				download_requests.setdefault(cache_key, preview_file_id)

		downloaded = dict(await asyncio.gather(*[
			_download_preview(cache_key, file_id)
			for cache_key, file_id in download_requests.items()
		])) if download_requests else {}

		jobs = [
			(cache_key, downloaded.get(cache_key), is_video)
			for cache_key, is_video in job_video_types.items()
			if _preview_cache_get(cache_key) is None
		]

		processed = await asyncio.to_thread(_process_preview_batch, jobs) if jobs else {}
		for cache_key, (content, cacheable) in processed.items():
			if cacheable:
				_preview_cache_set(cache_key, content)

		preview_payloads: list[tuple[bytes, str]] = []
		for cache_key, filename in preview_entries:
			content = _preview_cache_get(cache_key)
			if content is None:
				content = processed[cache_key][0]
			preview_payloads.append((content, filename))

		thread_id = ENCODED_FORWARD_THREAD_ID if ENCODED_FORWARD_THREAD_ID > 0 else None
		caption = display_text if len(display_text) <= 1024 else None
		media = [
			InputMediaPhoto(
				media=BufferedInputFile(content, filename=filename),
				caption=caption if index == 0 else None,
				parse_mode="HTML" if index == 0 and caption else None,
			)
			for index, (content, filename) in enumerate(preview_payloads)
		]

		if len(media) == 1:
			content, filename = preview_payloads[0]
			await bot.send_photo(
				chat_id=ENCODED_FORWARD_CHAT_ID,
				message_thread_id=thread_id,
				photo=BufferedInputFile(content, filename=filename),
				caption=caption,
				reply_markup=display_keyboard,
				parse_mode="HTML" if caption else None,
			)
		else:
			await bot.send_media_group(
				chat_id=ENCODED_FORWARD_CHAT_ID,
				message_thread_id=thread_id,
				media=media
			)

			await bot.send_message(
				chat_id=ENCODED_FORWARD_CHAT_ID,
				message_thread_id=thread_id,
				text=display_text,
				reply_markup=display_keyboard,
				parse_mode="HTML",
			)

		if caption is None:
			await bot.send_message(
				chat_id=ENCODED_FORWARD_CHAT_ID,
				message_thread_id=thread_id,
				text=display_text,
				reply_markup=display_keyboard,
				parse_mode="HTML",
			)
	except Exception as exc:
		print(f"[ENCODED_FORWARD] send failed: {exc}", flush=True)
		try:
			await bot.send_message(
				chat_id=ENCODED_FORWARD_CHAT_ID,
				message_thread_id=ENCODED_FORWARD_THREAD_ID if ENCODED_FORWARD_THREAD_ID > 0 else None,
				text=display_text,
				parse_mode="HTML",
			)
		except Exception as fallback_exc:
			print(f"[ENCODED_FORWARD] text fallback failed: {fallback_exc}", flush=True)


def _upload_keyboard() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(
		inline_keyboard=[[
			InlineKeyboardButton(text="✅ 上传完成", callback_data="enc:upload:done"),
		]]
	)


async def _notify_media_limit(message: Message, text: str) -> None:
	if not message.from_user:
		return
	key = (message.chat.id, message.from_user.id)
	now = asyncio.get_running_loop().time()
	if now - OVERFLOW_NOTICE_TIME.get(key, 0) < 5:
		return
	OVERFLOW_NOTICE_TIME[key] = now
	await message.reply(f"⚠️ {text}")


async def _update_upload_panel(message: Message, session: dict[str, Any]) -> None:
	count = len(session["items"])
	text = (
		f"📥 已收到 {count} / {MAX_BATCH_MEDIA} 个媒体\n\n"
		"继续发送媒体，或点击“上传完成”进入编辑菜单。"
	)
	panel_message_id = session.get("panel_message_id")
	if panel_message_id:
		await bot.edit_message_text(
			chat_id=message.chat.id,
			message_id=int(panel_message_id),
			text=text,
			reply_markup=_upload_keyboard(),
		)
		return

	panel = await message.reply(text, reply_markup=_upload_keyboard())
	session["panel_message_id"] = panel.message_id


async def _finish_upload(
	key: tuple[int, int],
	message: Message,
	session: dict[str, Any],
) -> None:
	items = list(session.get("items", []))
	if not items:
		raise ValueError("尚未收到媒体")

	video_durations = [
		int(item.get("duration", 0) or 0)
		for item in items
		if item.get("file_type") == "video"
	]
	state = {
		"owner_user_id": key[1],
		"user_id": key[1],
		"file_id": items[0]["file_id"],
		"file_type": items[0]["file_type"],
		"items": items,
		"no_forward": False,
		"flash_seconds": 0,
		"has_video": bool(video_durations),
		"video_flash_seconds": (max(video_durations) + 15) if video_durations else 60,
		"valid_mode": "perm",
	}
	token, encoded, parsed = _build_token_and_encoded(state)
	display_text = _build_display(parsed, token, encoded)
	markup = _build_controls_keyboard(state, encoded)
	panel_message_id = session.get("panel_message_id")

	if panel_message_id:
		await bot.edit_message_text(
			chat_id=key[0],
			message_id=int(panel_message_id),
			text=display_text,
			reply_markup=markup,
			parse_mode="HTML",
		)
	else:
		panel = await message.reply(display_text, reply_markup=markup, parse_mode="HTML")
		panel_message_id = panel.message_id

	ENCODER_UI_STATE[(key[0], int(panel_message_id))] = state
	if UPLOAD_SESSIONS.get(key) is session:
		UPLOAD_SESSIONS.pop(key, None)
	source_message = session.get("source_message", message)
	asyncio.create_task(_forward_encoded_if_whitelisted(source_message, encoded, items))


async def _process_queued_media(message: Message) -> None:
	if not message.from_user:
		return
	key = (message.chat.id, message.from_user.id)
	lock = USER_MEDIA_LOCKS.setdefault(key, asyncio.Lock())

	async with lock:
		session = UPLOAD_SESSIONS.get(key)
		if not session:
			return
		file_type, file_id = _extract_media_info(message)
		preview_info = _extract_preview_info(message, file_type, file_id)
		session["items"].append({
			"file_id": file_id,
			"file_type": file_type,
			"duration": int(getattr(message.video, "duration", 0) or 0) if file_type == "video" else 0,
			**preview_info,
		})
		session["source_message"] = message
		session["processed_count"] = int(session.get("processed_count", 0)) + 1

		if len(session["items"]) >= MAX_BATCH_MEDIA:
			await _finish_upload(key, message, session)
		else:
			await _update_upload_panel(message, session)


async def _media_worker(worker_id: int) -> None:
	while True:
		message = await MEDIA_QUEUE.get()
		key = (
			(message.chat.id, message.from_user.id)
			if message.from_user
			else None
		)
		try:
			await _process_queued_media(message)
			# await _forward_media_in_background(message)
		except asyncio.CancelledError:
			raise
		except Exception as exc:
			print(f"[MEDIA_WORKER {worker_id}] failed: {exc}", flush=True)
			await message.reply(f"❌ 处理媒体失败: {exc}")
		finally:
			if key:
				remaining = max(0, USER_MEDIA_PENDING.get(key, 1) - 1)
				if remaining:
					USER_MEDIA_PENDING[key] = remaining
				else:
					USER_MEDIA_PENDING.pop(key, None)
			MEDIA_QUEUE.task_done()


@dp.message(F.chat.type == "private", Command("start"))
async def cmd_start(message: Message, command: CommandObject) -> None:
	print(f"[CMD_START]{command}", flush=True)
	print(f"{message}", flush=True)
	if "/start fly_" in message.text:
		await message.delete()
		return

	args = command.args or ""
	if not args.startswith("fly_"):
		return
	
	await message.reply(
		"👋 你好！\n\n"
		"发送文件 （ 图片/文件/视频/音频/语音...）给我，我会回复取件码(加密后字符串)。\n"
		"每批最多发送 10 个媒体，发送完成后点击“上传完成”。\n"
		"你也可以直接粘贴取件码，我会解码返回媒体。\n\n"
	)


@dp.message(Command("about"))
async def cmd_about(message: Message) -> None:
	await message.reply("你好\n欢迎")


@dp.message(
	F.chat.type == "private",
	F.document | F.photo | F.video | F.audio | F.voice | F.animation | F.sticker,
)
async def on_media(message: Message) -> None:
	if not message.from_user:
		return

	key = (message.chat.id, message.from_user.id)
	if USER_MEDIA_PENDING.get(key, 0) >= MAX_USER_PENDING:
		await _notify_media_limit(message, "发送速度过快，请稍后再试")
		return

	session = UPLOAD_SESSIONS.get(key)
	if not session:
		session = {
			"items": [],
			"accepted_count": 0,
			"processed_count": 0,
			"panel_message_id": None,
		}
		UPLOAD_SESSIONS[key] = session

	if int(session["accepted_count"]) >= MAX_BATCH_MEDIA:
		await _notify_media_limit(message, "每批最多上传 10 个媒体，多余媒体未加入")
		return

	try:
		MEDIA_QUEUE.put_nowait(message)
	except asyncio.QueueFull:
		if int(session["accepted_count"]) == 0:
			UPLOAD_SESSIONS.pop(key, None)
		await _notify_media_limit(message, "系统正在处理较多媒体，请稍后再试")
		return

	session["accepted_count"] = int(session["accepted_count"]) + 1
	USER_MEDIA_PENDING[key] = USER_MEDIA_PENDING.get(key, 0) + 1

@dp.callback_query(F.data.startswith("takeoff:"))
async def on_takeoff(callback: CallbackQuery) -> None:
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return
	else:
		for entity in callback.message.entities or []:
			entity_type = getattr(entity.type, "value", entity.type)
			if entity_type == "text_link" and entity.url and "https://b.oy/" in entity.url:
				parse_text = entity.url.replace("https://b.oy/", "")
				await extract_encode(parse_text, callback.message, callback.from_user.id)

		# await callback.answer(
		# 	text=f"查询完成{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
		# 	cache_time=10
		# )
		chat_id = callback.message.chat.id
		message_id = callback.message.message_id

		await callback.answer(
			url=f"https://t.me/autodecoder666bot?start=fly_{chat_id}_{message_id}",
			cache_time=10
		)
		print(f"{callback}")
	pass


@dp.callback_query(F.data.startswith("enc:"))
async def on_encode_controls(callback: CallbackQuery) -> None:
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return
	if callback.message.chat.type != "private":
		await callback.answer("仅支持私信", show_alert=True)
		return
	if callback.data == "enc:upload:done":
		key = (callback.message.chat.id, callback.from_user.id)
		lock = USER_MEDIA_LOCKS.setdefault(key, asyncio.Lock())
		async with lock:
			session = UPLOAD_SESSIONS.get(key)
			if not session or session.get("panel_message_id") != callback.message.message_id:
				await callback.answer("此上传批次已结束", show_alert=True)
				return
			unprocessed = int(session["accepted_count"]) - int(session["processed_count"])
			if unprocessed > 0:
				await callback.answer(f"还有 {unprocessed} 个媒体正在处理中，请稍后", show_alert=True)
				return
			try:
				await _finish_upload(key, callback.message, session)
			except Exception as exc:
				await callback.answer(f"完成上传失败: {exc}", show_alert=True)
				return
		await callback.answer("已进入编辑菜单")
		return

	state_key = (callback.message.chat.id, callback.message.message_id)
	state = ENCODER_UI_STATE.get(state_key)
	if not state:
		await callback.answer("此按钮已失效，请重新发送媒体", show_alert=True)
		return

	if (callback.from_user and callback.from_user.id) != int(state.get("owner_user_id", 0)):
		await callback.answer("只能由原发送者操作", show_alert=True)
		return

	try:
		_, group, value = str(callback.data).split(":", 2)
		if group == "fw":
			state["no_forward"] = value == "1"
		elif group == "fl":
			state["flash_seconds"] = int(value)
		elif group == "vu":
			if value not in {"perm", "10m", "30m", "1h"}:
				raise ValueError("invalid valid mode")
			state["valid_mode"] = value
		else:
			raise ValueError("unknown control group")

		long_flash_seconds = int(state.get("video_flash_seconds", 60))
		force_no_forward = (
			int(state.get("flash_seconds", 0)) in {20, long_flash_seconds}
			or str(state.get("valid_mode", "perm")) in {"10m", "30m"}
		)
		if force_no_forward:
			state["no_forward"] = True

		token, encoded, parsed = _build_token_and_encoded(state)
		markup = _build_controls_keyboard(state, encoded)
		await callback.message.edit_text(_build_display(parsed, token, encoded), reply_markup=markup, parse_mode="HTML")
		await callback.answer("已更新密文")
	except Exception as exc:
		await callback.answer(f"更新失败: {exc}", show_alert=True)


async def extract_encode(parse_text: str, message: Message, receiver_id: int = None) -> str:
	token = UtfConverter.unicode_cjk_to_telegram(parse_text)
	data = UtfConverter.parse_file_token(token)

	valid_until_dt = datetime.strptime(str(data["valid_until"]), "%Y%m%d%H%M%S")
	now = datetime.now()
	_cleanup_used_flash_nonces(now)

	if now > valid_until_dt:
		overdue_seconds = int((now - valid_until_dt).total_seconds())
		overdue_text = _format_duration(overdue_seconds)
		await message.reply(
			"❌ 此 token 已过期\n"
			f"过期时间: {valid_until_dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
			f"已过期: {overdue_text}"
		)
		return

	flash_seconds = int(data.get("flash_seconds", 0))
	nonce_key = str(data.get("nonce", ""))
	if flash_seconds > 0:
		expires_at = USED_FLASH_NONCES.get(nonce_key)
		if expires_at and now < expires_at:
			await message.reply("❌ 此闪读密文仅可读取一次")
			return
		if str(data.get("valid_until", "")) == "99991231235959":
			expires_at = now + timedelta(days=PERM_FLASH_NONCE_RETENTION_DAYS)
		else:
			expires_at = valid_until_dt
		USED_FLASH_NONCES[nonce_key] = expires_at
		marked_nonce = nonce_key

	sent_media_messages = await _send_all_media(message, data, receiver_id=receiver_id)

	if flash_seconds > 0:
		for sent_media_message in sent_media_messages:
			asyncio.create_task(_delete_message_later(sent_media_message, flash_seconds))

	'''
	await message.reply(
		"✅ 解码成功\n\n"
		f"token:\n{token}\n\n"
		"解析字段:\n"
		f"nonce: {data['nonce']}\n"
		f"user_id: {data['user_id']}\n"
		f"file_id: {data['file_id']}\n"
		f"file_type: {data['file_type']}\n"
		f"no_forward: {data['no_forward']}\n"
		f"flash_seconds: {data['flash_seconds']}\n"
		f"valid_until: {data['valid_until']}"
	)
	'''

@dp.message(F.chat.type == "private", F.text)
async def on_text(message: Message) -> None:
	text = (message.text or "").strip()
	if not text or len(text) < 15:
		return

	marked_nonce = ""
	try:
		parse_text = text
		START = "⟦["
		END = "]⟧"
		pattern = re.escape(START) + r"(.*?)" + re.escape(END)
		matches = re.findall(pattern, text, flags=re.S)

		for item in matches:
			parse_text = item.strip()
			break

		await extract_encode(parse_text, message)

	except Exception as exc:
		if marked_nonce:
			USED_FLASH_NONCES.pop(marked_nonce, None)
		await message.reply(f"❌ 解码或解析失败: {exc}")


async def main() -> None:
	global bot_name
	me = await bot.get_me()
	bot_name = str(getattr(me, "username", "") or "")
	print(f"Bot started as @{bot_name}", flush=True)
	await bot.set_my_commands(
		[BotCommand(command="start", description="开始")],
		scope=BotCommandScopeAllPrivateChats(),
	)
	workers = [asyncio.create_task(_media_worker(index)) for index in range(MEDIA_WORKER_COUNT)]
	try:
		await dp.start_polling(bot)
	finally:
		for worker in workers:
			worker.cancel()
		await asyncio.gather(*workers, return_exceptions=True)


if __name__ == "__main__":
	asyncio.run(main())
