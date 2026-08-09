"""
使用 aiogram 的 Bot API 实现一个 Telegram Bot：

1) 接收用户发送的文件，获取 file_unique_id，
	先通过 build_file_token 生成 token，再用 telegram_to_unicode_cjk 转成 CJK 字符串。

2) 接收用户粘贴的 CJK 字符串，
	先用 unicode_cjk_to_telegram 还原 token，再用 parse_file_token 解析字段。
"""



from __future__ import annotations
import base64
import hashlib
import hmac
import re
import asyncio
import os
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile
from aiogram.types import User, BotCommand, BotCommandScopeAllPrivateChats, BufferedInputFile, CallbackQuery, ChatJoinRequest, ChatMemberUpdated, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)


from html import escape
from typing import Union

from utils.utf_utils import UtfConverter
from utils.blacklist_utils import BlacklistEntry, BlacklistStore
from utils.batch_utils import BatchStore
from utils.invite_link_utils import SharedInviteLinkStore
from utils.received_media_utils import ReceivedMediaStore
from utils.user_utils import UserExpireCache, UserExpire
from dotenv import load_dotenv



def _parse_whitelist_ids(raw: str) -> set[int]:
	ids: set[int] = set()
	for item in str(raw or "").split(","):
		text = item.strip()
		if not text:
			continue
		if text.lstrip("-").isdigit():
			ids.add(int(text))
	return ids


load_dotenv(dotenv_path='.env')
BOT_TOKEN = os.getenv("BOT_TOKEN")
MEDIA_FORWARD_USER_ID = int(os.getenv("MEDIA_FORWARD_USER_ID", "0") or 0)
ADMIN_USER_IDS = _parse_whitelist_ids(os.getenv("ADMIN_USER_IDS", ""))
#取件码及预览发送群组
ENCODED_FORWARD_CHAT_ID = int(os.getenv("ENCODED_FORWARD_CHAT_ID", "0") or 0)
ENCODED_FORWARD_THREAD_ID = int(os.getenv("ENCODED_FORWARD_THREAD_ID", "0") or 0)
#发言可以增加通行证时间的群组
MESSAGE_REWARD_CHAT_ID = int(os.getenv("MESSAGE_REWARD_CHAT_ID", str(ENCODED_FORWARD_CHAT_ID)) or 0)
DEFAULT_COVER_FILE_ID: str | None = None

volume_mount_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
default_user_expire_db_path = (
	Path(volume_mount_path) / "user_expire.sqlite3"
	if volume_mount_path
	else Path(__file__).resolve().parent / "data" / "user_expire.sqlite3"
)
user_expire_db_path = Path(
	os.getenv("USER_EXPIRE_DB_PATH", str(default_user_expire_db_path))
)
user_expire_cache = UserExpireCache(db_path=user_expire_db_path)
blacklist_store = BlacklistStore(db_path=user_expire_db_path)
batch_store = BatchStore(db_path=user_expire_db_path)
received_media_store = ReceivedMediaStore(db_path=user_expire_db_path)
shared_invite_link_store = SharedInviteLinkStore(db_path=user_expire_db_path)

from config import MEDIA_UPLOAD_EXTEND_MINUTES, MEDIA_VIEW_COST_MINUTES, MESSAGE_EXTEND_MINUTES, MAX_VALID_DURATION_MINUTES
from textwrap import dedent

UTC8 = timezone(timedelta(hours=8))



if not BOT_TOKEN:
	raise RuntimeError("Missing bot token. Please set ENCBOT_TOKEN or BOT_TOKEN.")


bot = Bot(
	token=BOT_TOKEN,
	default=DefaultBotProperties(link_preview_is_disabled=True),
)
dp = Dispatcher()
ENCODER_UI_STATE: dict[tuple[int, int], dict[str, Any]] = {}
UPLOAD_SESSIONS: dict[tuple[int, int], dict[str, Any]] = {}
USER_MEDIA_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
USER_MEDIA_PENDING: dict[tuple[int, int], int] = {}
OVERFLOW_NOTICE_TIME: dict[tuple[int, int], float] = {}
TAKEOFF_COUNTER_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
TAKEOFF_COUNTS: dict[tuple[int, int], int] = {}
TAKEOFF_USER_LOCKS: dict[int, asyncio.Lock] = {}
BATCH_LOCATION_LOCK = asyncio.Lock()
PENDING_BATCH_DISCUSSION_LOCATIONS: OrderedDict[
	tuple[int, int], tuple[int, int]
] = OrderedDict()
MAX_PENDING_BATCH_DISCUSSION_LOCATIONS = 500
AIRPORT_QUIZ_PROGRESS: dict[int, int] = {}
AIRPORT_QUIZ_RETRY_AT: dict[int, int] = {}
AIRPORT_QUIZ_PASSED_UNTIL: dict[int, int] = {}
AIRPORT_QUIZ_LOCKS: dict[int, asyncio.Lock] = {}
AIRPORT_INVITE_LINK_LOCK = asyncio.Lock()
PAID_INVITE_LOCKS: dict[str, asyncio.Lock] = {}
USED_PAID_INVITES: dict[str, int] = {}
USED_INVITE_CONFIRMATIONS: dict[tuple[int, int], int] = {}
PENDING_AIRPORT_JOIN_INVITES: dict[int, tuple[str, int]] = {}
MEDIA_QUEUE: asyncio.Queue[Message] = asyncio.Queue(maxsize=100)
MEDIA_FORWARD_QUEUE: asyncio.Queue[tuple[int, int]] = asyncio.Queue(maxsize=200)
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
USED_FLASH_NONCES: dict[tuple[str, int], datetime] = {}
PERM_FLASH_NONCE_RETENTION_DAYS = 30
AIRPORT_QUIZ_RETRY_SECONDS = 30 * 60
AIRPORT_QUIZ_PASS_SECONDS = 30 * 60
PAID_INVITE_COST_MINUTES = 24 * 60
PAID_INVITE_REWARD_MINUTES = 2 * 24 * 60
PAID_INVITE_LIFETIME_HOURS = 24
PAID_INVITE_USED_RETENTION_SECONDS = 48 * 60 * 60
PAID_INVITE_NAME_PATTERN = re.compile(
	r"^PI1\.([0-9a-z]{1,13})\.([0-9a-z]{5})\.([A-Za-z0-9_-]{8})$"
)
AIRPORT_INVITE_LINK_KEY = "airport-approved"
AIRPORT_INVITE_LINK_NAME = "airport-approved-url"
AIRPORT_QUIZ_QUESTIONS = (
	(
		"关于机场内资源的使用与讨论，以下哪种做法符合“三个禁止”？",
		(
			"利用资源营利，但不转发评论",
			"禁止营利，但可私下转发",
			"禁止转发，但可公开评判他人",
			"不营利、不外传、不随意评判",
			"小众资源不能外传，其他可分享",
		),
		3,
	),
	(
		"关于成员参与和群内关系，以下哪种态度符合机场的“三个原则”？",
		(
			"塔台是主人，成员只需服从",
			"真诚发言或分享，共同参与交流",
			"塔台应监督消息并处理所有争议",
			"成员只领取资源，不必参与",
			"群内参与应以付费交易为主",
		),
		1,
	),
	(
		"面对群内争议、系统故障或违规行为，以下哪种理解符合“三个任性”？",
		(
			"塔台须裁决资源归属与所有纠纷",
			"故障时塔台须立即修复并赔偿",
			"成员被移除后须公开完整说明",
			"群内无需管理，也不处理违规",
			"塔台须判定成员间一切是非",
			"违反核心价值者可被移除且不另解释",
		),
		5,
	),
	(
		"如果想维持飞行通行证的有效期，以下哪种做法符合机场规则？",
		(
			"只领取资源，不参与群内互动",
			"付费购买通行证有效期",
			"有效发言或上传资源保持活跃",
			"外传群内资源换取有效期",
			"上传非正太或萝莉资源",
		),
		2,
	),
	(
		"上传媒体资源时，以下哪种做法符合机场的上传规定？",
		(
			"不同系列混批上传且数量不限",
			"不同系列分批，每批最多十个",
			"重复上传无效或相同资源",
			"上传清水媒体换取正太资源",
			"上传后无需检查内容与系列",
		),
		1,
	),
	(
		"关于塔台(机器人)、飞机场(频道)与航站大厅(群组)的作用，以下哪一项说明错误？",
		(
			"塔台用于分享资源及进入群组",
			"指路牌用于指引前往机场",
			"飞机场用于查看并获取资源",
			"航站大厅用于交流并延长通行证",
			"三者功能完全相同",
		),
		4,
	),
)

JOIN_MODE = "invite"  # 可选值: "invite" 或 "request"


ENCODED_FORWARD_SEND_LOCK = asyncio.Lock()
async def _telegram_call_with_retry(
    label: str,
    operation,
    max_attempts: int = 4,
):
	async with ENCODED_FORWARD_SEND_LOCK:
		for attempt in range(max_attempts):
			try:
				return await operation()
			except TelegramRetryAfter as exc:
				if attempt + 1 >= max_attempts:
					raise

				delay = max(1, int(exc.retry_after)) + 1
				print(
					f"[TELEGRAM_RATE_LIMIT] {label}: "
					f"retry in {delay}s ({attempt + 1}/{max_attempts})",
					flush=True,
				)
				await asyncio.sleep(delay)



SELINE_IMAGE_PATHS = (Path(__file__).resolve().parent / "sepline.jpeg",)
TRADE_IMAGE_PATHS = (Path(__file__).resolve().parent / "trade.jpeg",)



@lru_cache(maxsize=1)
def _get_seline_image_bytes() -> bytes:
	for image_path in SELINE_IMAGE_PATHS:
		if image_path.is_file():
			return image_path.read_bytes()
	raise FileNotFoundError("Missing seline image: sepline.jpeg")


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


def _format_datetime_utc8(value: datetime) -> str:
	if value.tzinfo is None:
		value = value.replace(tzinfo=UTC8)
	return value.astimezone(UTC8).strftime("%m-%d %H:%M")


def _format_timestamp_utc8(timestamp: int) -> str:
	return _format_datetime_utc8(datetime.fromtimestamp(timestamp, tz=timezone.utc))





async def get_user_hyperlink(
    bot: Bot,
    user_info: Union[dict, User],
    show_uid: bool = False
) -> str:
    """
    取得 Telegram User hyperlink

    支援:
    - aiogram.types.User
    - dict user_info

    如果沒有 first_name，會自動透過 Telegram API 查詢
    """

    print(f"[get_user_hyperlink] user_info: {user_info}, show_uid: {show_uid}", flush=True)

    # --------------------
    # 先解析 user_id
    # --------------------
    if isinstance(user_info, User):
        user_id = user_info.id
        first_name = user_info.first_name or ""
        last_name = user_info.last_name or ""
        username = user_info.username

    else:
        user_id = user_info.get("id") or user_info.get("user_id")

        first_name = user_info.get("first_name") or ""
        last_name = user_info.get("last_name") or ""
        username = user_info.get("username")


    # --------------------
    # 沒有姓名資料，呼叫 API
    # --------------------
    if not first_name.strip():

        if user_id:
            user = await bot.get_chat(user_id)

            first_name = user.first_name or ""
            last_name = user.last_name or ""
            username = user.username

            user_id = user.id


    # --------------------
    # 組合名稱
    # --------------------
    user_title = first_name

    if last_name:
        user_title += f" {last_name}"

    if not user_title.strip():
        user_title = str(user_id)


    user_title = escape(user_title)


    # --------------------
    # 建立 hyperlink
    # --------------------
    if username:
        text = (
            f"<a href='https://t.me/{username}'>"
            f"{user_title}</a>"
        )
    else:
        text = (
            f"<a href='tg://user?id={user_id}'>"
            f"{user_title}</a>"
        )


    if show_uid:
        text += f" <code>{user_id}</code>"


    return text

def _extract_media_info(message: Message) -> tuple[str, str]:
	"""
	从消息中提取 (file_type, file_id)。
	若不是支持的媒体类型，抛出 ValueError。
	"""
	if message.document:
		mime_type = str(message.document.mime_type or "").lower()
		if mime_type == "video/mp4":
			return "video", message.document.file_id
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


def _extract_media_unique_id(message: Message) -> str:
	media = (
		message.document
		or (message.photo[-1] if message.photo else None)
		or message.video
		or message.audio
		or message.voice
		or message.animation
		or message.sticker
	)
	file_unique_id = str(getattr(media, "file_unique_id", "") or "").strip()
	if not file_unique_id:
		raise ValueError("媒体缺少 file_unique_id")
	return file_unique_id


def _extract_preview_info(message: Message, file_type: str, file_id: str) -> dict[str, str]:
	"""提取转发预览所需的缩略图标识，不把它写入取件码。"""
	if file_type == "document":
		return {}

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
	elif file_type == "video":
		if message.video:
			cover = getattr(message.video, "cover", None)
			if isinstance(cover, list) and cover:
				preview = cover[0]
			elif cover:
				preview = cover
			if not preview:
				preview = message.video.thumbnail
		elif message.document:
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


def _extract_media_metadata(message: Message, file_type: str) -> dict[str, Any]:
	media = {
		"document": message.document,
		"photo": message.photo[-1] if message.photo else None,
		"video": message.video or message.document,
		"audio": message.audio,
		"voice": message.voice,
		"animation": message.animation,
		"sticker": message.sticker,
	}.get(file_type)

	return {
		"file_size": max(0, int(getattr(media, "file_size", 0) or 0)),
		"duration": max(0, int(getattr(media, "duration", 0) or 0)),
		"file_name": str(getattr(media, "file_name", "") or ""),
	}


def _short_media_type(file_type: str) -> str:
	return {
		"document": "📄",
		"photo": "🖼️",
		"video": "🎬",
		"audio": "🎵",
		"voice": "🎙️",
		"animation": "🎞️",
		"sticker": "🏷️",
	}.get(file_type, "📎")


def _format_file_size(size: int) -> str:
	value = max(0, int(size or 0))
	if value == 0:
		return "未知大小"

	units = ("B", "KB", "MB", "GB", "TB")
	amount = float(value)
	unit = units[0]
	for unit in units:
		if amount < 1024 or unit == units[-1]:
			break
		amount /= 1024

	if unit == "B":
		return f"{int(amount)} {unit}"
	return f"{amount:.1f} {unit}"


def _format_media_duration(seconds: int) -> str:
	total_seconds = max(0, int(seconds or 0))
	hours, remainder = divmod(total_seconds, 3600)
	minutes, seconds = divmod(remainder, 60)
	if hours == 0:
		return f"{minutes:02d}:{seconds:02d}"
	return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


async def _build_display(data: dict[str, Any], token: str, encoded: str) -> str:
	valid_until = str(data.get("valid_until", ""))
	if valid_until == "99991231235959":
		valid_until_display = "永久有效"
	elif len(valid_until) == 14 and valid_until.isdigit():
		valid_until_display = _format_datetime_utc8(
			datetime.strptime(valid_until, "%Y%m%d%H%M%S").replace(tzinfo=UTC8)
		)
	else:
		valid_until_display = valid_until


	bot_name_lack = bot_name[:-1] if bot_name else ""
	start_char = "⟦["
	end_char = "]⟧"

	return_text=""

	if bool(data.get("anonymous", False)):
		user_url = "[匿名]"
	else:
		user_url = await get_user_hyperlink(bot, {"id":data.get("user_id", 0)}, show_uid=False)

	return_text += f"<a href=\"https://b.oy/{encoded}\">👤</a> {user_url} | "


	if(data['no_forward']==True):
		return_text += f"🚫 | "

	if(data['flash_seconds']>0):
		return_text += f"⚡ {data['flash_seconds']} 秒 | "
	# if bool(data.get("if_spoiler", False)):
	# 	return_text += "🙈 防剧透模式: 是\n"

	if(data['valid_until']!="99991231235959"):
		return_text += f"⏳ {valid_until_display} | "


	media_items = list(data.get("items", []))
	media_count = len(media_items)
	if media_count > 1:
		video_count = sum(
			1 for item in media_items
			if str(item.get("file_type", "")) == "video"
		)
		photo_count = sum(
			1 for item in media_items
			if str(item.get("file_type", "")) == "photo"
		)
		other_count = media_count - video_count - photo_count
		media_composition = [
			label
			for count, label in (
				(video_count, f"🎬x{video_count} "),
				(photo_count, f"🖼x{photo_count} "),
				(other_count, f"📄x{other_count} "),
			)
			if count > 0
		]
		return_text += f"📦 {media_count}  (  {' '.join(media_composition)} )\n"

	for item in media_items:
		
		_file_type = str(item.get("file_type", ""))
		if _file_type not in {"document", "audio", "voice"}:
			continue
		
		parts = [
			_short_media_type(str(item.get("file_type", ""))),
			_format_file_size(int(item.get("file_size", 0) or 0)),
		]
		duration = int(item.get("duration", 0) or 0)
		if duration > 0:
			parts.append(_format_media_duration(duration))

		file_name = re.sub(r"\s+", " ", str(item.get("file_name", "") or "")).strip()
		if len(file_name) > 12:
			file_name = f"{file_name[:12]}..."
		parts.append(escape(file_name) if file_name else "未命名")
		return_text += f"{parts[0]} {' | '.join(parts[1:])}\n"

	return_text += f"<code>{"ㅤ"*25}</code>"
	# return_text += (
	# 	f"\n将取件码👇传给 🤖 <a href=\"https://b.oy/{encoded}\">🤖</a><code>{bot_name_lack}</code><code> t</code> (去空格) \n\n{start_char}<code>{encoded}</code>{end_char}"
	# )
	# if len(encoded) > 256:
	# 	return_text += "\n\nℹ️ 批量取件码较长，请长按上方密文复制。"

	return return_text

def _build_keyboard(data: dict[str, Any], token: str, encoded: str) -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(
		inline_keyboard=[[
			InlineKeyboardButton(
				text="🈲 立即停飞",
				callback_data=f"takeoff:ban",
			),
			InlineKeyboardButton(
				text="🛫 请求起飞",
				callback_data=f"takeoff:fly",
			)
		]]
	)


def _takeoff_count_from_keyboard(markup: InlineKeyboardMarkup) -> int:
	for row in markup.inline_keyboard:
		for button in row:
			if str(button.callback_data or "").startswith("takeoff:fly"):
				match = re.search(r"\(\s*(\d+)\s*\)\s*$", button.text)
				return int(match.group(1)) if match else 0
	return 0


async def _increment_takeoff_count(message: Message) -> int:
	markup = message.reply_markup
	if not markup:
		return 0

	key = (message.chat.id, message.message_id)
	lock = TAKEOFF_COUNTER_LOCKS.setdefault(key, asyncio.Lock())
	async with lock:
		current_count = TAKEOFF_COUNTS.get(key)
		if current_count is None:
			current_count = _takeoff_count_from_keyboard(markup)
		new_count = current_count + 1
		TAKEOFF_COUNTS[key] = new_count

		new_rows = []
		for row in markup.inline_keyboard:
			new_row = []
			for button in row:
				if str(button.callback_data or "").startswith("takeoff:fly"):
					button = button.model_copy(
						update={"text": f"🛫 请求起飞 ( {new_count} )"}
					)
				new_row.append(button)
			new_rows.append(new_row)

		await message.edit_reply_markup(
			reply_markup=InlineKeyboardMarkup(inline_keyboard=new_rows)
		)
		return new_count


def _resolve_valid_until(mode: str) -> str:
	if mode == "perm":
		return "99991231235959"
	if mode == "10m":
		return (datetime.now(UTC8) + timedelta(minutes=10)).strftime("%Y%m%d%H%M%S")
	if mode == "30m":
		return (datetime.now(UTC8) + timedelta(minutes=30)).strftime("%Y%m%d%H%M%S")
	if mode == "1h":
		return (datetime.now(UTC8) + timedelta(hours=1)).strftime("%Y%m%d%H%M%S")
	raise ValueError(f"Unsupported valid mode: {mode}")


def _choice(label: str, selected: bool) -> str:
	return f"✅ {label}" if selected else f"{label}"


def _build_controls_keyboard(state: dict[str, Any], encoded: str) -> InlineKeyboardMarkup:
	no_forward = bool(state.get("no_forward", False))
	
	flash_seconds = int(state.get("flash_seconds", 0))
	valid_mode = str(state.get("valid_mode", "perm"))
	long_flash_seconds = int(state.get("video_flash_seconds", 60))
	long_flash_label = f"{long_flash_seconds}秒" if bool(state.get("has_video", False)) else "60秒"

	owner_user_id = int(state.get("owner_user_id", 0))

	now_timestamp = int(datetime.now().timestamp())
	user_expire = user_expire_cache.get(int(owner_user_id))
	if not user_expire or user_expire.expire_timestamp <= now_timestamp:
		anonymous = bool(state.get("anonymous", False))
		rows = [
			[
				InlineKeyboardButton(
					text="🕶️ 目前不显示上传者" if anonymous else "👤 目前显示上传者",
					callback_data=f"enc:an:{0 if anonymous else 1}",
				)
			]
		]
	else:
		anonymous = bool(state.get("anonymous", True))
		rows = [
			[
				InlineKeyboardButton(
					text="🚫 目前限制转发" if no_forward else "🆗 目前可以转发",
					callback_data=f"enc:fw:{0 if no_forward else 1}",
				)
			],
			[
				InlineKeyboardButton(
					text="🙈 目前已启用防剧透模式" if state.get("if_spoiler", False) else "🐵 目前未启用防剧透模式",
					callback_data=f"enc:sp:{0 if state.get('if_spoiler', False) else 1}",
					)
			],
			[
				InlineKeyboardButton(
					text="🕶️ 目前不显示上传者" if anonymous else "👤 目前显示上传者",
					callback_data=f"enc:an:{0 if anonymous else 1}",
				)
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
					text=_choice("60分钟", valid_mode == "1h"),
					callback_data="enc:vu:1h",
				)
			],
		]
	# if len(encoded) <= 256:
	# 	rows.append([
	# 		InlineKeyboardButton(
	# 			text="📋 复制密文",
	# 			copy_text=CopyTextButton(text=encoded),
	# 		)
	# 	])
	# owner_user_id = int(state.get("owner_user_id", 0))
	# if owner_user_id in ENCODED_FORWARD_WHITELIST_USER_IDS:
	send_status = str(state.get("send_status", "idle"))
	revision = int(state.get("revision", 1))
	sent_revision = int(state.get("sent_revision", 0))
	if send_status == "sending":
		send_text = "⏳ 送出中"
	elif sent_revision == revision:
		send_text = "✅ 已送出"
	elif send_status == "failed":
		send_text = "⚠️ 送出失败，重试"
	else:
		send_text = "📤 送出"
	rows.append([
		InlineKeyboardButton(
			text=send_text,
			callback_data="enc:send:now",
		),
		InlineKeyboardButton(
			text="❌ 取消",
			callback_data="enc:cancel:now",
		),
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
		if_spoiler=bool(state.get("if_spoiler", False)),
		anonymous=bool(state.get("anonymous", True)),
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
	if_spoiler = bool(data.get("if_spoiler", False))
	chat_id = receiver_id or message.from_user.id

	if file_type == "document":
		return await bot.send_document(chat_id=chat_id, document=file_id, protect_content=no_forward)
	if file_type == "photo":
		async with ENCODED_FORWARD_SEND_LOCK:
			return await bot.send_photo(
				chat_id=chat_id,
				photo=file_id,
				protect_content=no_forward,
				has_spoiler=if_spoiler,
			)
	if file_type == "video":
		async with ENCODED_FORWARD_SEND_LOCK:
			return await bot.send_video(
				chat_id=chat_id,
				video=file_id,
				protect_content=no_forward,
				has_spoiler=if_spoiler,
			)
	if file_type == "audio":
		return await bot.send_audio(chat_id=chat_id, audio=file_id, protect_content=no_forward)
	if file_type == "voice":
		return await bot.send_voice(chat_id=chat_id, voice=file_id, protect_content=no_forward)
	if file_type == "animation":
		return await bot.send_animation(
			chat_id=chat_id,
			animation=file_id,
			protect_content=no_forward,
			has_spoiler=if_spoiler,
		)
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

def _build_input_media(item: dict[str, Any], if_spoiler: bool = False):
    file_id = str(item["file_id"])
    file_type = str(item["file_type"])

    if file_type == "photo":
        return InputMediaPhoto(media=file_id, has_spoiler=if_spoiler)
    if file_type == "video":
        return InputMediaVideo(media=file_id, has_spoiler=if_spoiler)
    if file_type == "document":
        return InputMediaDocument(media=file_id)
    if file_type == "audio":
        return InputMediaAudio(media=file_id)

    raise ValueError(f"Unsupported album type: {file_type}")


def _is_invalid_media_reference_error(exc: TelegramBadRequest) -> bool:
    error_text = str(getattr(exc, "message", exc)).casefold()
    return any(marker in error_text for marker in (
        "media_file_invalid",
        "wrong file identifier",
        "wrong file_id",
        "file is temporarily unavailable",
    ))


async def _send_all_media(
    message: Message,
    data: dict[str, Any],
    receiver_id: int = None,
) -> tuple[list[Message], list[dict[str, Any]]]:
    source_items = data.get("items") or [{
        "file_id": data["file_id"],
        "file_type": data["file_type"],
    }]
    items = [
        {**item, "_item_index": index}
        for index, item in enumerate(source_items, start=1)
    ]

    no_forward = bool(data.get("no_forward", False))
    if_spoiler = bool(data.get("if_spoiler", False))
    sent_messages: list[Message] = []
    skipped_items: list[dict[str, Any]] = []
    pending_group: list[dict[str, Any]] = []
    pending_kind: str | None = None

    async def send_item(item: dict[str, Any]) -> None:
        item_data = dict(data)
        item_data.update(item)
        try:
            sent = await _send_media_by_type(
                message,
                item_data,
                receiver_id=receiver_id,
            )
        except TelegramBadRequest as exc:
            if not _is_invalid_media_reference_error(exc):
                raise
            skipped_items.append(item)
            print(
                f"[MEDIA_SEND] skipped invalid file_id at item "
                f"#{item.get('_item_index', '?')}: {exc.message}",
                flush=True,
            )
            return
        sent_messages.append(sent)

    async def flush_group() -> None:
        nonlocal pending_group, pending_kind


        if not pending_group:
            return

        if len(pending_group) >= 2:
            media = [
                _build_input_media(item, if_spoiler=if_spoiler)
                for item in pending_group
            ]

            try:
                async with ENCODED_FORWARD_SEND_LOCK:
                    result = await bot.send_media_group(
                        chat_id=receiver_id or message.from_user.id,
                        media=media,
                        protect_content=no_forward,
                    )
            except TelegramBadRequest as exc:
                if not _is_invalid_media_reference_error(exc):
                    raise
                print(
                    "[MEDIA_SEND] album contains an invalid file_id; "
                    "retrying items individually",
                    flush=True,
                )
                for item in pending_group:
                    await send_item(item)
            else:
                sent_messages.extend(result)
        else:
            await send_item(pending_group[0])

        pending_group = []
        pending_kind = None

    for item in items:
        kind = _album_kind(str(item["file_type"]))

        # 不支持相簿的类型
        if kind is None:
            await flush_group()
            await send_item(item)
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
    return sent_messages, skipped_items

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


def _enqueue_media_forward(message: Message) -> None:
	if MEDIA_FORWARD_USER_ID <= 0:
		return

	try:
		MEDIA_FORWARD_QUEUE.put_nowait((message.chat.id, message.message_id))
	except asyncio.QueueFull:
		print("[MEDIA_FORWARD] queue full, skipped", flush=True)


async def _forward_media_in_background(from_chat_id: int, message_id: int) -> None:
	if MEDIA_FORWARD_USER_ID <= 0:
		return

	try:
		await asyncio.wait_for(
			bot.copy_message(
				chat_id=MEDIA_FORWARD_USER_ID,
				from_chat_id=from_chat_id,
				message_id=message_id,
			),
			timeout=30,
		)
	except Exception as exc:
		print(f"[MEDIA_FORWARD] forward failed: {exc}", flush=True)


async def _media_forward_worker() -> None:
	while True:
		from_chat_id, message_id = await MEDIA_FORWARD_QUEUE.get()
		try:
			await _forward_media_in_background(from_chat_id, message_id)
		except asyncio.CancelledError:
			raise
		finally:
			MEDIA_FORWARD_QUEUE.task_done()


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
	owner_user_id: int,
	encoded: str,
	items: list[dict[str, Any]],
) -> dict:
	global DEFAULT_COVER_FILE_ID
	if ENCODED_FORWARD_CHAT_ID == 0:
		return {"ok":False,"error_msg":"ENCODED_FORWARD_CHAT_ID is 0"}

	def success_result(published_message: Message, mode: str) -> dict[str, Any]:
		return {
			"ok": True,
			"mode": mode,
			"channel_chat_id": int(published_message.chat.id),
			"channel_message_id": int(published_message.message_id),
		}



	preview_show = True

	display_keyboard = None
	display_text = encoded
	try:
		token = UtfConverter.unicode_cjk_to_telegram(encoded)
		parsed = UtfConverter.parse_file_token(token)
		parsed_items = list(parsed.get("items", []))
		if not parsed_items:
			raise ValueError("encoded 中没有媒体")
		display_text = await _build_display(parsed, token, encoded)
		display_keyboard = _build_keyboard(parsed, token, encoded)
		if_spoiler = bool(parsed.get("if_spoiler", False))

		flash_seconds = parsed.get("flash_seconds", 0)
		if flash_seconds >0:
			preview_show = False


		preview_payloads: list[tuple[bytes, str]] = []
		if preview_show:
			preview_entries: list[tuple[tuple[str, str], str]] = []
			download_requests: dict[tuple[str, str], str] = {}
			job_video_types: dict[tuple[str, str], bool] = {}
			source_items_by_file_id = {
				str(item.get("file_id", "")): item
				for item in items
				if item.get("file_id")
			}

			for index, parsed_item in enumerate(parsed_items):
				file_type = str(parsed_item.get("file_type", ""))
				if file_type == "document":
					continue

				preview_file_id = ""
				preview_unique_id = str(parsed_item.get("file_id", index))
				source_item = source_items_by_file_id.get(
					str(parsed_item.get("file_id", "")),
				)
				if source_item:
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

			for cache_key, filename in preview_entries:
				content = _preview_cache_get(cache_key)
				if content is None:
					content = processed[cache_key][0]
				preview_payloads.append((content, filename))

		thread_id = ENCODED_FORWARD_THREAD_ID if ENCODED_FORWARD_THREAD_ID > 0 else None
		caption = display_text if len(display_text) <= 1024 else None

	except Exception as exc:
		print(f"{exec}")
		return {"ok":False}
		

		
	try:

		if preview_show and preview_payloads:
			media = [
				InputMediaPhoto(
					media=BufferedInputFile(content, filename=filename),
					has_spoiler=if_spoiler,
					# caption=caption if index == 0 else None,
					# parse_mode="HTML" if index == 0 and caption else None,
				)
				for index, (content, filename) in enumerate(preview_payloads)
			]

			if len(media) == 1:
				content, filename = preview_payloads[0]
				published_message = await _telegram_call_with_retry(
					"send preview photo",
					lambda: bot.send_photo(
						chat_id=ENCODED_FORWARD_CHAT_ID,
						message_thread_id=thread_id,
						photo=BufferedInputFile(content, filename=filename),
						caption=caption,
						reply_markup=display_keyboard if caption else None,
						parse_mode="HTML" if caption else None,
						has_spoiler=if_spoiler,
					),
				)
				if caption is None:
					published_message = await _telegram_call_with_retry(
						"send encoded text",
						lambda: bot.send_message(
							chat_id=ENCODED_FORWARD_CHAT_ID,
							message_thread_id=thread_id,
							text=display_text,
							reply_markup=display_keyboard,
							parse_mode="HTML",
						),
					)

			else:

				album = await _telegram_call_with_retry(
					"send preview album",
					lambda: bot.send_media_group(
						chat_id=ENCODED_FORWARD_CHAT_ID,
						message_thread_id=thread_id,
						media=media,
					),
				)

				published_message = await _telegram_call_with_retry(
					"send encoded text",
					lambda: bot.send_message(
						chat_id=ENCODED_FORWARD_CHAT_ID,
						message_thread_id=thread_id,
						text=display_text,
						reply_markup=display_keyboard,
						parse_mode="HTML",
						reply_to_message_id=album[-1].message_id,
					),
				)




				# if DEFAULT_COVER_FILE_ID is None:
				# 	send_result = await bot.send_photo(
				# 		chat_id=ENCODED_FORWARD_CHAT_ID,
				# 		message_thread_id=thread_id,
				# 		photo=BufferedInputFile(
				# 			_get_seline_image_bytes(),
				# 			filename="seline.jpeg",
				# 		),
				# 		caption=caption,
				# 		reply_markup=display_keyboard,
				# 		parse_mode="HTML" if caption else None,
				# 		reply_to_message_id=album[-1].message_id
				# 	)
				# 	DEFAULT_COVER_FILE_ID = send_result.photo[-1].file_id if send_result.photo else None
				# else:
				# 	await bot.send_photo(
				# 		chat_id=ENCODED_FORWARD_CHAT_ID,
				# 		message_thread_id=thread_id,
				# 		photo=DEFAULT_COVER_FILE_ID,
				# 		caption=caption,
				# 		reply_markup=display_keyboard,
				# 		parse_mode="HTML" if caption else None,
				# 		reply_to_message_id=album[-1].message_id
				# 	)

				# if caption is None:
				# 	await bot.send_message(
				# 		chat_id=ENCODED_FORWARD_CHAT_ID,
				# 		message_thread_id=thread_id,
				# 		text=display_text,
				# 		parse_mode="HTML",
				# 	)
			return success_result(published_message, "preview")
		elif preview_show:

			fallback_message = await _telegram_call_with_retry(
				"send text fallback",
				lambda: bot.send_message(
					chat_id=ENCODED_FORWARD_CHAT_ID,
					message_thread_id=thread_id,
					text=display_text,
					parse_mode="HTML",
					reply_markup=display_keyboard,
				),
			)

			return success_result(fallback_message, "text_fallback")


		else:
			if DEFAULT_COVER_FILE_ID is None:
				published_message = await _telegram_call_with_retry(
					"send default cover",
					lambda: bot.send_photo(
						chat_id=ENCODED_FORWARD_CHAT_ID,
						message_thread_id=thread_id,
						photo=BufferedInputFile(
							_get_seline_image_bytes(),
							filename="seline.jpeg",
						),
						caption=caption,
						reply_markup=display_keyboard if caption else None,
						parse_mode="HTML" if caption else None,
					),
				)
				DEFAULT_COVER_FILE_ID = (
					published_message.photo[-1].file_id
					if published_message.photo
					else None
				)
			else:
				published_message = await _telegram_call_with_retry(
					"send cached default cover",
					lambda: bot.send_photo(
						chat_id=ENCODED_FORWARD_CHAT_ID,
						message_thread_id=thread_id,
						photo=DEFAULT_COVER_FILE_ID,
						caption=caption,
						reply_markup=display_keyboard if caption else None,
						parse_mode="HTML" if caption else None,
					),
				)

			if caption is None:
				published_message = await _telegram_call_with_retry(
					"send encoded text",
					lambda: bot.send_message(
						chat_id=ENCODED_FORWARD_CHAT_ID,
						message_thread_id=thread_id,
						text=display_text,
						reply_markup=display_keyboard,
						parse_mode="HTML",
					),
				)
			return success_result(published_message, "default_cover")
	except TelegramRetryAfter as exc:
		print(f"[ENCODED_FORWARD] rate limit retries exhausted: {exc}", flush=True)
		return {"ok": False, "reason": "rate_limited", "retry_after": exc.retry_after}
	except TelegramBadRequest as exc:
		if "CHAT_RESTRICTED" in str(exc):
			print(
				"[ENCODED_FORWARD] target chat forbids media",
				flush=True,
			)
			return {
				"ok": False,
				"reason": "chat_restricted",
				"failed_stage": "media",
			}
		raise
	except Exception as exc:
		print(f"[ENCODED_FORWARD] send failed: {exc}", flush=True)


		try:
			fallback_message = await _telegram_call_with_retry(
				"send encoded fallback text",
				lambda: bot.send_message(
					chat_id=ENCODED_FORWARD_CHAT_ID,
					message_thread_id=ENCODED_FORWARD_THREAD_ID if ENCODED_FORWARD_THREAD_ID > 0 else None,
					text=display_text,
					reply_markup=display_keyboard,
					parse_mode="HTML",
				),
			)
			return success_result(fallback_message, "error_text_fallback")
		except Exception as fallback_exc:
			print(f"[ENCODED_FORWARD] text fallback failed: {fallback_exc}", flush=True)
		return {"ok":False}


def minutes_to_day_hour(minutes: int):
	minutes = max(0, int(minutes))
	total_hours = minutes // 60
	days = total_hours // 24
	hours = total_hours % 24
	remaining_minutes = minutes % 60
	view_count = minutes // MEDIA_VIEW_COST_MINUTES

	parts = []
	if days:
		parts.append(f"{days} 天")
	if hours:
		parts.append(f"{hours} 小时")
	if remaining_minutes or not parts:
		parts.append(f"{remaining_minutes} 分钟")
	text = " ".join(parts)

	return text, view_count


async def _record_batch_channel_location(
	batch_id: str,
	channel_chat_id: int,
	channel_message_id: int,
) -> None:
	channel_key = (int(channel_chat_id), int(channel_message_id))
	async with BATCH_LOCATION_LOCK:
		batch_store.upsert_channel_location(
			batch_id,
			channel_key[0],
			channel_key[1],
		)
		pending_discussion = PENDING_BATCH_DISCUSSION_LOCATIONS.pop(
			channel_key,
			None,
		)
		if pending_discussion:
			batch_store.update_discussion_location(
				channel_key[0],
				channel_key[1],
				pending_discussion[0],
				pending_discussion[1],
			)


async def _send_encoded_snapshot(
	state_key: tuple[int, int],
	revision: int,
	owner_user_id: int,
	batch_id: str,
	encoded: str,
	items: list[dict[str, Any]],
	is_first_send: bool,
) -> None:
	success = False
	accepted_count = 0
	forward_status: dict[str, Any] = {}
	try:
		forward_status = await _forward_encoded_if_whitelisted(owner_user_id, encoded, items)
		success = bool(forward_status.get("ok", False))
	except Exception as exc:
		print(f"[ENCODED_FORWARD] background send failed: {exc}", flush=True)
		success = False
	if success:
		try:
			await _record_batch_channel_location(
				batch_id,
				int(forward_status["channel_chat_id"]),
				int(forward_status["channel_message_id"]),
			)
		except Exception as exc:
			print(f"[BATCH] channel location save failed: {exc}", flush=True)
	state = ENCODER_UI_STATE.get(state_key)
	if not state:
		return

	current_revision = int(state.get("revision", 1))
	if success:
		# 先记录成功版本，奖励或通知异常时也不会让按钮卡在“送出中”。
		state["sent_revision"] = revision
		try:
			accepted_count = received_media_store.accept_batch(
				[
					str(item.get("file_unique_id", ""))
					for item in items
				],
				batch_id,
			)
		except Exception as exc:
			print(f"[RECEIVED_MEDIA] accept failed: {exc}", flush=True)
		if is_first_send:
			try:
				now_timestamp = int(datetime.now().timestamp())
				previous_user_expire = user_expire_cache.get(owner_user_id)
				previous_expire_timestamp = (
					previous_user_expire.expire_timestamp
					if previous_user_expire
					else 0
				)
				base_timestamp = max(now_timestamp, previous_expire_timestamp)
				requested_minutes = 0

				# if accepted_count <= 0:
				# 	return  # 实际代码中应继续更新 UI，而非直接退出函数

				if accepted_count > 0:
					requested_minutes = accepted_count * MEDIA_UPLOAD_EXTEND_MINUTES
				
				user_expire = user_expire_cache.extend_minutes(
					owner_user_id,
					requested_minutes,
				)

				actual_added_minutes = max(
					0,
					(user_expire.expire_timestamp - base_timestamp) // 60,
				)
				remaining_minutes = max(
					0,
					(user_expire.expire_timestamp - now_timestamp) // 60,
				)
				actual_added_text = minutes_to_day_hour(actual_added_minutes)[0]
				remaining_text, remaining_view_count = minutes_to_day_hour(remaining_minutes)
				expire_text = _format_timestamp_utc8(user_expire.expire_timestamp)

				notify_text = (
					f"✅ 分享 {len(items)} 个资源成功，已为你延长 {actual_added_text} 的有效时间。\n"
					f"🎫 飞行通行证到期时间为：{expire_text}。（相当于 {remaining_view_count} 个资源）\n\n"
					f"🎈 请注意，通行证有效时间上限为 {minutes_to_day_hour(MAX_VALID_DURATION_MINUTES)[0]}，超过上限的部分将不会延长。"
				)

				await bot.send_message(
					chat_id=owner_user_id,
					text=notify_text,
				)

				print(
					f"[ENCODED_FORWARD] granted {actual_added_minutes}/{requested_minutes} "
					f"minutes to user {owner_user_id}",
					flush=True,
				)
			except Exception as exc:
				print(f"[ENCODED_FORWARD] membership reward failed: {exc}", flush=True)

	state["send_status"] = "idle" if success or current_revision != revision else "failed"

	current_encoded = str(state.get("encoded", ""))
	if not current_encoded:
		return
	try:
		await bot.edit_message_reply_markup(
			chat_id=state_key[0],
			message_id=state_key[1],
			reply_markup=_build_controls_keyboard(state, current_encoded),
		)
	except Exception as exc:
		print(f"[ENCODED_FORWARD] status keyboard update failed: {exc}", flush=True)

	


async def _handle_send_encoded(
	callback: CallbackQuery,
	state_key: tuple[int, int],
	state: dict[str, Any],
) -> None:
	owner_user_id = int(state.get("owner_user_id", 0))
	is_first_send = int(state.get("sent_revision", 0)) == 0
	# if owner_user_id not in ENCODED_FORWARD_WHITELIST_USER_IDS:
	# 	await callback.answer("你没有送出权限", show_alert=True)
	# 	return

	revision = int(state.get("revision", 1))
	if str(state.get("send_status", "idle")) == "sending":
		await callback.answer("正在送出，请勿重复点击")
		return
	if int(state.get("sent_revision", 0)) == revision:
		await callback.answer("当前版本已经送出")
		return

	encoded_snapshot = str(state.get("encoded", ""))
	if not encoded_snapshot:
		await callback.answer("当前密文无效，请重新上传", show_alert=True)
		return
	items_snapshot = [dict(item) for item in state.get("items", [])]
	if not items_snapshot:
		await callback.answer("当前媒体列表为空", show_alert=True)
		return
	batch_id = str(state.get("batch_id", "") or "").strip()
	if not batch_id:
		batch_id = secrets.token_urlsafe(12)
		state["batch_id"] = batch_id

	state["send_status"] = "sending"
	await callback.answer("正在送出")
	try:
		await callback.message.edit_reply_markup(
			reply_markup=_build_controls_keyboard(state, encoded_snapshot),
		)
	except Exception as exc:
		print(f"[ENCODED_FORWARD] sending keyboard update failed: {exc}", flush=True)

	task = asyncio.create_task(
		_send_encoded_snapshot(
			state_key=state_key,
			revision=revision,
			owner_user_id=owner_user_id,
			batch_id=batch_id,
			encoded=encoded_snapshot,
			items=items_snapshot,
			is_first_send=is_first_send,
		)
	)
	state["send_task"] = task

	def _clear_send_task(completed_task: asyncio.Task) -> None:
		if state.get("send_task") is completed_task:
			state.pop("send_task", None)

	task.add_done_callback(_clear_send_task)


async def _handle_cancel_encoded(
	callback: CallbackQuery,
	state_key: tuple[int, int],
	state: dict[str, Any],
) -> None:
	if str(state.get("send_status", "idle")) == "sending":
		await callback.answer("正在送出，暂时无法取消", show_alert=True)
		return

	was_sent = int(state.get("sent_revision", 0)) > 0
	if not was_sent:
		received_media_store.release_pending_many([
			str(item.get("file_unique_id", ""))
			for item in state.get("items", [])
		])

	ENCODER_UI_STATE.pop(state_key, None)
	text = "✅ 配置已关闭，已经送出的资源不受影响。" if was_sent else "✅ 已取消，本批媒体未送出。"
	try:
		await callback.message.edit_text(text, reply_markup=None)
	except Exception as exc:
		print(f"[ENCODED_CANCEL] panel update failed: {exc}", flush=True)
		await callback.message.edit_reply_markup(reply_markup=None)
	await callback.answer("配置已关闭" if was_sent else "已取消")


def _upload_keyboard() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(
		inline_keyboard=[[
			InlineKeyboardButton(text="⚙️ 上传已完成，进入配置", callback_data="enc:upload:done"),
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
		f"📥 已收到 {count} 个媒体 ( 不同系列请一定要分开传，每批次最多 {MAX_BATCH_MEDIA} 个 )\n\n"
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
		"anonymous": True,
		"if_spoiler": False,
		"flash_seconds": 0,
		"has_video": bool(video_durations),
		"video_flash_seconds": (max(video_durations) + 15) if video_durations else 60,
		"valid_mode": "perm",
		"revision": 1,
		"sent_revision": 0,
		"send_status": "idle",
	}
	token, encoded, parsed = _build_token_and_encoded(state)
	state["token"] = token
	state["encoded"] = encoded
	display_text = await _build_display(parsed, token, encoded)
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
		file_unique_id = _extract_media_unique_id(message)
		preview_info = _extract_preview_info(message, file_type, file_id)
		media_metadata = _extract_media_metadata(message, file_type)
		session["items"].append({
			"file_id": file_id,
			"file_unique_id": file_unique_id,
			"file_type": file_type,
			**media_metadata,
			**preview_info,
		})
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
			_enqueue_media_forward(message)
		except asyncio.CancelledError:
			raise
		except Exception as exc:
			print(f"[MEDIA_WORKER {worker_id}] failed: {exc}", flush=True)
			if key:
				file_unique_id = ""
				try:
					file_unique_id = _extract_media_unique_id(message)
				except Exception:
					pass
				session = UPLOAD_SESSIONS.get(key)
				was_added = bool(session and any(
					str(item.get("file_unique_id", "")) == file_unique_id
					for item in session.get("items", [])
				))
				if file_unique_id and not was_added:
					received_media_store.release_pending(
						file_unique_id,
						message.chat.id,
						message.message_id,
					)
					if session:
						session["accepted_count"] = max(
							int(session.get("processed_count", 0)),
							int(session.get("accepted_count", 0)) - 1,
						)
			await message.reply(f"❌ 处理媒体失败: {exc}")
		finally:
			if key:
				remaining = max(0, USER_MEDIA_PENDING.get(key, 1) - 1)
				if remaining:
					USER_MEDIA_PENDING[key] = remaining
				else:
					USER_MEDIA_PENDING.pop(key, None)
			MEDIA_QUEUE.task_done()


@dp.message(F.chat.type == "private", Command("me"))
async def cmd_me(message: Message) -> None:
	if not message.from_user:
		return

	now_timestamp = int(datetime.now().timestamp())
	user_expire = user_expire_cache.get(int(message.from_user.id))
	if not user_expire or user_expire.expire_timestamp <= now_timestamp:
		await message.reply(
			"🎫 飞行通行证\n\n"
			"状态：目前没有有效的通行证\n"
			"你可以在指定群组发言或分享资源来增加有效时间。\n\n"
			"🎈 如果你发现你的通行证归零，那就是机器人重开机了，灯台是不备份数据的。"
		)
		return

	remaining_seconds = user_expire.expire_timestamp - now_timestamp
	remaining_minutes = remaining_seconds // 60
	available_view_count = remaining_minutes // MEDIA_VIEW_COST_MINUTES
	expire_text = _format_timestamp_utc8(user_expire.expire_timestamp)

	await message.reply(
		"🎫 飞行通行证\n\n"
		"状态：✅ 有效\n"
		f"剩余时间：{_format_duration(remaining_seconds)}\n"
		f"到期时间：{expire_text}\n"
		f"目前可请求：{available_view_count} 个资源"
	)


@dp.message(F.chat.type == "private", Command("bonus"))
async def cmd_bonus(message: Message, command: CommandObject) -> None:
	if not _is_admin_message(message):
		await message.reply("❌ 无效指令")
		return

	if not message.from_user:
		return

	args = str(command.args or "").strip()

	if not args:
		target_user_id = int(message.from_user.id)
	else:
		target_user_id = _parse_positive_user_id(args)
		if target_user_id is None:
			await message.reply("用法：/bonus [用户id]")
			return



	bonus_minutes = MAX_VALID_DURATION_MINUTES
	now_timestamp = int(datetime.now().timestamp())
	previous_user_expire = user_expire_cache.get(target_user_id)
	previous_expire_timestamp = (
		previous_user_expire.expire_timestamp
		if previous_user_expire
		else 0
	)
	base_timestamp = max(now_timestamp, previous_expire_timestamp)

	user_expire = user_expire_cache.extend_minutes(
		target_user_id,
		bonus_minutes,
		group_message_timestamp=now_timestamp,
	)
	actual_added_minutes = max(
		0,
		(user_expire.expire_timestamp - base_timestamp) // 60,
	)
	actual_added_text = minutes_to_day_hour(actual_added_minutes)[0]
	remaining_minutes = max(
		0,
		(user_expire.expire_timestamp - now_timestamp) // 60,
	)
	remaining_text, remaining_view_count = minutes_to_day_hour(remaining_minutes)
	expire_text = _format_timestamp_utc8(user_expire.expire_timestamp)

	await message.reply(
		"✅ 已发放飞行时限奖励\n"
		f"目标用户：{target_user_id}\n"
		f"本次增加：{actual_added_text}\n"
		f"到期时间：{expire_text}\n"
		f"目前可请求：{remaining_view_count} 个资源（约 {remaining_text}）"
	)


def _is_admin_message(message: Message) -> bool:
	return bool(
		message.from_user
		and int(message.from_user.id) in ADMIN_USER_IDS
	)


def _parse_positive_user_id(raw: str) -> int | None:
	text = str(raw or "").strip()
	if not text.isdigit():
		return None
	user_id = int(text)
	return user_id if user_id > 0 else None


def _format_blacklist_entry(entry: BlacklistEntry) -> str:
	return (
		f"用户 ID：{entry.user_id}\n"
		f"封禁原因：{entry.reason}\n"
		f"操作管理员：{entry.created_by}\n"
		f"封禁时间：{_format_timestamp_utc8(entry.created_at)}"
	)


async def _ban_user(
	user_id: int,
	reason: str,
	created_by: int,
) -> tuple[BlacklistEntry, str]:
	entry = blacklist_store.ban(user_id, reason, created_by)
	if MESSAGE_REWARD_CHAT_ID == 0:
		return entry, "MESSAGE_REWARD_CHAT_ID 尚未配置"

	try:
		await bot.ban_chat_member(
			chat_id=MESSAGE_REWARD_CHAT_ID,
			user_id=user_id,
		)
	except Exception as exc:
		print(
			f"[BLACKLIST] failed to ban user {user_id} "
			f"from chat {MESSAGE_REWARD_CHAT_ID}: {exc}",
			flush=True,
		)
		return entry, str(exc)
	
	try:
		await _telegram_call_with_retry(
			"ban encoded-forward member",
			lambda: bot.ban_chat_member(
				chat_id=ENCODED_FORWARD_CHAT_ID,
				user_id=user_id,
			),
		)
	except Exception as exc:
		print(
			f"[BLACKLIST] failed to ban user {user_id} "
			f"from chat {ENCODED_FORWARD_CHAT_ID}: {exc}",
			flush=True,
		)
		return entry, str(exc)	

	return entry, ""


@dp.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject) -> None:
	if not _is_admin_message(message):
		return

	args = str(command.args or "").strip()
	parts = args.split(maxsplit=1)
	explicit_user_id = _parse_positive_user_id(parts[0]) if parts else None

	if explicit_user_id is not None:
		target_user_id = explicit_user_id
		reason = parts[1].strip() if len(parts) > 1 else ""
	else:
		replied_user = (
			message.reply_to_message.from_user
			if message.reply_to_message
			else None
		)
		target_user_id = int(replied_user.id) if replied_user else 0
		reason = args

	if target_user_id <= 0 or not reason:
		await message.reply(
			"用法：/ban [用户id] [原因]\n"
			"或回复用户消息：/ban [原因]"
		)
		return
	if target_user_id in ADMIN_USER_IDS:
		await message.reply("❌ 不能封禁管理员")
		return
	if len(reason) > 200:
		await message.reply("❌ 封禁原因不能超过 200 个字符")
		return

	entry, group_ban_error = await _ban_user(
		target_user_id,
		reason,
		int(message.from_user.id),
	)

	reply_text = f"✅ 已加入黑名单\n{_format_blacklist_entry(entry)}"
	if group_ban_error:
		reply_text += f"\n\n⚠️ 群组移除失败：{group_ban_error}"
	else:
		reply_text += "\n\n✅ 已从群组移除并禁止重新加入"
	await message.reply(reply_text)


@dp.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject) -> None:
	if not _is_admin_message(message):
		return

	target_user_id = _parse_positive_user_id(str(command.args or ""))
	if target_user_id is None:
		await message.reply("用法：/unban [用户id]")
		return

	if not blacklist_store.is_blocked(target_user_id):
		await message.reply(f"ℹ️ 用户不在黑名单中：{target_user_id}")
		return
	if MESSAGE_REWARD_CHAT_ID == 0:
		await message.reply(
			"❌ 无法解除封禁：MESSAGE_REWARD_CHAT_ID 尚未配置"
		)
		return

	try:
		await bot.unban_chat_member(
			chat_id=MESSAGE_REWARD_CHAT_ID,
			user_id=target_user_id,
			only_if_banned=True,
		)
	except Exception as exc:
		print(
			f"[BLACKLIST] failed to unban user {target_user_id} "
			f"from chat {MESSAGE_REWARD_CHAT_ID}: {exc}",
			flush=True,
		)
		await message.reply(f"❌ 群组解除封禁失败：{exc}")
		return

	try:
		await _telegram_call_with_retry(
			"unban encoded-forward member",
			lambda: bot.unban_chat_member(
				chat_id=ENCODED_FORWARD_CHAT_ID,
				user_id=target_user_id,
				only_if_banned=True,
			),
		)
	except Exception as exc:
		print(
			f"[BLACKLIST] failed to unban user {target_user_id} "
			f"from chat {ENCODED_FORWARD_CHAT_ID}: {exc}",
			flush=True,
		)
		await message.reply(f"❌ 群组解除封禁失败：{exc}")
		return


	blacklist_store.unban(target_user_id)
	await message.reply(
		f"✅ 已从黑名单移除并解除群组封禁：{target_user_id}"
	)


@dp.message(Command("baninfo"))
async def cmd_baninfo(message: Message, command: CommandObject) -> None:
	if not _is_admin_message(message):
		return

	target_user_id = _parse_positive_user_id(str(command.args or ""))
	if target_user_id is None:
		await message.reply("用法：/baninfo [用户id]")
		return

	entry = blacklist_store.get(target_user_id)
	if not entry:
		await message.reply(f"ℹ️ 用户不在黑名单中：{target_user_id}")
		return
	await message.reply(f"🚫 黑名单资料\n{_format_blacklist_entry(entry)}")


@dp.message(Command("banlist"))
async def cmd_banlist(message: Message, command: CommandObject) -> None:
	if not _is_admin_message(message):
		return

	page_text = str(command.args or "").strip()
	page = _parse_positive_user_id(page_text) if page_text else 1
	if page is None:
		await message.reply("用法：/banlist [页码]")
		return

	page_size = 10
	entries, total = blacklist_store.list_page(page, page_size)
	if total == 0:
		await message.reply("黑名单目前为空")
		return

	total_pages = (total + page_size - 1) // page_size
	if page > total_pages:
		await message.reply(f"❌ 页码超出范围，共 {total_pages} 页")
		return

	lines = [f"🚫 黑名单（第 {page}/{total_pages} 页，共 {total} 人）"]
	for entry in entries:
		lines.append(
			f"{entry.user_id}｜{entry.reason}｜"
			f"{_format_timestamp_utc8(entry.created_at)}"
		)
	await message.reply("\n".join(lines))


def _base36_encode(value: int) -> str:
	digits = "0123456789abcdefghijklmnopqrstuvwxyz"
	value = int(value)
	if value < 0:
		raise ValueError("Base36 value cannot be negative")
	if value == 0:
		return "0"
	encoded = ""
	while value:
		value, remainder = divmod(value, 36)
		encoded = digits[remainder] + encoded
	return encoded


def _paid_invite_signature(payload: str) -> str:
	secret = str(os.getenv("INVITE_SIGNING_SECRET", "") or BOT_TOKEN or "")
	digest = hmac.new(
		secret.encode("utf-8"),
		payload.encode("ascii"),
		hashlib.sha256,
	).digest()
	return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:8]


def _build_paid_invite_name(inviter_user_id: int) -> str:
	encoded_user_id = _base36_encode(inviter_user_id)
	if len(encoded_user_id) > 13:
		raise ValueError("Telegram user ID is too large for paid invite name")
	nonce = _base36_encode(secrets.randbelow(36 ** 5)).zfill(5)
	payload = f"PI1.{encoded_user_id}.{nonce}"
	name = f"{payload}.{_paid_invite_signature(payload)}"
	if len(name) > 32:
		raise ValueError("Paid invite name exceeds Telegram's 32-character limit")
	return name


def _parse_paid_invite_name(name: str | None) -> int | None:
	match = PAID_INVITE_NAME_PATTERN.fullmatch(str(name or ""))
	if not match:
		return None
	encoded_user_id, nonce, supplied_signature = match.groups()
	payload = f"PI1.{encoded_user_id}.{nonce}"
	expected_signature = _paid_invite_signature(payload)
	if not hmac.compare_digest(supplied_signature, expected_signature):
		return None
	try:
		user_id = int(encoded_user_id, 36)
	except ValueError:
		return None
	return user_id if user_id > 0 else None


def _prune_used_paid_invites(now_timestamp: int | None = None) -> None:
	now_timestamp = now_timestamp or int(datetime.now().timestamp())
	cutoff = now_timestamp - PAID_INVITE_USED_RETENTION_SECONDS
	for invite_link, used_at in list(USED_PAID_INVITES.items()):
		if used_at < cutoff:
			USED_PAID_INVITES.pop(invite_link, None)
			lock = PAID_INVITE_LOCKS.get(invite_link)
			if lock is not None and not lock.locked():
				PAID_INVITE_LOCKS.pop(invite_link, None)
	for confirmation_key, used_at in list(USED_INVITE_CONFIRMATIONS.items()):
		if used_at < cutoff:
			USED_INVITE_CONFIRMATIONS.pop(confirmation_key, None)
	for user_id, (_, expire_timestamp) in list(PENDING_AIRPORT_JOIN_INVITES.items()):
		if expire_timestamp > 0 and expire_timestamp <= now_timestamp:
			PENDING_AIRPORT_JOIN_INVITES.pop(user_id, None)


def _is_current_chat_member(status: Any) -> bool:
	return (
		status.status in ("member", "administrator", "creator")
		or (status.status == "restricted" and status.is_member is True)
	)


async def _is_airport_member(user_id: int) -> bool:
	for chat_id in (MESSAGE_REWARD_CHAT_ID, ENCODED_FORWARD_CHAT_ID):
		if chat_id == 0:
			continue
		try:
			status = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
		except Exception as exc:
			print(
				f"[PAID_INVITE] member lookup failed for user {user_id} "
				f"in chat {chat_id}: {exc}",
				flush=True,
			)
			continue
		if _is_current_chat_member(status):
			return True
	return False


def _paid_invite_confirmation_keyboard() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(
		inline_keyboard=[[
			InlineKeyboardButton(
				text="✅ 确认扣除 1 天并建立",
				callback_data="paid_invite:confirm",
			),
			InlineKeyboardButton(
				text="取消",
				callback_data="paid_invite:cancel",
			),
		]],
	)


@dp.message(F.chat.type == "private", Command("invite"))
async def cmd_invite(message: Message) -> None:
	if not message.from_user:
		return
	user_id = int(message.from_user.id)
	if blacklist_store.is_blocked(user_id):
		# await message.reply("❌ 你目前无法建立邀请连结。")
		return
	if MESSAGE_REWARD_CHAT_ID == 0:
		await message.reply("❌ 航站大厅尚未配置，请联系塔台。")
		return
	if not await _is_airport_member(user_id):
		await message.reply("❌ 只有航站大厅或机场的现有成员可以建立邀请。")
		return
	user_expire = user_expire_cache.get(user_id)
	now_timestamp = int(datetime.now().timestamp())
	if (
		not user_expire
		or user_expire.expire_timestamp - now_timestamp
		< PAID_INVITE_COST_MINUTES * 60
	):
		await message.reply(
			"❌ 飞行通行证余额不足。\n\n"
			f"你目前的飞行通行证剩余时间为：{_format_duration(0 if not user_expire else max(0, user_expire.expire_timestamp - now_timestamp))}\n"
			"建立邀请需要消耗完整的 1 天有效期。您可以透过在指定「机场大厅」群组发言或分享资源给塔台机器人来增加有效时间。"
		)
		return
	await message.reply(
		"🎟️ 建立单人邀请\n\n"
		"建立邀请将消耗 1 天飞行通行证。\n\n"
		"🔹 邀请连结有效 24 小时，仅限一位符合资格的申请者通过。\n"
		"🔹 申请者仍须拥有超过 2 天的通行证 (透过分享资源)，并完成飞官考试。\n"
		"🔹 连结建立后，过期、无人使用或申请遭拒均不退还通行证期限。\n"
		"🎁 申请者进入机场后，邀请人可得 2 天通行证期限。",
		reply_markup=_paid_invite_confirmation_keyboard(),
	)


@dp.callback_query(F.data == "paid_invite:cancel")
async def on_paid_invite_cancel(callback: CallbackQuery) -> None:
	if not callback.message:
		await callback.answer("无效的邀请确认", show_alert=True, cache_time=0)
		return
	key = (int(callback.from_user.id), int(callback.message.message_id))
	_prune_used_paid_invites()
	if key in USED_INVITE_CONFIRMATIONS:
		await callback.answer("此操作已经处理", cache_time=0)
		return
	USED_INVITE_CONFIRMATIONS[key] = int(datetime.now().timestamp())
	await callback.message.edit_text("已取消建立邀请。")
	await callback.answer("已取消", cache_time=0)


@dp.callback_query(F.data == "paid_invite:confirm")
async def on_paid_invite_confirm(callback: CallbackQuery) -> None:
	if not callback.message or callback.message.chat.type != "private":
		await callback.answer("邀请只能在机器人私聊中建立", show_alert=True, cache_time=0)
		return
	user_id = int(callback.from_user.id)
	confirmation_key = (user_id, int(callback.message.message_id))
	_prune_used_paid_invites()
	if confirmation_key in USED_INVITE_CONFIRMATIONS:
		await callback.answer("此操作已经处理", cache_time=0)
		return
	USED_INVITE_CONFIRMATIONS[confirmation_key] = int(datetime.now().timestamp())
	await callback.answer("正在建立邀请……", cache_time=0)
	try:
		await callback.message.edit_reply_markup(reply_markup=None)
	except Exception:
		pass

	if MESSAGE_REWARD_CHAT_ID == 0:
		await callback.message.edit_text("❌ 航站大厅尚未配置，请联系塔台。")
		return
	if blacklist_store.is_blocked(user_id):
		await callback.message.edit_text("❌ 你目前无法建立邀请连结。")
		return
	if not await _is_airport_member(user_id):
		await callback.message.edit_text(
			"❌ 只有航站大厅或机场的现有成员可以建立邀请。"
		)
		return

	user_lock = TAKEOFF_USER_LOCKS.setdefault(user_id, asyncio.Lock())
	async with user_lock:
		user_expire = user_expire_cache.get(user_id)
		now_timestamp = int(datetime.now().timestamp())
		if (
			not user_expire
			or user_expire.expire_timestamp - now_timestamp
			< PAID_INVITE_COST_MINUTES * 60
		):
			await callback.message.edit_text(
				"❌ 飞行通行证余额不足。\n"
				"建立邀请需要消耗完整的 1 天有效期。"
			)
			return

		invite = None
		consumed = False
		try:
			invite = await bot.create_chat_invite_link(
				chat_id=MESSAGE_REWARD_CHAT_ID,
				name=_build_paid_invite_name(user_id),
				expire_date=datetime.now(timezone.utc) + timedelta(
					hours=PAID_INVITE_LIFETIME_HOURS
				),
				creates_join_request=True,
			)
			updated_user = user_expire_cache.consume_minutes(
				user_id,
				PAID_INVITE_COST_MINUTES,
			)
			if updated_user is None:
				raise RuntimeError("飞行通行证余额不足")
			consumed = True
			remaining_seconds = max(
				0,
				updated_user.expire_timestamp - int(datetime.now().timestamp()),
			)
			await callback.message.edit_text(
				"✅ 单人审核邀请已建立\n\n"
				"已扣除 1 天飞行通行证。\n"
				"连结将在 24 小时后失效，并会在一位符合资格的申请者"
				"通过审核后撤销。\n"
				"申请者仍须拥有超过 2 天通行证并完成机场考试。\n\n"
				f"目前剩余时间：{_format_duration(remaining_seconds)}",
				reply_markup=InlineKeyboardMarkup(
					inline_keyboard=[[
						InlineKeyboardButton(
							text="点击复制邀请连结 📋",
							copy_text=CopyTextButton(text=invite.invite_link),
						)
					]],
				),
			)
		except Exception as exc:
			if invite is not None:
				try:
					await bot.revoke_chat_invite_link(
						chat_id=MESSAGE_REWARD_CHAT_ID,
						invite_link=invite.invite_link,
					)
				except Exception as revoke_exc:
					USED_PAID_INVITES[invite.invite_link] = int(
						datetime.now().timestamp()
					)
					print(
						f"[PAID_INVITE] rollback revoke failed for user "
						f"{user_id}: {revoke_exc}",
						flush=True,
					)
			if consumed:
				try:
					user_expire_cache.extend_minutes(
						user_id,
						PAID_INVITE_COST_MINUTES,
					)
				except Exception as refund_exc:
					print(
						f"[PAID_INVITE] refund failed for user {user_id}: "
						f"{refund_exc}",
						flush=True,
					)
			print(f"[PAID_INVITE] creation failed for user {user_id}: {exc}", flush=True)
			try:
				await callback.message.edit_text(
					"❌ 暂时无法建立邀请；若已扣除期限，系统已尝试自动退还。"
				)
			except Exception:
				pass


def _airport_access_text() -> str:
	return dedent("""
		现实世界已经足够喧嚣，我们都需要一个安静的角落，卸下疲惫与伪装，做回最真实的自己。

		这里没有KPI，没有社交面具，只有放松的闲聊与纯粹的光影陪伴。为了守护这份难得的清净，我们定下了这些小小的约定。如果你愿意遵守，欢迎入座：

		<blockquote>三个禁止</blockquote>
		1️⃣ 禁谈营利：这里不是名利场，不谈钱，不欢迎任何需要付费的灰色资源。
		2️⃣ 严禁外传：群内资源仅限内部参考与放松，请勿转发。我们只在自己的小圈子里分享快乐。
		3️⃣ 禁止评判：大家都是来放松的，不是来被说教的。遇到不喜欢的言论或资源，轻轻划过就好。若涉及小众内容，请体贴地使用防剧透模式。

		<blockquote>三个原则</blockquote>
		1️⃣ 没有主人：认同理念的都是主人，小圈圈里大家都是主人。但会有一个“塔台”来维护系统正常跟清理违规内容。
		2️⃣ 顺其自然：默认没有人时刻盯着。违规的内容“塔台”看到了就删，没看到便随风而去，一切随缘。
		3️⃣ 分享与参与：想进群，请先分享资源作为敲门砖；想看别人的资源，请多发言或继续分享。用真诚换真诚，用资源换资源。

		<blockquote>三个任性</blockquote>
		1️⃣ 不想管：谁偷了谁的原创，谁又骗了谁，“塔台”没时间去当判官。只关注当下的放松，不纠结过去的恩怨。
		2️⃣ 不打工：请不要把责任无限上纲，“塔台”不是帮你打工的人。机器人卡了、坏了，或是群炸了，没有责任马上修好。
		3️⃣ 不解释：群风自由，没有群规限制，但一旦违反核心价值观，塔台踢了就踢了，不解释了。

		如果你能接受这些约定，那么，欢迎加入<u>镇泰飞机场</u>。

	""").strip()


def _airport_access_keyboard() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(
		inline_keyboard=[[
			InlineKeyboardButton(
				text="✈️ 申请进入机场",
				callback_data="airport:access:request",
			)
		]],
	)


def _airport_quiz_text(question_index: int) -> str:
	question, _, _ = AIRPORT_QUIZ_QUESTIONS[question_index]
	return (
		f"📝 机场入场考试（{question_index + 1}/{len(AIRPORT_QUIZ_QUESTIONS)}）\n\n"
		f"{question}\n\n"
		"请选择一个答案。"
	)


def _airport_quiz_keyboard(question_index: int) -> InlineKeyboardMarkup:
	_, options, _ = AIRPORT_QUIZ_QUESTIONS[question_index]
	return InlineKeyboardMarkup(
		inline_keyboard=[
			[
				InlineKeyboardButton(
					text=option,
					callback_data=f"airport:quiz:{question_index}:{option_index}",
				)
			]
			for option_index, option in enumerate(options)
		],
	)


async def _get_or_create_airport_invite_link() -> str:
	if ENCODED_FORWARD_CHAT_ID == 0:
		raise RuntimeError("镇泰飞机场尚未配置")

	async with AIRPORT_INVITE_LINK_LOCK:
		stored_link = shared_invite_link_store.get(AIRPORT_INVITE_LINK_KEY)
		if stored_link and stored_link.chat_id != ENCODED_FORWARD_CHAT_ID:
			shared_invite_link_store.delete(AIRPORT_INVITE_LINK_KEY)
			stored_link = None

		if stored_link:
			try:
				validated_link = await bot.edit_chat_invite_link(
					chat_id=ENCODED_FORWARD_CHAT_ID,
					invite_link=stored_link.invite_link,
					name=AIRPORT_INVITE_LINK_NAME,
					creates_join_request=True,
				)
			except TelegramBadRequest as exc:
				print(
					f"[AIRPORT_INVITE] stored link is invalid, replacing it: {exc}",
					flush=True,
				)
				shared_invite_link_store.delete(AIRPORT_INVITE_LINK_KEY)
			except Exception as exc:
				print(
					f"[AIRPORT_INVITE] validation unavailable, using stored link: {exc}",
					flush=True,
				)
				return stored_link.invite_link
			else:
				if not bool(validated_link.is_revoked):
					shared_invite_link_store.save(
						AIRPORT_INVITE_LINK_KEY,
						ENCODED_FORWARD_CHAT_ID,
						str(validated_link.invite_link),
						AIRPORT_INVITE_LINK_NAME,
						created_at=stored_link.created_at,
					)
					return str(validated_link.invite_link)
				shared_invite_link_store.delete(AIRPORT_INVITE_LINK_KEY)

		invite = await bot.create_chat_invite_link(
			chat_id=ENCODED_FORWARD_CHAT_ID,
			name=AIRPORT_INVITE_LINK_NAME,
			creates_join_request=True,
		)
		invite_link = str(invite.invite_link)
		try:
			shared_invite_link_store.save(
				AIRPORT_INVITE_LINK_KEY,
				ENCODED_FORWARD_CHAT_ID,
				invite_link,
				AIRPORT_INVITE_LINK_NAME,
			)
		except Exception:
			try:
				await bot.revoke_chat_invite_link(
					chat_id=ENCODED_FORWARD_CHAT_ID,
					invite_link=invite_link,
				)
			except Exception as revoke_exc:
				print(
					f"[AIRPORT_INVITE] rollback revoke failed: {revoke_exc}",
					flush=True,
				)
			raise
		return invite_link


async def _send_airport_join_request_invite(user_id: int, request_plant_channel: int = 0) -> None:
	if MESSAGE_REWARD_CHAT_ID == 0 or ENCODED_FORWARD_CHAT_ID == 0:
		raise RuntimeError("航站大厅尚未配置")

	status = await bot.get_chat_member(chat_id=MESSAGE_REWARD_CHAT_ID, user_id=user_id)
	airport_invitation = ""
	create_chat_id = 0
	chat_title = ""
	is_current_member = False

	if request_plant_channel <= 0:
		is_current_member = (
			status.status in ("member", "administrator", "creator")
			or (
				status.status == "restricted"
				and status.is_member is True
			)
		)

	if request_plant_channel ==1 or is_current_member:
		create_chat_id = ENCODED_FORWARD_CHAT_ID
		chat_title = "🛫 镇泰飞机场 "

		airport_invitation = (
			"亲爱的旅客，欢迎抵达「镇泰飞机场」航站大厅。\n\n"
			"进入航站楼后，请先向候机区的其他旅客发言问好。"
			"大厅内的航班信息板将显示目前开放登机、可以起飞的航班。\n\n"
			"移动端旅客可点击「镇泰飞机场」字样查看航班；"
			"桌面端旅客可点击旁边的小箭头，选择您准备搭乘的航班并前往对应登机口。\n\n"
			"办理登机前，请先加入「镇泰机场」频道，以完成登机资格验证，"
			"确保您能够顺利登机起飞。\n\n"
			"祝您候机愉快，航程顺利。"
		)


	else:
		create_chat_id = MESSAGE_REWARD_CHAT_ID
		airport_invitation = (
			"亲爱的旅客，诚邀您进入「镇泰飞机场航站大厅」。\n\n"
			"航站大厅是所有旅客起飞前必须加入的候机区域，"
			"您可以在这里查看航班动态、办理登机手续，并与其他旅客交流。\n\n"
			"在大厅内参与交流，还可延长飞行通行证有效期限。"
		)
		chat_title = "🏢 航站大厅 "

	invite_link = ""
	invite_expire_timestamp = 0
	is_shared_airport_invite = create_chat_id == ENCODED_FORWARD_CHAT_ID
	if is_shared_airport_invite:
		invite_link = await _get_or_create_airport_invite_link()
	elif create_chat_id == MESSAGE_REWARD_CHAT_ID:
		remembered_invite = PENDING_AIRPORT_JOIN_INVITES.get(user_id)
		if remembered_invite is not None:
			remembered_link, remembered_expire_timestamp = remembered_invite
			now_timestamp = int(datetime.now().timestamp())
			if (
				(remembered_expire_timestamp <= 0 or remembered_expire_timestamp > now_timestamp)
				and remembered_link not in USED_PAID_INVITES
			):
				invite_link = remembered_link
				invite_expire_timestamp = remembered_expire_timestamp
			else:
				PENDING_AIRPORT_JOIN_INVITES.pop(user_id, None)

	if not invite_link:
		invite_expire_date = datetime.now(timezone.utc) + timedelta(minutes=5)
		invite = await bot.create_chat_invite_link(
			chat_id=create_chat_id,
			name=f"airport-access-{user_id}",
			expire_date=invite_expire_date,
			creates_join_request=True,
		)
		invite_link = str(invite.invite_link)
		invite_expire_timestamp = int(invite_expire_date.timestamp())

	remaining_invite_seconds = max(
		0,
		invite_expire_timestamp - int(datetime.now().timestamp()),
	) if invite_expire_timestamp > 0 else 0
	invite_deadline_text = (
		f"请在 {_format_duration(remaining_invite_seconds)} 内送出入场审核申请。"
		if remaining_invite_seconds > 0
		else "请使用此连结送出入场审核申请。"
	)

	invite_description = (
		"机场审核邀请连结"
		if is_shared_airport_invite
		else "你的专属邀请连结"
	)
	await bot.send_message(
		chat_id=user_id,
		text=f"✅ {airport_invitation}\n\n{invite_description}已准备完成，{invite_deadline_text}",
		reply_markup=InlineKeyboardMarkup(
			inline_keyboard=[[

				InlineKeyboardButton(
					text=f"{chat_title}🔗",
					url=invite_link,
				)
			]]
		),
	)


@dp.message(F.chat.type == "private", Command("rule"))
async def cmd_rule(message: Message) -> None:
	upload_extend_text = minutes_to_day_hour(MEDIA_UPLOAD_EXTEND_MINUTES)[0]
	view_cost_text = minutes_to_day_hour(MEDIA_VIEW_COST_MINUTES)[0]
	message_extend_text = minutes_to_day_hour(MESSAGE_EXTEND_MINUTES)[0]
	max_duration_text = minutes_to_day_hour(MAX_VALID_DURATION_MINUTES)[0]

	await message.reply(
		"📋 镇泰塔台当前规则\n\n"
		"<i>飞行通行证期限是镇泰飞机场查看媒体的有效时间，通行证有效时间可以通过分享媒体、群组发言来延长；请求媒体会消耗通行证有效时间。</i>\n\n"
		"1️⃣ 分享媒体奖励\n"
		f"每成功分享一个媒体，增加 {upload_extend_text}。\n\n"
		"2️⃣ 请求媒体消耗(飞机场)\n"
		f"每请求一个媒体，消耗 {view_cost_text}。\n\n"
		"3️⃣ 群组(航站大厅)发言奖励\n"
		f"符合条件的一次群组发言，增加 {message_extend_text}，一分钟只采计一次。\n\n"
		"4️⃣ 通行证期限上限\n"
		f"飞行通行证最多保留 {max_duration_text}，可重覆扩展效期；效期超过上限就不会继续累加，低于效期即可再扩展。\n",
		parse_mode="HTML",
	)


@dp.message(F.chat.type == "private", Command("about"))
@dp.message(F.chat.type == "private", Command("airport_access_request"))
async def cmd_airport_access_request(message: Message) -> None:
	await message.reply(
		_airport_access_text(),
		parse_mode="HTML",
		reply_markup=_airport_access_keyboard(),
	)


@dp.message(F.chat.type == "private", Command("start"))
async def cmd_start(message: Message, command: CommandObject) -> None:
	if (command.args or "").strip():
		try:
			await message.delete()
		except Exception as exc:
			print(f"[START] failed to delete parameterized command: {exc}", flush=True)
		return

	await cmd_airport_access_request(message)


@dp.callback_query(F.data == "airport:access:request")
async def on_airport_access_request(callback: CallbackQuery) -> None:
	user_id = int(callback.from_user.id)
	now_timestamp = int(datetime.now().timestamp())
	user_expire = user_expire_cache.get(user_id)
	remaining_seconds = max(
		0,
		(user_expire.expire_timestamp if user_expire else 0) - now_timestamp,
	)

	if remaining_seconds <= 2 * 24 * 60 * 60:
		text = (
			"❌ 入场审核未通过：\n飞行通行证有效时间需要超过 2 天。\n"
			"请先上传 10 个「正太」媒体资源 ( 给镇泰塔台机器人 )，再重新申请。\n"
			"\n"
			"‼️ 不同系列放在同批上传，将被拉黑，请分批上传。\n"
		)

		await callback.answer(
			text,
			show_alert=True,
			cache_time=5,
		)
		return

	if MESSAGE_REWARD_CHAT_ID == 0:
		await callback.answer("机场群组尚未配置，请联系塔台", show_alert=True, cache_time=0)
		return

	lock = AIRPORT_QUIZ_LOCKS.setdefault(user_id, asyncio.Lock())
	async with lock:
		now_timestamp = int(datetime.now().timestamp())
		retry_at = AIRPORT_QUIZ_RETRY_AT.get(user_id, 0)
		if retry_at > now_timestamp:
			await callback.answer(
				f"答题锁定中，请在 {_format_duration(retry_at - now_timestamp)} 后重新申请。",
				show_alert=True,
				cache_time=0,
			)
			return
		AIRPORT_QUIZ_RETRY_AT.pop(user_id, None)

		if AIRPORT_QUIZ_PASSED_UNTIL.get(user_id, 0) > now_timestamp:
			try:
				await _send_airport_join_request_invite(user_id)
			except Exception as exc:
				print(f"[AIRPORT_ACCESS] invite creation failed: {exc}", flush=True)
				await callback.answer(
					"考试已通过，但暂时无法建立机场邀请，请稍后重试。",
					show_alert=True,
					cache_time=0,
				)
				return
			await callback.answer("审核邀请已重新发送", cache_time=0)
			return

		if user_id in AIRPORT_QUIZ_PROGRESS:
			await callback.answer("考试正在进行，请完成目前的题目。", show_alert=True, cache_time=0)
			return

		AIRPORT_QUIZ_PROGRESS[user_id] = 0
		try:
			await bot.send_message(
				chat_id=user_id,
				text=_airport_quiz_text(0),
				reply_markup=_airport_quiz_keyboard(0),
			)
		except Exception as exc:
			AIRPORT_QUIZ_PROGRESS.pop(user_id, None)
			print(f"[AIRPORT_ACCESS] quiz delivery failed: {exc}", flush=True)
			await callback.answer(
				"暂时无法发送考试题目，请稍后重试。",
				show_alert=True,
				cache_time=0,
			)
			return

	await callback.answer(
		f"机场入场考试已发送，请完成 {len(AIRPORT_QUIZ_QUESTIONS)} 道单选题。",
		cache_time=0,
	)


@dp.callback_query(F.data.startswith("airport:quiz:"))
async def on_airport_quiz_answer(callback: CallbackQuery) -> None:
	if not callback.message or callback.message.chat.type != "private":
		await callback.answer("考试仅能在机器人私信中进行", show_alert=True, cache_time=0)
		return

	user_id = int(callback.from_user.id)
	try:
		_, _, question_text, option_text = str(callback.data).split(":", 3)
		question_index = int(question_text)
		option_index = int(option_text)
	except (TypeError, ValueError):
		await callback.answer("无效的考试选项", show_alert=True, cache_time=0)
		return

	lock = AIRPORT_QUIZ_LOCKS.setdefault(user_id, asyncio.Lock())
	async with lock:
		current_question = AIRPORT_QUIZ_PROGRESS.get(user_id)
		if current_question is None:
			await callback.answer("本次考试已结束，请重新申请。", show_alert=True, cache_time=0)
			return
		if question_index != current_question or not 0 <= question_index < len(AIRPORT_QUIZ_QUESTIONS):
			await callback.answer("题目已经更新，请回答目前显示的题目。", show_alert=True, cache_time=0)
			return

		_, options, correct_option = AIRPORT_QUIZ_QUESTIONS[question_index]
		if not 0 <= option_index < len(options):
			await callback.answer("无效的考试选项", show_alert=True, cache_time=0)
			return

		if option_index != correct_option:
			AIRPORT_QUIZ_PROGRESS.pop(user_id, None)
			AIRPORT_QUIZ_PASSED_UNTIL.pop(user_id, None)
			AIRPORT_QUIZ_RETRY_AT[user_id] = (
				int(datetime.now().timestamp()) + AIRPORT_QUIZ_RETRY_SECONDS
			)
			await callback.message.edit_text(
				"❌ 回答错误，本次考试未通过。\n\n"
				"请重新阅读机场核心精神，30 分钟后再申请答题。"
			)
			await callback.answer("回答错误，30 分钟后才能重新申请。", show_alert=True, cache_time=0)
			return

		next_question = question_index + 1
		if next_question < len(AIRPORT_QUIZ_QUESTIONS):
			AIRPORT_QUIZ_PROGRESS[user_id] = next_question
			await callback.message.edit_text(
				_airport_quiz_text(next_question),
				reply_markup=_airport_quiz_keyboard(next_question),
			)
			await callback.answer("回答正确，进入下一题。", cache_time=0)
			return

		AIRPORT_QUIZ_PROGRESS.pop(user_id, None)
		AIRPORT_QUIZ_PASSED_UNTIL[user_id] = (
			int(datetime.now().timestamp()) + AIRPORT_QUIZ_PASS_SECONDS
		)
		await callback.message.edit_text(
			f"✅ {len(AIRPORT_QUIZ_QUESTIONS)} 道题目全部答对，"
			"机场核心精神考试通过。"
		)
		await callback.answer("考试通过，正在建立审核邀请。", cache_time=0)
		try:
			await _send_airport_join_request_invite(user_id)
		except Exception as exc:
			print(f"[AIRPORT_ACCESS] invite creation failed after quiz: {exc}", flush=True)
			await bot.send_message(
				chat_id=user_id,
				text="考试已经通过，但暂时无法建立邀请；请稍后再次点击申请进入机场。",
				reply_markup=_airport_access_keyboard(),
			)


@dataclass(slots=True)
class AirportJoinContext:
	request: ChatJoinRequest
	user_id: int
	chat_id: int
	is_paid_invite: bool = False
	invite_link: str = ""
	invite_expire_timestamp: int = 0
	inviter_user_id: int = 0


@dataclass(frozen=True, slots=True)
class JoinRejection:
	code: str
	reason: str


def _build_join_context(join_request: ChatJoinRequest) -> AirportJoinContext:
	chat_id = int(join_request.chat.id)
	invite = join_request.invite_link
	invite_expire_date = getattr(invite, "expire_date", None) if invite else None
	invite_expire_timestamp = (
		int(invite_expire_date.timestamp()) if invite_expire_date else 0
	)
	inviter_user_id = (
		_parse_paid_invite_name(invite.name)
		if invite is not None and chat_id == MESSAGE_REWARD_CHAT_ID
		else None
	)
	return AirportJoinContext(
		request=join_request,
		user_id=int(join_request.from_user.id),
		chat_id=chat_id,
		is_paid_invite=inviter_user_id is not None,
		invite_link=str(invite.invite_link) if invite is not None else "",
		invite_expire_timestamp=invite_expire_timestamp,
		inviter_user_id=inviter_user_id or 0,
	)


async def _is_member_of_chat(chat_id: int, user_id: int) -> bool:
	status = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
	return _is_current_chat_member(status)


async def _get_join_rejection_reason(
	context: AirportJoinContext,
) -> JoinRejection | None:
	if blacklist_store.is_blocked(context.user_id):
		return JoinRejection("blacklisted", "你目前无法申请进入机场。")

	now_timestamp = int(datetime.now().timestamp())
	user_expire = user_expire_cache.get(context.user_id)
	remaining_seconds = max(
		0,
		(user_expire.expire_timestamp if user_expire else 0) - now_timestamp,
	)
	if remaining_seconds <= 2 * 24 * 60 * 60:
		text = (
			"❌ 入场审核未通过：\n飞行通行证有效时间需要超过 2 天。\n"
			"请先上传 10 个「正太」媒体资源 ( 给镇泰塔台机器人 )，再重新申请。\n"
			"\n"
			"‼️ 不同系列放在同批上传，将被拉黑，请分批上传。\n"
		)


		return JoinRejection("insufficient_time", text)

	if AIRPORT_QUIZ_PASSED_UNTIL.get(context.user_id, 0) <= now_timestamp:
		return JoinRejection(
			"quiz_required",
			(
				f"尚未完成 {len(AIRPORT_QUIZ_QUESTIONS)} 道机场核心精神单选题，"
				"请从申请按钮重新开始考试。"
			),
		)

	if context.chat_id == ENCODED_FORWARD_CHAT_ID:
		if not await _is_member_of_chat(
			MESSAGE_REWARD_CHAT_ID,
			context.user_id,
		):
			return JoinRejection(
				"lobby_required",
				"请先加入航站大厅群组，再申请加入飞机场群组。",
			)

	return None


async def _reject_join_request(
	context: AirportJoinContext,
	reason: str,
	*,
	include_access_help: bool = True,
) -> None:
	try:
		await bot.send_message(
			chat_id=context.request.user_chat_id,
			text=f"{_airport_access_text()}",
			parse_mode="HTML" if include_access_help else None
		)


		text = f"❌ 入场审核未通过：{reason}"
		if context.is_paid_invite and include_access_help:
			text += "\n\n付费邀请不会免除机场资格要求，本次申请不会占用此邀请连结。"
		# if include_access_help:
		# 	text += f"\n\n{_airport_access_text()}"



		await bot.send_message(
			chat_id=context.request.user_chat_id,
			text=text,
			parse_mode="HTML" if include_access_help else None,
			reply_markup=_airport_access_keyboard() if include_access_help else None,
		)
	except Exception as exc:
		print(f"[AIRPORT_ACCESS] rejection notice failed: {exc}", flush=True)

	try:
		await bot.decline_chat_join_request(
			chat_id=context.chat_id,
			user_id=context.user_id,
		)
	except Exception as exc:
		print(f"[AIRPORT_ACCESS] join rejection failed: {exc}", flush=True)


async def _notify_join_retry(context: AirportJoinContext) -> None:
	try:
		await bot.send_message(
			chat_id=context.request.user_chat_id,
			text="Telegram 暂时无法完成入场审核，请稍后使用原连结重试。",
		)
	except Exception as exc:
		print(f"[AIRPORT_ACCESS] retry notice failed: {exc}", flush=True)


def _remember_failed_join_invite(context: AirportJoinContext) -> None:
	if context.chat_id != MESSAGE_REWARD_CHAT_ID or not context.invite_link:
		return
	now_timestamp = int(datetime.now().timestamp())
	if (
		context.invite_expire_timestamp > 0
		and context.invite_expire_timestamp <= now_timestamp
	):
		return
	PENDING_AIRPORT_JOIN_INVITES[context.user_id] = (
		context.invite_link,
		context.invite_expire_timestamp,
	)


async def _approve_join_request(context: AirportJoinContext) -> bool:
	try:
		await bot.approve_chat_join_request(
			chat_id=context.chat_id,
			user_id=context.user_id,
		)
		return True
	except Exception as exc:
		print(
			f"[AIRPORT_ACCESS] approval failed for user {context.user_id}, "
			f"chat {context.chat_id}: {exc}",
			flush=True,
		)
		return False


async def _consume_paid_invite(context: AirportJoinContext) -> None:
	USED_PAID_INVITES[context.invite_link] = int(datetime.now().timestamp())
	try:
		await bot.revoke_chat_invite_link(
			chat_id=context.chat_id,
			invite_link=context.invite_link,
		)
	except Exception as exc:
		print(
			f"[PAID_INVITE] revoke failed after approval for link "
			f"{context.invite_link}, applicant {context.user_id}, "
			f"inviter {context.inviter_user_id}: {exc}",
			flush=True,
		)


async def _reward_paid_invite_creator(context: AirportJoinContext) -> None:
	if not context.is_paid_invite:
		return
	if context.user_id == context.inviter_user_id:
		return

	inviter_lock = TAKEOFF_USER_LOCKS.setdefault(
		context.inviter_user_id,
		asyncio.Lock(),
	)
	async with inviter_lock:
		now_timestamp = int(datetime.now().timestamp())
		previous_user = user_expire_cache.get(context.inviter_user_id)
		previous_expire_timestamp = (
			previous_user.expire_timestamp if previous_user else 0
		)
		base_timestamp = max(now_timestamp, previous_expire_timestamp)
		updated_user = user_expire_cache.extend_minutes(
			context.inviter_user_id,
			PAID_INVITE_REWARD_MINUTES,
		)
		actual_added_seconds = max(
			0,
			updated_user.expire_timestamp - base_timestamp,
		)
		remaining_seconds = max(
			0,
			updated_user.expire_timestamp - int(datetime.now().timestamp()),
		)

	print(
		f"[PAID_INVITE] rewarded inviter {context.inviter_user_id} "
		f"{actual_added_seconds} seconds for applicant {context.user_id}",
		flush=True,
	)
	try:
		if actual_added_seconds > 0:
			text = (
				"🎉 推荐成功\n\n"
				"你建立的单人邀请已有一位旅客通过审核。\n"
				"飞行通行证奖励：2 天。\n"
				f"本次实际增加：{_format_duration(actual_added_seconds)}。\n"
				f"当前剩余时间：{_format_duration(remaining_seconds)}。"
			)
		else:
			text = (
				"🎉 推荐成功\n\n"
				"你建立的单人邀请已有一位旅客通过审核。\n"
				"由于飞行通行证已达到 3 天上限，本次未再增加期限。"
			)
		await bot.send_message(chat_id=context.inviter_user_id, text=text)
	except Exception as exc:
		print(
			f"[PAID_INVITE] reward notice failed for inviter "
			f"{context.inviter_user_id}: {exc}",
			flush=True,
		)


async def _send_airport_welcome(user_id: int) -> None:
	welcome_notice = (
		"亲爱的旅客，欢迎加入「镇泰飞机场」。\n\n"
		"接下来，您可以通过以下设施开启旅程：\n\n"
		"🗼 使用「镇泰塔台」机器人提交与分享资源；\n"
		"🏢 前往「航站大厅」与其他旅客交流发言；\n"
		"🛫 进入「镇泰飞机场」频道搭乘航班，"
		"选择您想要搭乘并起飞的班机。\n\n"
		"各项设施已准备就绪，祝您航程愉快。"
	)
	await bot.send_message(chat_id=user_id, text=f"✅ {welcome_notice}")


async def _after_join_approved(context: AirportJoinContext) -> None:
	if context.is_paid_invite:
		await _consume_paid_invite(context)
		try:
			await _reward_paid_invite_creator(context)
		except Exception as exc:
			print(
				f"[PAID_INVITE] creator reward failed for inviter "
				f"{context.inviter_user_id}: {exc}",
				flush=True,
			)

	if context.chat_id == MESSAGE_REWARD_CHAT_ID:
		PENDING_AIRPORT_JOIN_INVITES.pop(context.user_id, None)
		try:
			await _send_airport_join_request_invite(
				context.user_id,
				request_plant_channel=1,
			)
		except Exception as exc:
			print(
				f"[AIRPORT_ACCESS] next-stage invite failed for user "
				f"{context.user_id}: {exc}",
				flush=True,
			)
			try:
				await bot.send_message(
					chat_id=context.request.user_chat_id,
					text=(
						"✅ 已通过审核并加入航站大厅。\n"
						"暂时无法建立机场频道邀请，请稍后重新申请进入机场。"
					),
				)
			except Exception:
				pass
		return

	if context.chat_id == ENCODED_FORWARD_CHAT_ID:
		AIRPORT_QUIZ_PASSED_UNTIL.pop(context.user_id, None)
		try:
			await _send_airport_welcome(context.user_id)
		except Exception as exc:
			print(
				f"[AIRPORT_ACCESS] welcome notice failed for user "
				f"{context.user_id}: {exc}",
				flush=True,
			)


async def _process_join_request(context: AirportJoinContext) -> None:
	if (
		context.is_paid_invite
		and context.invite_link in USED_PAID_INVITES
	):
		remembered_invite = PENDING_AIRPORT_JOIN_INVITES.get(context.user_id)
		if remembered_invite and remembered_invite[0] == context.invite_link:
			PENDING_AIRPORT_JOIN_INVITES.pop(context.user_id, None)
		await _reject_join_request(
			context,
			"此单人邀请已经由其他申请者使用，请向邀请人索取新的连结。",
			include_access_help=False,
		)
		return

	try:
		rejection = await _get_join_rejection_reason(context)
	except Exception as exc:
		print(
			f"[AIRPORT_ACCESS] eligibility check failed for user "
			f"{context.user_id}: {exc}",
			flush=True,
		)
		await _notify_join_retry(context)
		return

	if rejection:
		if rejection.code in {"insufficient_time", "quiz_required"}:
			_remember_failed_join_invite(context)
		await _reject_join_request(context, rejection.reason)
		return

	if not await _approve_join_request(context):
		await _notify_join_retry(context)
		return

	await _after_join_approved(context)


@dp.chat_join_request(F.chat.id.in_({MESSAGE_REWARD_CHAT_ID, ENCODED_FORWARD_CHAT_ID}))
async def on_airport_join_request(join_request: ChatJoinRequest) -> None:
	context = _build_join_context(join_request)
	if context.is_paid_invite:
		_prune_used_paid_invites()
		lock = PAID_INVITE_LOCKS.setdefault(context.invite_link, asyncio.Lock())
		async with lock:
			await _process_join_request(context)
		return

	await _process_join_request(context)


async def _send_lobby_welcome(user: User) -> None:
	display_name = escape(str(user.full_name or "新旅客"))
	mention = f'<a href="tg://user?id={int(user.id)}">{display_name}</a>'
	welcome_text = (
		f"🎉 欢迎抵达航站大厅，{mention}！\n\n"
		"🏢 航站大厅(本群)：与其他旅客交流，发言可延长飞行通行证。\n"
		"🗼 镇泰塔台：提交、分享资源与邀请他人。\n"
		"🛫 镇泰飞机场：查看航班并获取资源。\n\n"
		"请先和其他旅客进行有内容的交流。问候语、刷屏或为了取得时数而发送的无意义内容不会获得奖励。\n\n"
		"祝你候机愉快，航程顺利。"
	)

	airport_url = await _get_or_create_airport_invite_link()

	tower_url = (
		f"https://t.me/{bot_name}"
		if bot_name
		else "https://t.me/ztTowerRobot"
	)
	welcome_keyboard = InlineKeyboardMarkup(
		inline_keyboard=[[
			InlineKeyboardButton(text="🛫 飞机场", url=airport_url),
			InlineKeyboardButton(text="🗼 塔台", url=tower_url),
			InlineKeyboardButton(text="🪧 指路牌", url="https://t.me/ztTowerRobot")
		]],
	)

	if TRADE_IMAGE_PATHS[0].is_file():
		await bot.send_photo(
			chat_id=MESSAGE_REWARD_CHAT_ID,
			photo=FSInputFile(TRADE_IMAGE_PATHS[0]),
			caption=welcome_text,
			parse_mode="HTML",
			reply_markup=welcome_keyboard,
		)
	else:
		await bot.send_message(
			chat_id=MESSAGE_REWARD_CHAT_ID,
			text=welcome_text,
			parse_mode="HTML",
			reply_markup=welcome_keyboard,
		)


@dp.chat_member(F.chat.id.in_({MESSAGE_REWARD_CHAT_ID, ENCODED_FORWARD_CHAT_ID}))
async def on_airport_member_updated(update: ChatMemberUpdated) -> None:
	target_user = update.new_chat_member.user
	target_user_id = int(target_user.id)
	actor_user_id = int(update.from_user.id)

	if target_user.is_bot:
		return

	was_member = _is_current_chat_member(update.old_chat_member)
	is_member = _is_current_chat_member(update.new_chat_member)
	is_new_lobby_member = (
		int(update.chat.id) == MESSAGE_REWARD_CHAT_ID
		and not was_member
		and is_member
	)
	if is_new_lobby_member:
		if (
			target_user_id not in ADMIN_USER_IDS
			and blacklist_store.is_blocked(target_user_id)
		):
			try:
				await _ban_user(
					user_id=target_user_id,
					reason="黑名单用户异常重新加入航站大厅",
					created_by=int(bot.id),
				)
			except Exception as exc:
				print(
					f"[LOBBY_WELCOME] failed to remove blacklisted user "
					f"{target_user_id}: {exc}",
					flush=True,
				)
			return

		try:
			await _send_lobby_welcome(target_user)
		except Exception as exc:
			print(
				f"[LOBBY_WELCOME] notice failed for user {target_user_id}: {exc}",
				flush=True,
			)
		return

	if target_user_id in ADMIN_USER_IDS:
		return
	if blacklist_store.is_blocked(target_user_id):
		return
	if not was_member:
		return
	if update.new_chat_member.status != "left":
		return
	if actor_user_id != target_user_id:
		return

	chat_name = (
		"航站大厅"
		if int(update.chat.id) == MESSAGE_REWARD_CHAT_ID
		else "镇泰飞机场"
	)
	reason = f"主动离开{chat_name}，系统自动加入黑名单"
	PENDING_AIRPORT_JOIN_INVITES.pop(target_user_id, None)
	try:
		_, group_ban_error = await _ban_user(
			user_id=target_user_id,
			reason=reason,
			created_by=int(bot.id),
		)
	except Exception as exc:
		print(
			f"[AUTO_BLACKLIST] failed for user {target_user_id}, "
			f"chat {update.chat.id}: {exc}",
			flush=True,
		)
		return

	print(
		f"[AUTO_BLACKLIST] user {target_user_id} voluntarily left "
		f"{chat_name} ({update.chat.id}); ban error: {group_ban_error or 'none'}",
		flush=True,
	)
	try:
		await bot.send_message(
			chat_id=target_user_id,
			text=(
				"🚫 已列入黑名单\n\n"
				f"系统检测到你主动离开{chat_name}。\n"
				"根据机场规则，主动离群将自动失去再次申请资格。"
			),
		)
	except Exception as exc:
		print(
			f"[AUTO_BLACKLIST] notice failed for user {target_user_id}: {exc}",
			flush=True,
		)


def _extract_automatic_forward_source(message: Message) -> tuple[int, int] | None:
	if not bool(message.is_automatic_forward):
		return None

	origin = message.forward_origin
	origin_chat = getattr(origin, "chat", None)
	origin_message_id = getattr(origin, "message_id", None)
	if origin_chat is not None and origin_message_id is not None:
		return int(origin_chat.id), int(origin_message_id)

	if message.forward_from_chat and message.forward_from_message_id:
		return (
			int(message.forward_from_chat.id),
			int(message.forward_from_message_id),
		)
	return None


@dp.message(
	F.chat.id == MESSAGE_REWARD_CHAT_ID,
	F.is_automatic_forward == True,
)
async def on_lobby_channel_auto_forward(message: Message) -> None:
	source_location = _extract_automatic_forward_source(message)
	if not source_location:
		return
	if source_location[0] != ENCODED_FORWARD_CHAT_ID:
		return

	discussion_location = (int(message.chat.id), int(message.message_id))
	async with BATCH_LOCATION_LOCK:
		updated = batch_store.update_discussion_location(
			source_location[0],
			source_location[1],
			discussion_location[0],
			discussion_location[1],
		)
		if not updated:
			PENDING_BATCH_DISCUSSION_LOCATIONS[source_location] = (
				discussion_location
			)
			PENDING_BATCH_DISCUSSION_LOCATIONS.move_to_end(source_location)
			while (
				len(PENDING_BATCH_DISCUSSION_LOCATIONS)
				> MAX_PENDING_BATCH_DISCUSSION_LOCATIONS
			):
				PENDING_BATCH_DISCUSSION_LOCATIONS.popitem(last=False)
			return

	print(
		f"[BATCH] discussion location updated "
		f"channel={source_location[0]}/{source_location[1]} "
		f"discussion={discussion_location[0]}/{discussion_location[1]}",
		flush=True,
	)


@dp.message(F.chat.id.in_({MESSAGE_REWARD_CHAT_ID, ENCODED_FORWARD_CHAT_ID}), F.text)
async def on_reward_group_message(message: Message) -> None:
	if not message.from_user or message.from_user.is_bot:
		return
	text = (message.text or "").strip()
	if not text or text.startswith("/"):
		return

	user_id = int(message.from_user.id)
	now_timestamp = int(datetime.now().timestamp())
	previous_user_expire = user_expire_cache.get(user_id)
	if (
		previous_user_expire
		and now_timestamp - previous_user_expire.group_message_timestamp < 60
	):
		# print(f"[MESSAGE_REWARD] user {user_id} message too frequent, skip reward -{now_timestamp - previous_user_expire.group_message_timestamp}", flush=True)
		return

	base_timestamp = max(
		now_timestamp,
		previous_user_expire.expire_timestamp if previous_user_expire else 0,
	)
	user_expire = user_expire_cache.extend_minutes(
		user_id,
		MESSAGE_EXTEND_MINUTES,
		group_message_timestamp=now_timestamp,
	)
	actual_added_minutes = max(
		0,
		(user_expire.expire_timestamp - base_timestamp) // 60,
	)
	print(
		f"[MESSAGE_REWARD] user {user_id} granted "
		f"{actual_added_minutes}/{MESSAGE_EXTEND_MINUTES} minutes",
		flush=True,
	)


@dp.message(
	F.chat.type == "private",
	F.document | F.photo | F.video | F.audio | F.voice | F.animation | F.sticker,
)
async def on_media(message: Message) -> None:
	if not message.from_user:
		return
	if blacklist_store.is_blocked(int(message.from_user.id)):
		return

	key = (message.chat.id, message.from_user.id)
	if USER_MEDIA_PENDING.get(key, 0) >= MAX_USER_PENDING:
		await _notify_media_limit(message, "发送速度过快，请稍后再试")
		return

	session = UPLOAD_SESSIONS.get(key)
	if session and int(session["accepted_count"]) >= MAX_BATCH_MEDIA:
		await _notify_media_limit(message, "每批最多上传 10 个媒体，多余媒体未加入")
		return

	try:
		file_type, file_id = _extract_media_info(message)
		file_unique_id = _extract_media_unique_id(message)
	except ValueError as exc:
		await message.reply(f"❌ 无法识别媒体: {exc}")
		return

	claimed = received_media_store.claim(
		file_unique_id=file_unique_id,
		file_id=file_id,
		file_type=file_type,
		user_id=int(message.from_user.id),
		source_chat_id=int(message.chat.id),
		source_message_id=int(message.message_id),
	)
	if not claimed:
		await _notify_media_limit(message, "此媒体已经收过，本批未计入")
		return

	if not session:
		session = {
			"items": [],
			"accepted_count": 0,
			"processed_count": 0,
			"panel_message_id": None,
		}
		UPLOAD_SESSIONS[key] = session

	try:
		MEDIA_QUEUE.put_nowait(message)
	except asyncio.QueueFull:
		received_media_store.release_pending(
			file_unique_id,
			message.chat.id,
			message.message_id,
		)
		if int(session["accepted_count"]) == 0:
			UPLOAD_SESSIONS.pop(key, None)
		await _notify_media_limit(message, "系统正在处理较多媒体，请稍后再试")
		return

	session["accepted_count"] = int(session["accepted_count"]) + 1
	USER_MEDIA_PENDING[key] = USER_MEDIA_PENDING.get(key, 0) + 1


@dp.callback_query(F.data.startswith("ta:b:"))
async def on_takeoff_admin_blacklist(callback: CallbackQuery) -> None:
	if int(callback.from_user.id) not in ADMIN_USER_IDS:
		await callback.answer("❌ 你没有权限执行此操作", show_alert=True)
		return
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return

	payload = str(callback.data or "").removeprefix("ta:b:")
	parts = payload.split(":")
	if len(parts) != 3:
		await callback.answer("消息位置参数无效", show_alert=True)
		return
	target_user_id = _parse_positive_user_id(parts[0])
	source_chat_text = parts[1]
	source_message_id = _parse_positive_user_id(parts[2])
	if (
		target_user_id is None
		or not source_chat_text.lstrip("-").isdigit()
		or int(source_chat_text) == 0
		or source_message_id is None
	):
		await callback.answer("上传者或消息位置参数无效", show_alert=True)
		return
	source_chat_id = int(source_chat_text)
	if target_user_id in ADMIN_USER_IDS:
		await callback.answer("❌ 不能封禁管理员", show_alert=True)
		return

	_, group_ban_error = await _ban_user(
		target_user_id,
		"管理员取件审核后拉黑",
		int(callback.from_user.id),
	)
	delete_error = ""
	try:
		await bot.delete_message(
			chat_id=source_chat_id,
			message_id=source_message_id,
		)
	except Exception as exc:
		delete_error = str(exc)
		print(
			f"[TAKEOFF_ADMIN] source message delete failed for "
			f"{source_chat_id}/{source_message_id}: {exc}",
			flush=True,
		)

	if not group_ban_error and not delete_error:
		try:
			await callback.message.edit_reply_markup(reply_markup=None)
		except Exception as exc:
			print(f"[TAKEOFF_ADMIN] keyboard cleanup failed: {exc}", flush=True)
		await callback.answer("已删除群消息并拉黑上传者", show_alert=True)
	elif group_ban_error and delete_error:
		await callback.answer(
			"已写入黑名单，但删除群消息和移出群组均失败，请查看日志",
			show_alert=True,
		)
	elif group_ban_error:
		await callback.answer(
			"已删除群消息并写入黑名单，但移出群组失败，请查看日志",
			show_alert=True,
		)
	else:
		await callback.answer(
			"已拉黑并移出上传者，但删除群消息失败，请查看日志",
			show_alert=True,
		)


@dp.callback_query(F.data.startswith("ta:d:"))
async def on_takeoff_admin_delete(callback: CallbackQuery) -> None:
	if int(callback.from_user.id) not in ADMIN_USER_IDS:
		await callback.answer("❌ 你没有权限执行此操作", show_alert=True)
		return
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return

	payload = str(callback.data or "").removeprefix("ta:d:")
	parts = payload.split(":")
	if len(parts) != 2:
		await callback.answer("消息位置参数无效", show_alert=True)
		return
	source_chat_text = parts[0]
	source_message_id = _parse_positive_user_id(parts[1])
	if (
		not source_chat_text.lstrip("-").isdigit()
		or int(source_chat_text) == 0
		or source_message_id is None
	):
		await callback.answer("消息位置参数无效", show_alert=True)
		return
	source_chat_id = int(source_chat_text)

	try:
		await bot.delete_message(
			chat_id=source_chat_id,
			message_id=source_message_id,
		)
	except Exception as exc:
		print(
			f"[TAKEOFF_ADMIN] source message delete failed for "
			f"{source_chat_id}/{source_message_id}: {exc}",
			flush=True,
		)
		await callback.answer("删除群消息失败，请查看日志", show_alert=True)
		return
	try:
		await callback.message.edit_reply_markup(reply_markup=None)
	except Exception as exc:
		print(f"[TAKEOFF_ADMIN] keyboard cleanup failed: {exc}", flush=True)
	await callback.answer("群消息已删除")


@dp.callback_query(F.data.startswith("takeoff:ban"))
async def on_takeoff_ban(callback: CallbackQuery) -> None:
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return

	entities = [
		*(getattr(callback.message, "entities", None) or []),
		*(getattr(callback.message, "caption_entities", None) or []),
	]

	for entity in entities:
		entity_type = getattr(entity.type, "value", entity.type)
		entity_url = str(entity.url or "")
		if entity_type != "text_link" or not entity_url.startswith("https://b.oy/"):
			continue

		try:
			parse_text = entity_url.removeprefix("https://b.oy/")
			token = UtfConverter.unicode_cjk_to_telegram(parse_text)
			parsed = UtfConverter.parse_file_token(token)
			owner_user_id = int(parsed["user_id"])
			requester_user_id = int(callback.from_user.id)
		except Exception as exc:
			await callback.answer(f"解析 Owner 失败: {exc}", show_alert=True)
			return

		if requester_user_id != owner_user_id:
			await callback.answer("❌ 你不是机长，无法停飞此班机", show_alert=True)
			return

		try:
			await callback.message.delete()
		except Exception as delete_exc:
			print(f"[TAKEOFF_BAN] delete failed: {delete_exc}", flush=True)
			try:
				await callback.message.edit_reply_markup(
					reply_markup=InlineKeyboardMarkup(
						inline_keyboard=[[
							InlineKeyboardButton(
								text="已停飞",
								callback_data="takeoff:grounded",
							)
						]]
					)
				)
			except Exception as edit_exc:
				await callback.answer(f"停飞失败: {edit_exc}", show_alert=True)
				return

		await callback.answer("机长已停飞此班机", show_alert=True)
		return

	print(f"消息中找不到有效的取件码链接=>{callback.message}")
	await callback.answer("消息中找不到有效的取件码链接", show_alert=True)


@dp.callback_query(F.data == "takeoff:grounded")
async def on_takeoff_grounded(callback: CallbackQuery) -> None:
	await callback.answer("机长已停飞此班机", show_alert=True)


def _extract_takeoff_code(message: Message) -> str | None:
	entities = [
		*(getattr(message, "entities", None) or []),
		*(getattr(message, "caption_entities", None) or []),
	]
	for entity in entities:
		entity_type = getattr(entity.type, "value", entity.type)
		entity_url = str(getattr(entity, "url", "") or "")
		if entity_type == "text_link" and entity_url.startswith("https://b.oy/"):
			parse_text = entity_url.removeprefix("https://b.oy/")
			if parse_text:
				return parse_text
	return None


def _ceil_div(value: int, divisor: int) -> int:
	if value <= 0:
		return 0
	return (value + divisor - 1) // divisor


def _get_first_takeoff_batch_id(send_result: dict[str, Any]) -> str | None:
	try:
		file_unique_ids: list[str] = []
		for sent_message in send_result.get("sent_media_messages", []):
			try:
				file_unique_ids.append(_extract_media_unique_id(sent_message))
			except ValueError as exc:
				print(
					f"[TAKEOFF] output media has no file_unique_id: {exc}",
					flush=True,
				)

		batch_ids_by_file = received_media_store.get_batch_ids(file_unique_ids)
		first_batch_id = next(
			(
				batch_id
				for file_unique_id in file_unique_ids
				if (batch_id := batch_ids_by_file.get(file_unique_id))
			),
			None,
		)
		missing_count = sum(
			1
			for file_unique_id in file_unique_ids
			if not batch_ids_by_file.get(file_unique_id)
		)
		print(
			f"[TAKEOFF] delivered batch_id={first_batch_id or 'None'} "
			f"media_count={len(file_unique_ids)} missing_batch_count={missing_count}",
			flush=True,
		)
		return first_batch_id
	except Exception as exc:
		print(f"[TAKEOFF] batch_id lookup failed: {exc}", flush=True)
		return None


@dp.callback_query(F.data.startswith("takeoff:batch:"))
async def on_takeoff_batch_id(callback: CallbackQuery) -> None:
	batch_id = str(callback.data or "").removeprefix("takeoff:batch:").strip()
	if not batch_id:
		await callback.answer("此飞行申请已失效", show_alert=True, cache_time=0)
		return

	tower_bot = f"{bot_name}" if bot_name else "ztTowerRobot"
	text = f"""
🎫 <code>{tower_bot}_{batch_id}</code>
<i>请发送至塔台 <code>{tower_bot}</code>，即可起飞 ✈️</i>。
"""
	try:
		await bot.send_message(
			chat_id=callback.from_user.id,
			text=text.strip(),
			parse_mode="HTML",
		)
	except Exception as exc:
		print(f"[TAKEOFF] batch_id message failed: {exc}", flush=True)
		await callback.answer("起飞许可编号发送失败，请稍后重试", show_alert=True)
		return
	await callback.answer(cache_time=0)


@dp.callback_query(F.data.startswith("takeoff:fly"))
async def on_takeoff(callback: CallbackQuery) -> None:
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return

	parse_text = _extract_takeoff_code(callback.message)
	if not parse_text:
		
		await callback.answer("消息中找不到有效的取件码链接", show_alert=True)
		return

	reader_user_id = int(callback.from_user.id)
	print("2439 reader_user_id = {reader_user_id}")

	is_admin  = False
	if reader_user_id in ADMIN_USER_IDS:
		is_admin = True

	try:
		token = UtfConverter.unicode_cjk_to_telegram(parse_text)
		parsed = UtfConverter.parse_file_token(token)
		parsed_items = list(parsed.get("items", []))
		requested_qty = len(parsed_items) if parsed_items else 1
		valid_until_dt = datetime.strptime(
			str(parsed["valid_until"]),
			"%Y%m%d%H%M%S",
		).replace(tzinfo=UTC8)
	except Exception as exc:
		print(f"[TAKEOFF] token parse failed: {exc}", flush=True)
		await callback.answer("❌ 无法解析此航班", show_alert=True, cache_time=0)
		return

	now = datetime.now(UTC8)
	if now > valid_until_dt and not is_admin:
		# overdue_text = _format_duration(int((now - valid_until_dt).total_seconds()))
		await callback.answer(
			text=f"❌ 此航班已过期 ( 超过有效时间 )",
			show_alert=True,
			cache_time=100000,
		)
		return

	requested_minutes = requested_qty * MEDIA_VIEW_COST_MINUTES
	user_lock = TAKEOFF_USER_LOCKS.setdefault(reader_user_id, asyncio.Lock())

	async with user_lock:
		now_timestamp = int(datetime.now().timestamp())
		user_expire = user_expire_cache.get(reader_user_id)
		if (
			not user_expire
			or now_timestamp - user_expire.group_message_timestamp > 24 * 60 * 60
		):

			await callback.answer(
				text=(
					"📢 航站广播\n\n"
					"为确保航站持续开放，避免因缺少互动而触发电报官方限制，请各位旅客先前往「航站大厅」参与发言交流，让航站保持良好运行。\n\n"
					"👍 你可以:\n"
					"回覆对已搭乘班机资源的体验与点评\n"
					"回应其他旅客的发言\n\n"
					"👎 不建议:\n"
					"发问候语\n"
					"述说需要发言\n"
				),
				parse_mode="HTML",
				show_alert=True,
			)

			return

		available_minutes = max(
			0,
			((user_expire.expire_timestamp if user_expire else 0) - now_timestamp) // 60,
		)

		if available_minutes < requested_minutes:
			missing_minutes = requested_minutes - available_minutes
			word_qty = _ceil_div(missing_minutes, MESSAGE_EXTEND_MINUTES)
			upload_qty = _ceil_div(missing_minutes, MEDIA_UPLOAD_EXTEND_MINUTES)
			required_until_text = _format_timestamp_utc8(now_timestamp + requested_minutes * 60)
			await callback.answer(
				text=(
					f"飞行通行证期限需要超过 {required_until_text}。\n"
					f"你还差 {minutes_to_day_hour(missing_minutes)[0]}，"
					f"你可以选择在大厅发言 {word_qty} 句 ( 1 分钟只计 1 句 )，或再分享到塔台 {upload_qty} 个资源。"
				),
				show_alert=True,
				cache_time=0,
			)
			return

		original_expire_timestamp = user_expire.expire_timestamp
		if user_expire_cache.consume_minutes(reader_user_id, requested_minutes) is None:
			await callback.answer("飞行通行证余额不足，请重新尝试", show_alert=True, cache_time=0)
			return

		try:
			send_result = await extract_encode(
				parse_text,
				callback.message,
				reader_user_id,
			)

			if not send_result.get("ok", False):
				user_expire_cache.update(reader_user_id, original_expire_timestamp)
				reason = send_result.get("reason", "unknown")
				if reason == "expired":
					overdue_text = _format_duration(int(send_result.get("overdue_seconds", 0)))
					answer_text = f"❌ 此 token 已过期\n已过期: {overdue_text}"
				elif reason == "flash_used":
					answer_text = "❌ 此闪读密文仅可读取一次"
				else:
					answer_text = "❌ 无法解析此 token"
				await callback.answer(answer_text, show_alert=True, cache_time=0)
				return




			batch_id = _get_first_takeoff_batch_id(send_result)

			skipped_qty = int(send_result.get("skipped_count", 0) or 0)
			delivered_qty = max(0, requested_qty - skipped_qty)
			delivered_minutes = delivered_qty * MEDIA_VIEW_COST_MINUTES
			if skipped_qty:
				user_expire_cache.extend_minutes(
					reader_user_id,
					skipped_qty * MEDIA_VIEW_COST_MINUTES,
				)

			requested_human_time = minutes_to_day_hour(delivered_minutes)[0]


			new_user_expire = user_expire_cache.get(reader_user_id)

			expire_text = _format_timestamp_utc8(new_user_expire.expire_timestamp)

			remaining_minutes = max(
				0,
				(new_user_expire.expire_timestamp - now_timestamp) // 60,
			)

			remaining_text, remaining_view_count = minutes_to_day_hour(remaining_minutes)

			notify_text = (
				f"✅ 获取 {delivered_qty} 个资源成功，本次消耗 {requested_human_time} 的有效时间。\n"
				f"🎫 当前飞行通行证到期时间为：{expire_text}。（ 相当于 {remaining_view_count} 个资源 ） \n\n"
				f"🎈 每获取一个媒体需要消耗  {MEDIA_VIEW_COST_MINUTES} 分钟的飞行通行证有效期。"
			)
			if skipped_qty:
				notify_text += (
					f"\n⚠️ 已跳过 {skipped_qty} 个失效或暂时不可用的资源，"
					"未扣除对应时间。"
				)

			notify_keyboard_rows: list[list[InlineKeyboardButton]] = []
			can_request_takeoff_clearance = (
				bool(batch_id)
				and str(parsed.get("valid_until", "")) == "99991231235959"
				and not bool(parsed.get("no_forward", False))
				and int(parsed.get("flash_seconds", 0) or 0) == 0
			)
			if can_request_takeoff_clearance:
				notify_keyboard_rows.append([
					InlineKeyboardButton(
						text="🎫 密文分享",
						callback_data=f"takeoff:batch:{batch_id}",
					),
				])

			if is_admin:
				uploader_id = int(parsed.get("user_id", 0) or 0)
				source_chat_id = int(callback.message.chat.id)
				source_message_id = int(callback.message.message_id)
				uploader_text = await get_user_hyperlink(
					bot,
					{"id": uploader_id},
					show_uid=True,
				)
				notify_text += f"\n👤 上传者：{uploader_text}"
				notify_keyboard_rows.extend(
					[
						[
							InlineKeyboardButton(
								text="🚫 删除消息并拉黑上传者",
								callback_data=(
									"ta:b:"
									f"{uploader_id}:{source_chat_id}:"
									f"{source_message_id}"
								),
							),
						],
						[
							InlineKeyboardButton(
								text="🗑 删除消息",
								callback_data=(
									"ta:d:"
									f"{source_chat_id}:{source_message_id}"
								),
							),
						],
					]
				)
			notify_markup = (
				InlineKeyboardMarkup(inline_keyboard=notify_keyboard_rows)
				if notify_keyboard_rows
				else None
			)

			await bot.send_message(
				chat_id=reader_user_id,
				text=notify_text,
				parse_mode="HTML",
				disable_web_page_preview=True,
				reply_markup=notify_markup,
			)


		except Exception as exc:
			user_expire_cache.update(reader_user_id, original_expire_timestamp)
			print(f"[TAKEOFF] media delivery failed: {exc}", flush=True)
			await callback.answer("❌ 媒体发送失败，请稍后重试", show_alert=True, cache_time=0)
			return



	chat_id = callback.message.chat.id
	message_id = callback.message.message_id
	try:
		takeoff_count = await _increment_takeoff_count(callback.message)
		print(f"{callback.message.chat.id}/{callback.message.message_id} takeoff count updated: {takeoff_count}", flush=True)

		if takeoff_count and (takeoff_count == 3 or takeoff_count == 9 or takeoff_count == 12):
			message_url = f"https://t.me/c/{str(chat_id).lstrip('-100')}/{message_id}"
			
			text =(
				f"📢 <b>航站广播：</b>目前已有 <code><b>{takeoff_count}</b></code> 位旅客搭乘 <b><a href=\"{message_url}\">ZT-{message_id}</a></b> 航班。\n"
				f"尚未登机的旅客，请尽速前往 <b><a href=\"{message_url}\">登机口</a></b> 办理登机手续。"
			)


			discussion_location = batch_store.get_discussion_location(
				int(chat_id),
				int(message_id),
			)
			reply_parameters: dict[str, int] = {}
			if (
				discussion_location
				and discussion_location[0] == MESSAGE_REWARD_CHAT_ID
				and discussion_location[1] is not None
			):
				reply_parameters["reply_to_message_id"] = discussion_location[1]

			await bot.send_message(
				chat_id=MESSAGE_REWARD_CHAT_ID,
				text=text.strip(),
				parse_mode="HTML",
				**reply_parameters,
			)

	except Exception as exc:
		print(f"[TAKEOFF] counter update failed: {exc}", flush=True)

	await callback.answer(
		url=f"https://t.me/{bot_name}?start=fly_{chat_id}_{message_id}",
		cache_time=0,
	)


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

		if (
			str(state.get("send_status", "idle")) == "sending"
			and group != "cancel"
		):
			await callback.answer(
				"资源正在送出，暂时不能修改设定",
				show_alert=True,
			)
			return


		if int(state.get("sent_revision", 0)) > 0 and group != "cancel":
			await callback.answer(
				"此批资源已经送出，设定已锁定，不能修改或再次送出",
				show_alert=True,
			)
			return

		if group == "send":
			await _handle_send_encoded(callback, state_key, state)
			return
		if group == "cancel":
			await _handle_cancel_encoded(callback, state_key, state)
			return
		if group == "fw":
			state["no_forward"] = value == "1"
		elif group == "an":
			state["anonymous"] = value == "1"
		elif group == "sp":
			state["if_spoiler"] = value == "1"
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
		state["revision"] = int(state.get("revision", 1)) + 1
		state["token"] = token
		state["encoded"] = encoded
		if str(state.get("send_status", "idle")) != "sending":
			state["send_status"] = "idle"
		markup = _build_controls_keyboard(state, encoded)
		await callback.message.edit_text(await _build_display(parsed, token, encoded), reply_markup=markup, parse_mode="HTML")
		await callback.answer("已更新密文")
	except Exception as exc:
		await callback.answer(f"更新失败: {exc}", show_alert=True)


async def extract_encode(parse_text: str, message: Message, receiver_id: int = None) -> dict[str, Any]:
	token = UtfConverter.unicode_cjk_to_telegram(parse_text)
	data = UtfConverter.parse_file_token(token)
	marked_flash_key: tuple[str, int] | None = None

	print(f"2693 receiver_id={receiver_id}")
	is_admin  = False
	if receiver_id in ADMIN_USER_IDS:
		is_admin = True

	valid_until_dt = datetime.strptime(
		str(data["valid_until"]),
		"%Y%m%d%H%M%S",
	).replace(tzinfo=UTC8)
	now = datetime.now(UTC8)
	_cleanup_used_flash_nonces(now)

	if now > valid_until_dt and not is_admin:
		overdue_seconds = int((now - valid_until_dt).total_seconds())
		overdue_text = _format_duration(overdue_seconds)
		await message.reply(
			"❌ 此 token 已过期\n"
			f"过期时间: {_format_datetime_utc8(valid_until_dt)}\n"
			f"已过期: {overdue_text}"
		)

		return {"ok": False, "reason": "expired", "overdue_seconds": overdue_seconds}

	flash_seconds = int(data.get("flash_seconds", 0))
	nonce_key = str(data.get("nonce", ""))
	if flash_seconds > 0:
		if receiver_id is not None:
			reader_user_id = int(receiver_id)
		elif message.from_user:
			reader_user_id = int(message.from_user.id)
		else:
			raise ValueError("无法确认闪读用户")
		if reader_user_id <= 0:
			raise ValueError("闪读用户 ID 无效")

		flash_key = (nonce_key, reader_user_id)
		expires_at = USED_FLASH_NONCES.get(flash_key)
		if expires_at and now < expires_at:
			# await message.reply("❌ 此闪读密文仅可读取一次")
			return {"ok": False, "reason": "flash_used"}
		if str(data.get("valid_until", "")) == "99991231235959":
			expires_at = now + timedelta(days=PERM_FLASH_NONCE_RETENTION_DAYS)
		else:
			expires_at = valid_until_dt
		USED_FLASH_NONCES[flash_key] = expires_at
		marked_flash_key = flash_key

	print(f"extract_encode: token={token}, data={data}, receiver_id={receiver_id}, flash_seconds={flash_seconds}, marked_flash_key={marked_flash_key}", flush=True)
	try:
		sent_media_messages, skipped_items = await _send_all_media(
			message,
			data,
			receiver_id=receiver_id,
		)
		if not sent_media_messages and skipped_items:
			raise ValueError("所有媒体的 file_id 均无效或暂时不可用")
	except Exception:
		if marked_flash_key:
			USED_FLASH_NONCES.pop(marked_flash_key, None)
		raise

	if flash_seconds > 0:
		for sent_media_message in sent_media_messages:
			asyncio.create_task(_delete_message_later(sent_media_message, flash_seconds))

	return {
		"ok": True,
		"sent_media_messages": sent_media_messages,
		"skipped_items": skipped_items,
		"skipped_count": len(skipped_items),
		"marked_flash_key": marked_flash_key,
	}

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

def _extract_takeoff_batch_id(text: str) -> str | None:
	normalized_text = str(text or "").strip()
	if not normalized_text:
		return None
	first_line = normalized_text.splitlines()[0]
	tower_bot_name = bot_name or "ztTowerRobot"
	match = re.fullmatch(
		rf"(?:🎫\s*)?{re.escape(tower_bot_name)}_([A-Za-z0-9_-]{{16}})",
		first_line.strip(),
		flags=re.IGNORECASE,
	)
	return match.group(1) if match else None


@dp.message(F.chat.type == "private", F.text)
async def on_text(message: Message) -> None:
	text = (message.text or "").strip()
	if not text:
		return

	batch_id = _extract_takeoff_batch_id(text)
	if batch_id:
		print(f"batch_id={batch_id}", flush=True)
		try:
			items = received_media_store.get_media_by_batch_id(batch_id)
		except Exception as exc:
			print(f"[TAKEOFF] batch media lookup failed: {exc}", flush=True)
			await message.reply("❌ 起飞许可查询失败，请稍后重试")
			return

		if not items:
			await message.reply("❌ 找不到此起飞许可对应的媒体")
			return

		data = {
			"items": items,
			"file_id": items[0]["file_id"],
			"file_type": items[0]["file_type"],
			"no_forward": False,
			"if_spoiler": False,
		}
		try:
			sent_messages, skipped_items = await _send_all_media(
				message,
				data,
				receiver_id=int(message.from_user.id),
			)
		except Exception as exc:
			print(f"[TAKEOFF] batch media delivery failed: {exc}", flush=True)
			await message.reply("❌ 此批次的媒体发送失败，请稍后重试")
			return

		if not sent_messages:
			await message.reply("❌ 此批次的媒体目前都无法发送")
			return

		if skipped_items:
			await message.reply(
				f"⚠️ 已送出 {len(sent_messages)} 个媒体，"
				f"另有 {len(skipped_items)} 个媒体暂时无法取得。"
			)
		return

	if len(text) < 15:
		return

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
		await message.reply(f"❌ 解码或解析失败: {exc}")


async def main() -> None:
	global bot_name
	me = await bot.get_me()
	bot_name = str(getattr(me, "username", "") or "")
	print(f"Bot started as @{bot_name}", flush=True)
	await bot.set_my_commands(
		[
			# BotCommand(command="start", description="开始"),
			# BotCommand(command="about", description="关于我"),
			BotCommand(command="me", description="查询飞行通行证"),
			# BotCommand(command="bonus", description="塔台发放 10 天时限"),
			BotCommand(command="rule", description="查看飞行通行证规则"),
			BotCommand(command="airport_access_request", description="请求进入机场"),
			BotCommand(command="invite", description="建立单人审核邀请"),
		],
		scope=BotCommandScopeAllPrivateChats(),
	)
	workers = [asyncio.create_task(_media_worker(index)) for index in range(MEDIA_WORKER_COUNT)]
	forward_worker = asyncio.create_task(_media_forward_worker())
	try:
		await dp.start_polling(bot)
	finally:
		for worker in workers:
			worker.cancel()
		forward_worker.cancel()
		await asyncio.gather(*workers, forward_worker, return_exceptions=True)
		blacklist_store.close()
		batch_store.close()
		received_media_store.close()
		shared_invite_link_store.close()
		user_expire_cache.close()


if __name__ == "__main__":
	asyncio.run(main())
