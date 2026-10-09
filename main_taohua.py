
import asyncio
import base64
import html
from io import BytesIO
import os
import re
import time
from functools import lru_cache
from aiogram import Dispatcher, Bot, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BufferedInputFile, CallbackQuery, ChatJoinRequest, CopyTextButton, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.types import ReplyKeyboardRemove
from aiogram.types import ErrorEvent

from utils.format_utils import FormatUtils
from utils.parse_utils import ParseUtils
from utils.user_manager import UserManager
from utils.blacklist_utils import BlacklistEntry, BlacklistStore
from utils.peach_exchange_store import (
	remember_peach_exchange_record,
	has_peach_exchange_for_user,
	remove_peach_exchange_for_user,
	bump_alert_button_text,
)
from aiogram.exceptions import (
	TelegramBadRequest,
	TelegramNetworkError,
	TelegramRetryAfter,

)
from aiogram.filters import Command, CommandObject

from pathlib import Path
from shared_config import SharedConfig
SharedConfig.load(True)

from utils.emoji_utils import EmojiUtils

PEACH_CHANNEL_ID = os.getenv("PEACH_CHANNEL_ID",0)

SWITCHBOT_TOKEN = SharedConfig.get("switch_bot_token", "")
X_MAN_BOT_ID = SharedConfig.get("x_man_bot_id", 0)
KEY_MAN_ID = SharedConfig.get("key_man_id", 0)
BOT_TOKEN = SharedConfig.get("my_bot_token", "")
CHAT_ROW = SharedConfig.get("chat","")
if CHAT_ROW:
	CHAT_SCHOOL = CHAT_ROW.get("school")
	CHAT_SCHOOL_GROUP_ID = int(os.getenv("CHAT_SCHOOL_GROUP_ID", CHAT_SCHOOL.get("chat_id"))) 
	CHAT_SCHOOL_THREAD_ID = int(os.getenv("CHAT_SCHOOL_THREAD_ID", CHAT_SCHOOL.get("thread_id")))

	CHAT_PUBLIC = CHAT_ROW.get("public")
	CHAT_PUBLIC_GROUP_ID = int(os.getenv("CHAT_PUBLIC_GROUP_ID", CHAT_PUBLIC.get("chat_id"))) 
	CHAT_PUBLIC_THREAD_ID = int(os.getenv("CHAT_PUBLIC_THREAD_ID", CHAT_PUBLIC.get("thread_id")))
	CHAT_PUBLIC_LINK = os.getenv("CHAT_PUBLIC_LINK", CHAT_PUBLIC.get("invite_link"))

ADMIN_USER_IDS = ParseUtils.parse_user_ids(SharedConfig.get("whitelist_user_ids") or [])
# 主要用户始终保留访问权限，避免共享配置遗漏时意外将其排除。
ADMIN_USER_IDS.update(ParseUtils.parse_user_ids([KEY_MAN_ID]))



from utils.user_utils import UserExpireCache
TAKEOFF_USER_LOCKS: dict[int, asyncio.Lock] = {}
TAKEOFF_KICK_LOCKS: dict[int, asyncio.Lock] = {}
TAKEOFF_KICK_ACTION_STATE: dict[tuple[int, int, int], str] = {}
TAKEOFF_KICK_ORIGINAL_MARKUPS: dict[
	tuple[int, int, int], InlineKeyboardMarkup
] = {}

TAKEOFF_KICK_REASONS = {
	"not_shota": "非正太资源，例如萝莉、男同。",
	"other": "其他不符合要求的内容",
}


from config import (
	IGNORED_TEXT_SUBSTRINGS,
	MESSAGE_REWARD_MINUTES,
	MEDIA_UPLOAD_EXTEND_MINUTES,
	MEDIA_VIEW_CONSUMPTION_MINUTES,
	MAX_HP_CAPACITY_MINUTES,
	MAX_HP_CAPACITY_QUANTITY,
	HOURLY_CONSUMPTION_MINUTES,
	HOURLY_CONSUMPTION_QUANTITY,
	MESSAGE_REWARD_QUANTITY,
	MEDIA_UPLOAD_EXTEND_QUANTITY,
	MEDIA_VIEW_CONSUMPTION_QUANTITY
)

TARGET_CHATS = [
	("桃花林", CHAT_PUBLIC_GROUP_ID),
	("桃花源", CHAT_SCHOOL_GROUP_ID),
]

DEFAULT_COVER_FILE_ID: str | None = None
DEFAULT_IMAGE_PATHS = (Path(__file__).resolve().parent / "default_image.jpeg",)

import base64
from functools import lru_cache
from pathlib import Path

DEFAULT_IMAGE_PATHS = (
    Path(__file__).resolve().parent / "default_image.jpeg",
)

_WHITE_JPEG_BASE64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "//////////////////////////////////////////////////////////////////////////////////////////////"
    "//////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////"
    "//////////////////////////////////////////////////////////////////////////////////////////////"
    "//////////////wAARCAAQABADASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEBAAAAAAAAA"
    "AAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AMf/AP/Z"
)

_WHITE_JPEG_BYTES = base64.b64decode(_WHITE_JPEG_BASE64)


@lru_cache(maxsize=1)
def _get_default_image_bytes() -> bytes:
    image_path = DEFAULT_IMAGE_PATHS[0]

    if image_path.is_file():
        return image_path.read_bytes()

    image_path.write_bytes(_WHITE_JPEG_BYTES)
    return _WHITE_JPEG_BYTES

if not BOT_TOKEN:
	raise RuntimeError("Missing bot token. Please set ENCBOT_TOKEN or BOT_TOKEN.")

from utils.time_utils import app_now

bot = Bot(
	token=BOT_TOKEN,
	default=DefaultBotProperties(link_preview_is_disabled=True),
)
dp = Dispatcher()



INACTIVE_CLEANUP_LOCK = asyncio.Lock()

# Telegram 出站调用统一走 utils/telegram_gate：
# 全局并发上限 + 按 chat 限速 + 收到 429 只对当前 chat 冷却，避免一次限流拖死整个 bot。
from utils.telegram_gate import telegram_call as _telegram_call_with_retry


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


''''
用戶管理
'''

user_manager = UserManager(
	bot=bot,
	blacklist_store=blacklist_store,
	user_expire_cache=user_expire_cache,
	telegram_call=_telegram_call_with_retry,
	lobby_group_id=CHAT_SCHOOL_GROUP_ID,
	terminal_channel_id=CHAT_PUBLIC_GROUP_ID,
	duty_free_group_id=CHAT_PUBLIC_GROUP_ID,
	flight_board_channel_id=CHAT_PUBLIC_GROUP_ID,
	user_state_stores=(),
)
_ban_user = user_manager.ban_user
_unban_user = user_manager.unban_user

_delete_inactive_user_data = user_manager.delete_inactive_user_data
_is_current_chat_member = UserManager.is_current_chat_member


INTRO_MIN_LEN = 2
INTRO_MAX_LEN = 100
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")


class IntroStates(StatesGroup):
	waiting_intro = State()


async def _ask_intro(message: Message, state: FSMContext, text: str) -> None:
	"""发送强制回复提示，并把它记录为当前要回复的消息。"""
	prompt = await _telegram_call_with_retry(
		"send intro prompt",
		lambda: message.reply(
			text,
			parse_mode=ParseMode.HTML,
			reply_markup=ForceReply(
				force_reply=True,
				input_field_placeholder=f"输入介绍内容（{INTRO_MIN_LEN}~{INTRO_MAX_LEN} 字）",
			),
		),
		chat_id=message.chat.id,
	)
	await state.update_data(prompt_message_id=prompt.message_id)





def _extract_media_dict(message: Message) -> dict:
	file_type = None
	caption = None
	file_id = None
	file_name = None

	# print(f"Extracting media from message: {message}")
	
	# if reply_message.video:
	# 		cover = getattr(reply_message.video, "cover", None)
	# 		if isinstance(cover, list) and cover:
	# 			preview = cover[0]
	# 		elif cover:
	# 			preview = cover
	# 		if not preview:
	# 			preview = reply_message.video.thumbnail
	# 	elif reply_message.document:
	# 		preview = reply_message.document.thumbnail

	thumb_file_id = None

	if message.video:
		file_type = "video"
		file_id = message.video.file_id
		cover = getattr(message.video, "cover", None)
		if isinstance(cover, list) and cover:
			thumb_file_id = cover[0]
		elif cover:
			thumb_file_id = cover
		if not thumb_file_id:
			thumb_file_id = message.video.thumbnail


	elif message.document:
		mime_type = str(message.document.mime_type or "").lower()
		file_type = "video" if mime_type == "video/mp4" else "document"
		file_id = message.document.file_id
		thumb_file_id = message.document.thumbnail

	file_name = str((message.video or message.document).file_name or "").strip()

	
	if message.caption:
		caption = message.caption.strip()
		if EmojiUtils._extract_consecutive_emojis(caption):
			print(f"Caption contains consecutive emojis: {caption}")
			caption = EmojiUtils.strip_consecutive_emojis(caption)
			print(f"Caption cleaned: {caption}")

	return {"file_type": file_type, "caption": caption, "file_id": file_id, "file_name": file_name, "thumb_file_id": thumb_file_id}

def _extract_media_info(message: Message) -> tuple[str, str]:
	if message.video:
		return "video", message.video.file_id
	mime_type = str(message.document.mime_type or "").lower()
	return ("video" if mime_type == "video/mp4" else "document"), message.document.file_id

async def say_hello_to_x_man(bot_name):

	if SWITCHBOT_TOKEN:
		switchbot = Bot(
			token=SWITCHBOT_TOKEN,
			default=DefaultBotProperties(parse_mode=ParseMode.HTML)
		)
		try:
			await switchbot.send_message(X_MAN_BOT_ID, f"|_kick_|@{bot_name}")
			print("✅ Sent hello to X-Man bot", flush=True)
		except Exception as exc:
			print(f"❌ Failed to send hello to X-Man bot: {exc}", flush=True)
		finally:

			await switchbot.session.close()
	else:
		print("❌ No SWITCHBOT_TOKEN found, skipping hello to X-Man bot.", flush=True)

async def _check_bot_group_admin_permissions() -> str:
	groups = (
		("CHAT_PUBLIC_GROUP_ID", CHAT_PUBLIC_GROUP_ID),
		("CHAT_SCHOOL_GROUP_ID", CHAT_SCHOOL_GROUP_ID),
		("PEACH_CHANNEL_ID", PEACH_CHANNEL_ID),
		
	)
	notice_text = ""
	for setting_name, chat_id in groups:
		if chat_id == 0:
			print(
				f"❌[群組設定錯誤] {setting_name} 尚未配置。",
				flush=True,
			)
			continue

		try:
			bot_status = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
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
				f"❌[群組權限不足] 機器人在 {setting_name}={chat_id} 中的身分為 "
				f"{bot_status.status}，請將機器人設為管理員。\n"
			)
			continue
		notice_text += (
			f"✅[群組檢查成功] 機器人已在 {setting_name}={chat_id} 中並具有管理員權限。\n"
		)

	if notice_text:
		print(notice_text, flush=True)
		if int(KEY_MAN_ID or 0) > 0:
			try:
				await bot.send_message(chat_id=KEY_MAN_ID, text=notice_text)
			except Exception as exc:
				print(
					f"⚠️ [群組檢查通知未送出] KEY_MAN_ID={KEY_MAN_ID}: {exc}",
					flush=True,
				)
		else:
			print(
				"⚠️ [群組檢查通知未送出] KEY_MAN_ID 尚未配置。",
				flush=True,
			)
	return notice_text


@dp.message(
	F.chat.type == "private",
	F.document | F.photo | F.video | F.audio | F.voice | F.animation | F.sticker,
)
async def on_media(message: Message, state: FSMContext) -> None:
	if not message.from_user:
		return
	# 如果不是視頻或文件
	if not (message.video or message.document):
		await message.reply("只接受视频或文件消息")
		print(f"只接受视频或文件消息", flush=True)
		return

	media_info = _extract_media_dict(message)
	file_type = media_info.get("file_type")
	file_id = media_info.get("file_id")
	file_name = media_info.get("file_name")
	caption = media_info.get("caption")
	thumb_file_id = media_info.get("thumb_file_id")

		

	had_pending = await state.get_state() == IntroStates.waiting_intro.state
	await state.set_state(IntroStates.waiting_intro)
	await state.set_data(
		{
			"media_message_id": message.message_id,
			"file_type": file_type,
			"file_id": file_id,
			"uploader_id": int(message.from_user.id),
			"thumb_file_id": thumb_file_id,
		}
	)


	lines = []
	if had_pending:
		lines.append("⚠️ 上一个媒体尚未填写介绍内容，已被放弃。")

	if caption:
		chinese_name = caption
		
	elif not caption and file_name:
		chinese_name = Path(file_name).stem.strip() if CHINESE_RE.search(file_name) else ""
	else:
		chinese_name = ""
	
	if chinese_name:
		
		# keyboard = ReplyKeyboardMarkup(
		# 	keyboard=[
		# 		[
		# 			KeyboardButton(text=f"{html.escape(chinese_name)}"),
					
		# 		]
		# 	],
		# 	resize_keyboard=True,
		# 	one_time_keyboard=True,
		# 	input_field_placeholder="请选择操作",
		# 	selective=False,
		# )
		

		# await message.answer(
		# 	"可点击下方按钮复制文字作为介绍：",
		# 	reply_markup=keyboard
		# )



		await _telegram_call_with_retry(
			"send intro copy button",
			lambda: bot.send_message(
				message.chat.id,
				f"<a href=\"https://t.me/{bot_name}?text={html.escape(chinese_name)}\">{html.escape(chinese_name)}</a>\n\n",
				parse_mode="HTML",
				reply_markup=InlineKeyboardMarkup(
					inline_keyboard=[
						[
							InlineKeyboardButton(
								text="📋 复制",
								copy_text=CopyTextButton(text=f"{html.escape(chinese_name)}"),
							)
						]
					]
				),
			),
			chat_id=message.chat.id,
		)

		lines.append(
			"请回复此消息并输入介绍内容（2~100 字）。"
		)
	else:
		lines.append("请回复此消息并自行输入介绍内容（2~100 字）。")

	await _ask_intro(message, state, "\n\n".join(lines))


def encode_file_url(data: dict) -> str:
	return f"https://peach.{data['file_type']}/{data['uploader_id']}/{data['file_id']}/{data['thumb_file_id']}"

def parse_file_url(url: str) -> dict:
	parts = url.split("/")
	if len(parts) < 5:
		return {}
	file_type = parts[2].split(".")[-1]
	uploader_id = parts[3]
	file_id = parts[4]
	thumb_file_id = parts[5] if len(parts) > 5 else ""
	return {"file_type": file_type, "uploader_id": uploader_id, "file_id": file_id, "thumb_file_id": thumb_file_id}






# 置顶节流：同一群组在间隔内只置顶一次，减少 pin/服务消息带来的 API 调用量
PIN_MIN_INTERVAL_SECONDS = 60.0
LAST_INTRO_PIN_AT: dict[int, float] = {}


async def _pin_intro_post(message_id: int) -> None:
	"""置顶投稿；同一群组在 PIN_MIN_INTERVAL_SECONDS 内只置顶一次。"""
	now = time.monotonic()
	last = LAST_INTRO_PIN_AT.get(CHAT_PUBLIC_GROUP_ID, 0.0)
	if now - last < PIN_MIN_INTERVAL_SECONDS:
		return
	# 先占位再发请求，避免并发投稿同时通过检查
	LAST_INTRO_PIN_AT[CHAT_PUBLIC_GROUP_ID] = now
	try:
		await _telegram_call_with_retry(
			"pin intro post",
			lambda: bot.pin_chat_message(
				chat_id=CHAT_PUBLIC_GROUP_ID,
				message_id=message_id,
				disable_notification=True,
			),
			chat_id=CHAT_PUBLIC_GROUP_ID,
			kind="edit",
		)
	except Exception as exc:
		print(f"[ENCODED_FORWARD] pin post_item failed: {exc}", flush=True)


@dp.message(IntroStates.waiting_intro, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def on_intro_text(message: Message, state: FSMContext) -> None:
	if not message.from_user:
		return
	text = (message.text or "").strip()

	data = await state.get_data()

	replied = message.reply_to_message
	if not replied or replied.message_id not in (
		data.get("prompt_message_id"),
		data.get("media_message_id"),
	):
		await _ask_intro(message, state, "需要选择要介绍的媒体，并回复该消息，输入介绍内容。")
		return
	
	

	

	if not INTRO_MIN_LEN <= len(text) <= INTRO_MAX_LEN:
		await _ask_intro(
			message,
			state,
			f"介绍内容需为 {INTRO_MIN_LEN}~{INTRO_MAX_LEN} 字，目前 {len(text)} 字，请重新回复此消息。",
		)
		return

	preview = None
	thumb_file_id = ""


	thumb_file_id = data.get("thumb_file_id")
	if thumb_file_id:
		try:
			thumb_result = await bot.send_photo(
				chat_id=X_MAN_BOT_ID,
				photo=thumb_file_id,
			)

			print(f"thumb_result_file_id={thumb_result.photo[-1].file_id}", flush=True)
		
			
		except Exception as exc:
			print(f"[ENCODED_FORWARD] send thumb to X_MAN_BOT_ID failed: {exc}", flush=True)

			try:
				buffer = BytesIO()

				await bot.download(
					thumb_file_id,
					destination=buffer,
				)

				thumb_result = await bot.send_photo(
					chat_id=X_MAN_BOT_ID,
					photo=BufferedInputFile(
						buffer.getvalue(),
						filename="thumbnail.jpg",
					),
				)

				print(f"thumb_result2_file_id={thumb_result.photo[-1].file_id}", flush=True)
				thumb_file_id = thumb_result.photo[-1].file_id
				data["thumb_file_id"] = thumb_file_id
			except Exception as exc:
				print(f"[ENCODED_FORWARD] download and send thumb failed: {exc}", flush=True)


	
	url = encode_file_url(data)
	
	try:
		post_item = await _telegram_call_with_retry(
			"publish intro post",
			lambda: bot.send_message(
				CHAT_PUBLIC_GROUP_ID,
				f'<a href="{html.escape(url, quote=True)}">🌼</a> {html.escape(text)}',
				message_thread_id=CHAT_PUBLIC_THREAD_ID or None,
				parse_mode=ParseMode.HTML,
				reply_markup=InlineKeyboardMarkup(
					inline_keyboard=[[
						InlineKeyboardButton(text="👍", callback_data="alert:like"),
						InlineKeyboardButton(text="🍑", callback_data="peach:link"),
						InlineKeyboardButton(text="👎", callback_data="alert:dislike")
						]]
				),
			),
			chat_id=CHAT_PUBLIC_GROUP_ID,
		)
	except Exception as exc:
		# 发布失败（含限流）：清理 FSM 状态并提示重试，避免用户卡在介绍流程
		print(f"[ENCODED_FORWARD] publish intro post failed: {exc}", flush=True)
		await state.clear()
		try:
			await _telegram_call_with_retry(
				"notify intro publish failure",
				lambda: message.reply("❌ 发布失败（网络或 Telegram 限流），请稍后再试一次。"),
				chat_id=message.chat.id,
			)
		except Exception as reply_exc:
			print(f"[ENCODED_FORWARD] notify publish failure failed: {reply_exc}", flush=True)
		return

	# 将 post_item 置顶（带节流，见 _pin_intro_post）
	await _pin_intro_post(post_item.message_id)


	try:
		if PEACH_CHANNEL_ID and int(PEACH_CHANNEL_ID) != 0:
			if data['file_type'] == "video":
				await _telegram_call_with_retry(
					"forward intro video to peach channel",
					lambda: bot.send_video(
						chat_id = PEACH_CHANNEL_ID,
						video =data['file_id'],
						parse_mode="HTML",
						caption=f"{html.escape(text)}",
					),
					chat_id=PEACH_CHANNEL_ID,
				)
			elif data['file_type'] == "document":
				await _telegram_call_with_retry(
					"forward intro document to peach channel",
					lambda: bot.send_document(
						chat_id = PEACH_CHANNEL_ID,
						document =data['file_id'],
						parse_mode="HTML",
						caption=f"{html.escape(text)}",
					),
					chat_id=PEACH_CHANNEL_ID,
				)
	except Exception as exc:
		print(f"[ENCODED_FORWARD] send to PEACH_CHANNEL failed: {exc}", flush=True)


	await state.clear()
	
	# 介绍成功,增加用户期限(user_expire)六小时,并回复用户增加后的时间
	from_user_id = int(message.from_user.id)
	try:
		now_timestamp = int(app_now().timestamp())
		previous_user_expire = user_expire_cache.get(from_user_id)
		previous_expire_timestamp = (
			previous_user_expire.expire_timestamp
			if previous_user_expire
			else 0
		)
		base_timestamp = max(now_timestamp, previous_expire_timestamp)
		requested_minutes = MEDIA_UPLOAD_EXTEND_MINUTES
		peach_count = MEDIA_UPLOAD_EXTEND_MINUTES // MEDIA_VIEW_CONSUMPTION_MINUTES
		max_peach_count = MAX_HP_CAPACITY_MINUTES // MEDIA_VIEW_CONSUMPTION_MINUTES


		user_expire = user_expire_cache.extend_minutes(
			from_user_id,
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
		actual_added_text = FormatUtils.minutes_to_day_hour(actual_added_minutes)[0]
		remaining_text, remaining_view_count = FormatUtils.minutes_to_day_hour(remaining_minutes)
		expire_text = FormatUtils.format_timestamp_utc8(user_expire.expire_timestamp)
		
		status_text = await get_user_status(from_user_id)
		
		notify_text = (
			f"🌿 灵韵馈赠成功！你获得了 {peach_count} 点桃气值。 \n"
			f"\n\n{status_text}"
		)



		
		await _telegram_call_with_retry(
			"notify upload reward",
			lambda: bot.send_message(
				chat_id=from_user_id,
				text=notify_text,
				parse_mode="HTML",
				reply_markup=ReplyKeyboardRemove(),
			),
			chat_id=from_user_id,
		)

		# print(
		# 	f"[ENCODED_FORWARD] granted {actual_added_minutes}/{requested_minutes} "
		# 	f"minutes to user {from_user_id}",
		# 	flush=True,
		# )
	except Exception as exc:
		print(f"[ENCODED_FORWARD] membership reward failed: {exc}", flush=True)




	
'''
客製化
'''
def _format_blacklist_entry(entry: BlacklistEntry) -> str:
	text = (
		f"用户 ID：{entry.user_id}\n"
		f"封禁原因：{entry.reason}\n"
		f"操作管理员：{entry.created_by}\n"
		f"封禁时间：{FormatUtils.format_timestamp_utc8(entry.created_at)}"
	)
	if entry.expires_at > 0:
		text += f"\n到期时间：{FormatUtils.format_timestamp_utc8(entry.expires_at)}"
	else:
		text += "\n到期时间：永久"
	return text


def _build_takeoff_admin_keyboard(
	uploader_id: int,
	source_chat_id: int,
	source_message_id: int,
) -> list[list[InlineKeyboardButton]]:
	return [
		[
			InlineKeyboardButton(
				text="🚫 删除消息并拉黑上传者",
				callback_data=(
					f"ta:b:{uploader_id}:{source_chat_id}:{source_message_id}"
				),
			),
		],
		[
			InlineKeyboardButton(
				text="🗑 删除消息",
				callback_data=f"ta:d:{source_chat_id}:{source_message_id}",
			),
		],
		[
			InlineKeyboardButton(
				text="🦶 踢出用户",
				callback_data=(
					f"ta:k:{uploader_id}:{source_chat_id}:{source_message_id}"
				),
			),
		],
	]


def _build_takeoff_kick_reason_keyboard(
	uploader_id: int,
	source_chat_id: int,
	source_message_id: int,
) -> InlineKeyboardMarkup:
	rows = [
		[
			InlineKeyboardButton(
				text=reason,
				callback_data=(
					f"ta:kr:{code}:{uploader_id}:"
					f"{source_chat_id}:{source_message_id}"
				),
			)
		]
		for code, reason in TAKEOFF_KICK_REASONS.items()
	]
	rows.append([
		InlineKeyboardButton(
			text="❌ 取消",
			callback_data=(
				f"ta:kr:cancel:{uploader_id}:"
				f"{source_chat_id}:{source_message_id}"
			),
		)
	])
	return InlineKeyboardMarkup(inline_keyboard=rows)


def _takeoff_kick_processing_keyboard() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(inline_keyboard=[[
		InlineKeyboardButton(
			text="⏳ 正在移除用户",
			callback_data="ta:kp:wait",
		),
	]])


@dp.callback_query(F.data == "ta:kp:wait")
async def on_takeoff_kick_processing(callback: CallbackQuery) -> None:
	await callback.answer("正在处理，请勿重复操作")


@dp.callback_query(F.data.startswith("ta:k:"))
async def on_takeoff_admin_kick_menu(callback: CallbackQuery) -> None:
	if int(callback.from_user.id) not in ADMIN_USER_IDS:
		await callback.answer("❌ 你没有权限执行此操作", show_alert=True)
		return
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return

	parts = str(callback.data or "").removeprefix("ta:k:").split(":")
	if len(parts) != 3:
		await callback.answer("踢出用户参数无效", show_alert=True)
		return
	uploader_id = ParseUtils._parse_positive_user_id(parts[0])
	source_chat_text = parts[1]
	source_message_id = ParseUtils._parse_positive_user_id(parts[2])
	if (
		uploader_id is None
		or not source_chat_text.lstrip("-").isdigit()
		or int(source_chat_text) == 0
		or source_message_id is None
	):
		await callback.answer("踢出用户参数无效", show_alert=True)
		return
	if uploader_id in ADMIN_USER_IDS:
		await callback.answer("❌ 不能踢出管理员", show_alert=True)
		return

	action_key = (
		int(callback.message.chat.id),
		int(callback.message.message_id),
		uploader_id,
	)
	lock = TAKEOFF_KICK_LOCKS.setdefault(uploader_id, asyncio.Lock())
	if lock.locked() or TAKEOFF_KICK_ACTION_STATE.get(action_key) in {
		"processing",
		"completed",
	}:
		await callback.answer("该用户正在处理或已经处理完成", show_alert=True)
		return
	if callback.message.reply_markup:
		TAKEOFF_KICK_ORIGINAL_MARKUPS[action_key] = callback.message.reply_markup

	await callback.message.edit_reply_markup(
		reply_markup=_build_takeoff_kick_reason_keyboard(
			uploader_id,
			int(source_chat_text),
			source_message_id,
		)
	)
	await callback.answer("请选择移除理由")


@dp.callback_query(F.data.startswith("ta:kr:"))
async def on_takeoff_admin_kick_reason(callback: CallbackQuery) -> None:
	if int(callback.from_user.id) not in ADMIN_USER_IDS:
		await callback.answer("❌ 你没有权限执行此操作", show_alert=True)
		return
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return

	parts = str(callback.data or "").removeprefix("ta:kr:").split(":")
	if len(parts) != 4:
		await callback.answer("移除理由参数无效", show_alert=True)
		return
	reason_code = parts[0]
	uploader_id = ParseUtils._parse_positive_user_id(parts[1])
	source_chat_text = parts[2]
	source_message_id = ParseUtils._parse_positive_user_id(parts[3])
	if (
		uploader_id is None
		or not source_chat_text.lstrip("-").isdigit()
		or int(source_chat_text) == 0
		or source_message_id is None
	):
		await callback.answer("移除理由参数无效", show_alert=True)
		return
	source_chat_id = int(source_chat_text)
	action_key = (
		int(callback.message.chat.id),
		int(callback.message.message_id),
		uploader_id,
	)
	lock = TAKEOFF_KICK_LOCKS.setdefault(uploader_id, asyncio.Lock())

	if reason_code == "cancel":
		if lock.locked() or TAKEOFF_KICK_ACTION_STATE.get(action_key) in {
			"processing",
			"completed",
		}:
			await callback.answer("该用户正在处理或已经处理完成", show_alert=True)
			return
		original_markup = TAKEOFF_KICK_ORIGINAL_MARKUPS.pop(action_key, None)
		if original_markup is None:
			original_markup = InlineKeyboardMarkup(
				inline_keyboard=_build_takeoff_admin_keyboard(
					uploader_id,
					source_chat_id,
					source_message_id,
				)
			)
		await callback.message.edit_reply_markup(reply_markup=original_markup)
		await callback.answer("已取消")
		return

	reason = TAKEOFF_KICK_REASONS.get(reason_code)
	if not reason:
		await callback.answer("未知的移除理由", show_alert=True)
		return
	if uploader_id in ADMIN_USER_IDS:
		await callback.answer("❌ 不能踢出管理员", show_alert=True)
		return

	if CHAT_SCHOOL_GROUP_ID == 0 or CHAT_PUBLIC_GROUP_ID == 0:
		await callback.answer("桃花林或桃花源尚未配置", show_alert=True)
		return

	if lock.locked() or TAKEOFF_KICK_ACTION_STATE.get(action_key) in {
		"processing",
		"completed",
	}:
		await callback.answer("该用户正在处理或已经处理完成", show_alert=True)
		return

	async with lock:
		if TAKEOFF_KICK_ACTION_STATE.get(action_key) in {"processing", "completed"}:
			await callback.answer("该用户正在处理或已经处理完成", show_alert=True)
			return
		await callback.message.edit_reply_markup(
			reply_markup=_takeoff_kick_processing_keyboard()
		)
		TAKEOFF_KICK_ACTION_STATE[action_key] = "processing"
		try:
			await callback.answer("正在执行移除")
		except Exception as exc:
			print(f"[TAKEOFF_KICK] callback answer failed: {exc}", flush=True)

		notice_errors: list[str] = []
		try:
			await _telegram_call_with_retry(
				f"notify kicked uploader {uploader_id}",
				lambda: bot.send_message(
					chat_id=uploader_id,
					text=(
						"🦶 桃花村移除通知\n\n"
						"你将被移出「桃花林」与「桃花源」。\n"
						f"移除理由：{reason}\n\n"						
					),
				),
			)
		except Exception as exc:
			notice_errors.append(f"私聊通知失败：{exc}")
			print(
				f"[TAKEOFF_KICK] user notice failed for {uploader_id}: {exc}",
				flush=True,
			)

		try:
			await _telegram_call_with_retry(
				f"broadcast kicked uploader {uploader_id}",
				lambda: bot.send_message(
					chat_id=CHAT_SCHOOL_GROUP_ID,
					text=(
						"🦶 成员移除公告\n\n"
						f'<a href="tg://user?id={uploader_id}">{uploader_id}</a> '
						"将被移出「桃花林」与「桃花源」。\n"
						f"理由：{reason}"
					),
					parse_mode="HTML",
				),
			)
		except Exception as exc:
			notice_errors.append(f"桃花林公告失败：{exc}")
			print(
				f"[TAKEOFF_KICK] school broadcast failed for {uploader_id}: {exc}",
				flush=True,
			)


		entry, group_ban_error = await _ban_user(
			user_id=uploader_id,
			reason=reason,
			created_by=int(callback.from_user.id),
			target_chats=TARGET_CHATS,
		)

		

		####
		if group_ban_error:
			TAKEOFF_KICK_ACTION_STATE.pop(action_key, None)		
			print(f"[TAKEOFF_KICK] group ban failed for {uploader_id}: {group_ban_error}", flush=True)
			await callback.message.reply(group_ban_error)
			return

		try:
			_delete_inactive_user_data(uploader_id)
		except Exception as exc:
			TAKEOFF_KICK_ACTION_STATE.pop(action_key, None)
			await callback.message.edit_reply_markup(
				reply_markup=_build_takeoff_kick_reason_keyboard(
					uploader_id,
					source_chat_id,
					source_message_id,
				)
			)
			await callback.message.reply(f"❌ 用户已移出，但通行证数据删除失败：{exc}")
			return

		TAKEOFF_KICK_ACTION_STATE[action_key] = "completed"
		TAKEOFF_KICK_ORIGINAL_MARKUPS.pop(action_key, None)
		await callback.message.edit_reply_markup(reply_markup=None)
		print(
			f"[TAKEOFF_KICK] removed user {uploader_id}, reason={reason}; "	,	
			flush=True,
		)
		result_lines = [
			"✅ 用户移除完成",
			f"用户：{uploader_id}",
			f"理由：{reason}",			
			"通行证数据已删除。",
		]
		result_lines.extend(notice_errors)
		await callback.message.reply("\n".join(result_lines))


		


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
	target_user_id = ParseUtils._parse_positive_user_id(parts[0])
	source_chat_text = parts[1]
	source_message_id = ParseUtils._parse_positive_user_id(parts[2])
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
		target_chats=TARGET_CHATS
	)
	delete_error = ""
	try:
		await _telegram_call_with_retry(
			"delete source message (ban)",
			lambda: bot.delete_message(
				chat_id=source_chat_id,
				message_id=source_message_id,
			),
			chat_id=source_chat_id,
			kind="delete",
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
	source_message_id = ParseUtils._parse_positive_user_id(parts[1])
	if (
		not source_chat_text.lstrip("-").isdigit()
		or int(source_chat_text) == 0
		or source_message_id is None
	):
		await callback.answer("消息位置参数无效", show_alert=True)
		return
	source_chat_id = int(source_chat_text)

	try:
		await _telegram_call_with_retry(
			"delete source message (admin)",
			lambda: bot.delete_message(
				chat_id=source_chat_id,
				message_id=source_message_id,
			),
			chat_id=source_chat_id,
			kind="delete",
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
			await _telegram_call_with_retry(
				"delete takeoff message",
				lambda: callback.message.delete(),
				chat_id=callback.message.chat.id,
				kind="delete",
			)
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







# 同一条群消息的 👍/👎 编辑锁：防止并发双击造成重复编辑与重复计数
ALERT_EDIT_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


@dp.callback_query(F.data.startswith(("alert:like", "alert:dislike")))
async def on_alert_dislike(callback: CallbackQuery) -> None:
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return

	reply_markup = callback.message.reply_markup
	if not reply_markup or not getattr(reply_markup, "inline_keyboard", None):
		await callback.answer("没有可更新的按钮组", show_alert=True)
		return

	chat_id = int(callback.message.chat.id)
	message_id = int(callback.message.message_id)
	user_id = int(callback.from_user.id)
	edit_lock = ALERT_EDIT_LOCKS.setdefault((chat_id, message_id), asyncio.Lock())
	async with edit_lock:
		if not has_peach_exchange_for_user(user_id, message_id):
			await callback.answer("只有在兑换后的三分钟内可以 👍 或 👎", show_alert=True)
			return

		updated_rows: list[list[InlineKeyboardButton]] = []
		updated = False
		for row in reply_markup.inline_keyboard:
			new_row: list[InlineKeyboardButton] = []
			for button in row:
				button_callback = getattr(button, "callback_data", None)
				if button_callback == callback.data:
					new_text = bump_alert_button_text(button.text, button_callback)
					button = InlineKeyboardButton(
						text=new_text,
						callback_data=button_callback,
						url=button.url,
						web_app=button.web_app,
						login_url=button.login_url,
						switch_inline_query=button.switch_inline_query,
						switch_inline_query_current_chat=button.switch_inline_query_current_chat,
						switch_inline_query_chosen_chat=button.switch_inline_query_chosen_chat,
						copy_text=button.copy_text,
						callback_game=button.callback_game,
						pay=button.pay,
					)
					updated = True
				new_row.append(button)
			updated_rows.append(new_row)

		if not updated:
			await callback.answer("未找到可更新的按钮", show_alert=True)
			return

		try:
			await _telegram_call_with_retry(
				"update alert button",
				lambda: bot.edit_message_reply_markup(
					chat_id=chat_id,
					message_id=message_id,
					reply_markup=InlineKeyboardMarkup(inline_keyboard=updated_rows),
				),
				chat_id=chat_id,
				kind="edit",
			)
			remove_peach_exchange_for_user(user_id, message_id)
		except Exception:
			await callback.answer("更新按钮状态失败", show_alert=True)
			return

		await callback.answer("已更新", show_alert=False)
		return


@dp.callback_query(F.data.startswith(("click:like", "click:dislike")))
async def on_click_dislike(callback: CallbackQuery) -> None:
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return

	user_id = int(callback.from_user.id)
	message_id = int(callback.message.message_id)
	if has_peach_exchange_for_user(user_id, message_id):
		remove_peach_exchange_for_user(user_id, message_id)
		await callback.answer("有值", show_alert=True)
		return

	await callback.answer("没有值", show_alert=True)
	return

# 采菊投递节流：同一用户对同一条消息在间隔内只投递一次，防止连点重复发送
PEACH_REDELIVERY_MIN_INTERVAL = 10.0
PEACH_LAST_DELIVERY_AT: dict[tuple[int, int], float] = {}


def _prune_delivery_records() -> None:
	if len(PEACH_LAST_DELIVERY_AT) < 512:
		return
	cutoff = time.monotonic() - PEACH_REDELIVERY_MIN_INTERVAL
	for key, stamp in list(PEACH_LAST_DELIVERY_AT.items()):
		if stamp < cutoff:
			PEACH_LAST_DELIVERY_AT.pop(key, None)



@dp.callback_query(F.data.startswith(("peach:link","preview:link","buy:link")))
async def on_preview_link(callback: CallbackQuery) -> None:
	global DEFAULT_COVER_FILE_ID
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return
	message = callback.message

	act_type = callback.data.split(":")[0]

	
	

	# print(f"{callback.message.text}")

	if not message:
		await callback.answer("无法获取消息", show_alert=True)
		return

	entities = (
		getattr(message, "entities", None) or []
	) + (
		getattr(message, "caption_entities", None) or []
	)

	
	# print(f"message=>{message}")
	url = next((e.url for e in entities if e.type == "text_link" and e.url), "")
	if not url:
		await callback.answer("找不到链接", show_alert=True)
		return

	# 解析 url, 取出 file_type 和 file_id
	file_data = parse_file_url(url)
	uploader_id = file_data.get("uploader_id","")
	file_type = file_data.get("file_type","")
	file_id = file_data.get("file_id","")
	thumb_file_id = file_data.get("thumb_file_id","")

	
	if not file_id:
		await callback.answer("此菊花已失效", show_alert=True)
		return

	reader_user_id = int(callback.from_user.id)
	# print(f"2439 reader_user_id = {reader_user_id}")

	is_admin  = False
	if reader_user_id in ADMIN_USER_IDS:
		is_admin = True

	requested_minutes = MEDIA_VIEW_CONSUMPTION_MINUTES
	user_lock = TAKEOFF_USER_LOCKS.setdefault(reader_user_id, asyncio.Lock())

	chat_id = callback.message.chat.id
	message_id = callback.message.message_id

	async with user_lock:
		delivery_key = (reader_user_id, message_id)
		if (
			time.monotonic() - PEACH_LAST_DELIVERY_AT.get(delivery_key, 0.0)
			< PEACH_REDELIVERY_MIN_INTERVAL
		):
			await callback.answer("刚刚已发送过，请稍后再试", show_alert=True, cache_time=0)
			return
		_prune_delivery_records()
		now_timestamp = int(app_now().timestamp())
		user_expire = user_expire_cache.get(reader_user_id)
		# print(f"now_timestamp=>{now_timestamp}")
		# print(f"user_expire=>{user_expire}")
		if not is_admin:
		
			if (
				not user_expire
				or now_timestamp - user_expire.group_message_timestamp > 24 * 60 * 60
			):

				await callback.answer(
					text=(
						"📢 桃花村广播\n\n"
						"每日至少发言一次才能采菊。"					
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
						
				await callback.answer(
					text=(
						f"目前你的桃气值不足，无法进行采菊。\n\n"
						f"你可以选择在桃花村发言 ( 1 分钟可得 1 桃气值 ) 或是分享资源，就可以获得桃气值。"
					),
					show_alert=True,
					cache_time=0,
				)
				return

			original_expire_timestamp = user_expire.expire_timestamp
			if user_expire_cache.consume_minutes(reader_user_id, requested_minutes) is None:
				await callback.answer("桃气值不足，请重新尝试", show_alert=True, cache_time=0)
				return


		#=========================================================================================
		requested_human_time = FormatUtils.minutes_to_day_hour(requested_minutes)[0]
		new_user_expire = user_expire_cache.get(reader_user_id)
		expire_text = FormatUtils.format_timestamp_utc8(new_user_expire.expire_timestamp)
		remaining_minutes = max(
			0,
			(new_user_expire.expire_timestamp - now_timestamp) // 60,
		)

		remaining_text, remaining_view_count = FormatUtils.minutes_to_day_hour(remaining_minutes)

		cover_text = callback.message.text or callback.message.caption
		cover_text = cover_text.replace('🌼',f'<a href="{url}">🌼</a>')
		# print(f"cover_text=>{cover_text}")

		if act_type == "preview":
			notify_text = (
				f"{cover_text}"
			)
		else:
			notify_text = (
				f"{cover_text}\n\n"
				f"✅ 采菊成功，消耗 {requested_minutes // HOURLY_CONSUMPTION_MINUTES} 点桃气值。\n"
				f"🍑 剩余桃气值：{remaining_view_count} / {MAX_HP_CAPACITY_QUANTITY} 点\n"
				f"🕒 预计耗尽：{expire_text}\n"
			)

		notify_keyboard_rows: list[list[InlineKeyboardButton]] = []


		return_url = f"https://t.me/c/{str(chat_id).lstrip('-100')}/{message_id}"

		notify_keyboard_rows.append([
			InlineKeyboardButton(
				text="🔙 返回",
				url=f"{return_url}",
			),
		])

		if act_type == "preview":
			notify_keyboard_rows.append([
				InlineKeyboardButton(
					text="🍑 兑换",
					callback_data=f"buy:link:{message_id}",
				),
			])

		
		if is_admin:
			source_chat_id = int(callback.message.chat.id)
			source_message_id = int(callback.message.message_id)
			uploader_text = await FormatUtils.get_user_hyperlink(
				bot,
				{"id": uploader_id},
				show_uid=True,
			)
			
			notify_text += f"\n👤 上传者：{uploader_text}"
			notify_keyboard_rows.extend(
				_build_takeoff_admin_keyboard(
					uploader_id,
					source_chat_id,
					source_message_id,
				)
			)

		if act_type == "buy":
			# 从点击的 callback 直接复制其 reply_markup 中的按钮组，排除原来的“兑换”按钮
			source_markup = callback.message.reply_markup
			if source_markup and getattr(source_markup, "inline_keyboard", None):
				filtered_rows: list[list[InlineKeyboardButton]] = []
				for row in source_markup.inline_keyboard:
					filtered_row = [
						button for button in row
						if not (getattr(button, "callback_data", None) or "").startswith("buy:link:")
					]
					if filtered_row:
						filtered_rows.append(filtered_row)
				notify_markup = (
					InlineKeyboardMarkup(inline_keyboard=filtered_rows)
					if filtered_rows
					else None
				)
			else:
				notify_markup = (
					InlineKeyboardMarkup(inline_keyboard=notify_keyboard_rows)
					if notify_keyboard_rows
					else None
				)
		else:
			notify_markup = (
				InlineKeyboardMarkup(inline_keyboard=notify_keyboard_rows)
				if notify_keyboard_rows
				else None
			)

		try:
			if act_type == "preview":
				if thumb_file_id:
					await _telegram_call_with_retry(
						"deliver peach cover (photo)",
						lambda: bot.send_photo(
							chat_id = callback.from_user.id,
							photo =thumb_file_id,
							parse_mode="HTML",
							reply_markup=notify_markup,
							caption=notify_text,
						),
						chat_id=callback.from_user.id,
					)
					
				else:	
					if DEFAULT_COVER_FILE_ID is None:
						published_message = await _telegram_call_with_retry(
							"send default cover",
							lambda: bot.send_photo(
								chat_id = callback.from_user.id,
								photo=BufferedInputFile(
									_get_default_image_bytes(),
									filename="default_image.jpeg",
								),
								reply_markup=notify_markup,
								caption=notify_text,
								parse_mode="HTML" if notify_text else None,
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
								chat_id = callback.from_user.id,
								photo=DEFAULT_COVER_FILE_ID,
								reply_markup=notify_markup,
								caption=notify_text,
								parse_mode="HTML" if notify_text else None,
							),
						)



				await callback.answer(
					url=f"https://t.me/{bot_name}?start=fly_{chat_id}_{message_id}",
					cache_time=0,
				)


			elif act_type == "peach":

				if file_type == "video":
					await _telegram_call_with_retry(
						"deliver peach media (video)",
						lambda: bot.send_video(
							chat_id = callback.from_user.id,
							video =file_id,
							parse_mode="HTML",
							reply_markup=notify_markup,
							caption=notify_text,
						),
						chat_id=callback.from_user.id,
					)
				elif file_type == "document":
					await _telegram_call_with_retry(
						"deliver peach media (document)",
						lambda: bot.send_document(
							chat_id = callback.from_user.id,
							document =file_id,
							parse_mode="HTML",
							reply_markup=notify_markup,
							caption=notify_text,
						),
						chat_id=callback.from_user.id,
					)

				remember_peach_exchange_record(message_id, reader_user_id)
				PEACH_LAST_DELIVERY_AT[delivery_key] = time.monotonic()

				await callback.answer(
					url=f"https://t.me/{bot_name}?start=fly_{chat_id}_{message_id}",
					cache_time=0,
				)
			elif act_type == "buy":
				# todo 使用 edit 重新更新媒体, 视 file_type 和 file_id 确定
				await bot.edit_message_media(
					media=InputMediaDocument(
						media=file_id,
						parse_mode="HTML",
						caption=notify_text,
					),
					reply_markup=notify_markup,
					chat_id=callback.from_user.id,
					message_id=message_id,
				)
				source_message_id = callback.data.split(":")[-1]
				remember_peach_exchange_record(source_message_id, reader_user_id)
				PEACH_LAST_DELIVERY_AT[delivery_key] = time.monotonic()


		except TelegramRetryAfter as exc:
			# 闸门已重试仍被限流：回滚桃气值并明确提示限流，避免用户立刻重试加剧 flood
			user_expire_cache.update(reader_user_id, original_expire_timestamp)
			print(f"[TAKEOFF] media delivery rate limited: {exc}", flush=True)
			await callback.answer("⚠️ Telegram 限流中，请稍等几秒再试", show_alert=True, cache_time=0)
			return
		except Exception as exc:
			user_expire_cache.update(reader_user_id, original_expire_timestamp)
			if "Forbidden: bot can't initiate conversation with a user" in str(exc):
				await callback.answer("🤖 请先和新的桃宝机器人私信对话过一次", show_alert=True, cache_time=0)
				return
			print(f"[TAKEOFF] media delivery failed: {exc}", flush=True)
			await callback.answer("❌ 媒体发送失败，请稍后重试", show_alert=True, cache_time=0)
			return

@dp.callback_query(F.data.startswith("backup:peach:link"))
async def on_backup_peach_link(callback: CallbackQuery) -> None:
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return
	message = callback.message

	# print(f"{callback.message.text}")

	if not message:
		await callback.answer("无法获取消息", show_alert=True)
		return


	entities = getattr(message, "entities", None) or []
	url = next((e.url for e in entities if e.type == "text_link" and e.url), "")
	if not url:
		await callback.answer("找不到链接", show_alert=True)
		return

	# 解析 url, 取出 file_type 和 file_id
	file_data = parse_file_url(url)
	uploader_id = file_data.get("uploader_id","")
	file_type = file_data.get("file_type","")
	file_id = file_data.get("file_id","")
	thumb_file_id = file_data.get("thumb_file_id","")

	
	if not file_id:
		await callback.answer("此菊花已失效", show_alert=True)
		return

	reader_user_id = int(callback.from_user.id)
	# print(f"2439 reader_user_id = {reader_user_id}")

	is_admin  = False
	if reader_user_id in ADMIN_USER_IDS:
		is_admin = True



	now = app_now()


	requested_minutes = MEDIA_VIEW_CONSUMPTION_MINUTES
	user_lock = TAKEOFF_USER_LOCKS.setdefault(reader_user_id, asyncio.Lock())

	chat_id = callback.message.chat.id
	message_id = callback.message.message_id

	async with user_lock:
		delivery_key = (reader_user_id, message_id)
		if (
			time.monotonic() - PEACH_LAST_DELIVERY_AT.get(delivery_key, 0.0)
			< PEACH_REDELIVERY_MIN_INTERVAL
		):
			await callback.answer("刚刚已发送过，请稍后再试", show_alert=True, cache_time=0)
			return
		_prune_delivery_records()
		now_timestamp = int(app_now().timestamp())
		user_expire = user_expire_cache.get(reader_user_id)
		# print(f"now_timestamp=>{now_timestamp}")
		# print(f"user_expire=>{user_expire}")
		if (
			not user_expire
			or now_timestamp - user_expire.group_message_timestamp > 24 * 60 * 60
		):

			await callback.answer(
				text=(
					"📢 桃花村广播\n\n"
					"每日至少发言一次才能采菊。"					
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
					
			await callback.answer(
				text=(
					f"目前你的桃气值不足，无法进行采菊。\n\n"
					f"你可以选择在桃花村发言 ( 1 分钟可得 1 桃气值 ) 或是分享资源，就可以获得桃气值。"
				),
				show_alert=True,
				cache_time=0,
			)
			return

		original_expire_timestamp = user_expire.expire_timestamp
		if user_expire_cache.consume_minutes(reader_user_id, requested_minutes) is None:
			await callback.answer("桃气值不足，请重新尝试", show_alert=True, cache_time=0)
			return

		requested_human_time = FormatUtils.minutes_to_day_hour(requested_minutes)[0]


		new_user_expire = user_expire_cache.get(reader_user_id)

		expire_text = FormatUtils.format_timestamp_utc8(new_user_expire.expire_timestamp)

		remaining_minutes = max(
			0,
			(new_user_expire.expire_timestamp - now_timestamp) // 60,
		)

		remaining_text, remaining_view_count = FormatUtils.minutes_to_day_hour(remaining_minutes)

		cover_text = callback.message.text
		cover_text = cover_text.replace('🌼',f'<a href="{url}">🌼</a>')
		# print(f"cover_text=>{cover_text}")

		notify_text = (
			f"{cover_text}\n\n"
			f"✅ 采菊成功，消耗 {requested_minutes // HOURLY_CONSUMPTION_MINUTES} 点桃气值。\n"
			f"🍑 剩余桃气值：{remaining_view_count} / {MAX_HP_CAPACITY_QUANTITY} 点\n"
			f"🕒 预计耗尽：{expire_text}\n"
		)

		notify_keyboard_rows: list[list[InlineKeyboardButton]] = []

		return_url = f"https://t.me/c/{str(chat_id).lstrip('-100')}/{message_id}"

		notify_keyboard_rows.append([
			InlineKeyboardButton(
				text="🔙 返回",
				url=f"{return_url}",
			),
		])

		# notify_keyboard_rows.append([
		# 	InlineKeyboardButton(
		# 		text="👍",
		# 		callback_data=f"click:like:{message_id}",
		# 	),
		# 	InlineKeyboardButton(
		# 		text="👎",
		# 		callback_data=f"click:dislike:{message_id}",
		# 	),
		# ])

		if is_admin:
			source_chat_id = int(callback.message.chat.id)
			source_message_id = int(callback.message.message_id)
			uploader_text = await FormatUtils.get_user_hyperlink(
				bot,
				{"id": uploader_id},
				show_uid=True,
			)
			notify_text += f"\n👤 上传者：{uploader_text}"
			notify_keyboard_rows.extend(
				_build_takeoff_admin_keyboard(
					uploader_id,
					source_chat_id,
					source_message_id,
				)
			)
		notify_markup = (
			InlineKeyboardMarkup(inline_keyboard=notify_keyboard_rows)
			if notify_keyboard_rows
			else None
		)



		try:
			if file_type == "video":
				await _telegram_call_with_retry(
					"deliver peach media (video)",
					lambda: bot.send_video(
						chat_id = callback.from_user.id,
						video =file_id,
						parse_mode="HTML",
						reply_markup=notify_markup,
						caption=notify_text,
					),
					chat_id=callback.from_user.id,
				)
			elif file_type == "document":
				await _telegram_call_with_retry(
					"deliver peach media (document)",
					lambda: bot.send_document(
						chat_id = callback.from_user.id,
						document =file_id,
						parse_mode="HTML",
						reply_markup=notify_markup,
						caption=notify_text,
					),
					chat_id=callback.from_user.id,
				)

			# print(f"send_result: {send_result}")
			# if not send_result.get("ok", False):
				
			# 	await callback.answer("answer_text", show_alert=True, cache_time=0)
			# 	return

			# user_expire_cache.extend_minutes(
			# 	reader_user_id,
			# 	MEDIA_VIEW_CONSUMPTION_MINUTES,
			# )

			remember_peach_exchange_record(message_id, reader_user_id)
			PEACH_LAST_DELIVERY_AT[delivery_key] = time.monotonic()

			await callback.answer(
				url=f"https://t.me/{bot_name}?start=fly_{chat_id}_{message_id}",
				cache_time=0,
			)


		except TelegramRetryAfter as exc:
			# 闸门已重试仍被限流：回滚桃气值并明确提示限流，避免用户立刻重试加剧 flood
			user_expire_cache.update(reader_user_id, original_expire_timestamp)
			print(f"[TAKEOFF] media delivery rate limited: {exc}", flush=True)
			await callback.answer("⚠️ Telegram 限流中，请稍等几秒再试", show_alert=True, cache_time=0)
			return
		except Exception as exc:
			user_expire_cache.update(reader_user_id, original_expire_timestamp)
			if "Forbidden: bot can't initiate conversation with a user" in str(exc):
				await callback.answer("🤖 请先和新的桃宝机器人私信对话过一次", show_alert=True, cache_time=0)
				return
			print(f"[TAKEOFF] media delivery failed: {exc}", flush=True)
			await callback.answer("❌ 媒体发送失败，请稍后重试", show_alert=True, cache_time=0)
			return



# 删除「XXX 置顶了一条消息」服务消息
@dp.message(
	F.chat.id.in_({CHAT_SCHOOL_GROUP_ID, CHAT_PUBLIC_GROUP_ID}),
	F.pinned_message
)
async def delete_pin_service_message(message: Message) -> None:
	if message.pinned_message:
		await message.delete()
		return
	

@dp.message(F.chat.id.in_({CHAT_SCHOOL_GROUP_ID, CHAT_PUBLIC_GROUP_ID}), F.text)
async def on_reward_group_message(message: Message) -> None:

	
	if not message.from_user or message.from_user.is_bot:
		return
	text = (message.text or "").strip()
	if len(text) < 2 or text.startswith("/"):
		return

	if any(ignored_text in text for ignored_text in IGNORED_TEXT_SUBSTRINGS):
		return


	user_id = int(message.from_user.id)
	now_timestamp = int(app_now().timestamp())
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
		MESSAGE_REWARD_MINUTES,
		group_message_timestamp=now_timestamp,
	)
	actual_added_minutes = max(
		0,
		(user_expire.expire_timestamp - base_timestamp) // 60,
	)
	# print(
	# 	f"[MESSAGE_REWARD] user {user_id} granted "
	# 	f"{actual_added_minutes}/{MESSAGE_REWARD_MINUTES} minutes",
	# 	flush=True,
	# )


@dp.chat_join_request()
async def on_join_request(join_request: ChatJoinRequest) -> None:
	if join_request.chat.id not in {CHAT_PUBLIC_GROUP_ID, CHAT_SCHOOL_GROUP_ID}:
		print(f"[JOIN_REQUEST] ignored join request from chat {join_request.chat.id}", flush=True)
		return
	print(f"[JOIN_REQUEST] {join_request.from_user.id}", flush=True)
	try:
		now_timestamp = int(app_now().timestamp())
		user_expire = user_expire_cache.get(int(join_request.from_user.id))
		if not user_expire or user_expire.expire_timestamp <= now_timestamp:

			text = f"🚧 为避免坏份子混入桃花村，入村前请先交一份「投名状」——传送一份正太资源（文件或视频）给我，确认你我是否是同路人。\n\n📤 传送完成后，再重新申请加入群组。\n\n🍑 确认是同路人，方可入村。"
			try:
				await _telegram_call_with_retry(
					"join request notice dm",
					lambda: bot.send_message(chat_id=join_request.from_user.id, text=text),
					chat_id=join_request.from_user.id,
				)
			except Exception as exc:
				# DM 失败（含限流）不阻断拒绝流程，避免申请悬挂
				print(f"[JOIN_REQUEST] dm failed for {join_request.from_user.id}: {exc}", flush=True)
			await join_request.decline()

			return
		# 检查申请者的桃气值是否逾期或不存在
		
		await join_request.approve()
			
	except Exception as exc:
		print(f"[JOIN_REQUEST] failed to approve join request: {exc}", flush=True)


'''
Command
'''



@dp.message(F.chat.type == "private", Command("start"))
async def cmd_start(message: Message, command: CommandObject) -> None:
	args = str(command.args or "").strip()

	if "fly_" in args:
		try: 
			await message.delete()
		except Exception as exc:
			print(f"[START] failed to fly: {exc}", flush=True)
		
		return

	else:			
		return



@dp.message(F.chat.type == "private", Command("admin"))
async def cmd_admin(message: Message, command: CommandObject) -> None:
	if not UserManager._is_admin_message(message, ADMIN_USER_IDS):
		await message.reply("❌ 无效指令")
		return

	lines = [
		"🛠️ 管理员命令总览",
		"",
		"/me — 查看当前飞行通行证状态与剩余可请求数量",
		"/ban [用户id|回复用户] [原因] — 封禁用户并从群组移除",
		"/unban [用户id] — 解除封禁",		
		"/baninfo [用户id] — 查看黑名单资料",
		"/banlist [页码] — 查看黑名单列表",		
		"/userinfo [用户id] — 查询用户时限及黑名单状态",		
		"/rule — 查看机场规则与奖励机制",		
		"/start — 进入入口流程",		
		"/admin — 查看管理员命令说明",
	]
	await message.reply("\n".join(lines))

async def get_user_status(from_user_id):
	now_timestamp = int(app_now().timestamp())
	user_expire = user_expire_cache.get(int(from_user_id))
	if not user_expire or user_expire.expire_timestamp <= now_timestamp:
		status_text = (
			"<blockquote>📊 村民状态</blockquote>\n"
			"状态：目前没有桃气\n"
			"你可以在桃花村发言或分享资源来增加桃气值。"
		)
		return status_text

	remaining_seconds = user_expire.expire_timestamp - now_timestamp
	remaining_minutes = remaining_seconds // 60
	available_view_count = remaining_minutes // MEDIA_VIEW_CONSUMPTION_MINUTES
	expire_text = FormatUtils.format_timestamp_utc8(user_expire.expire_timestamp)
	hp_bar = FormatUtils.hp_bar(available_view_count, MAX_HP_CAPACITY_QUANTITY)
	status_text = (
		"<blockquote>📊 村民状态</blockquote>\n\n"
		f"🍑 桃气值：{available_view_count} / {MAX_HP_CAPACITY_QUANTITY} \n"
		f"{hp_bar}\n\n"
		f"⏳ 可维持：{FormatUtils.format_duration(remaining_seconds)}\n"
		f"🕒 预计耗尽：{expire_text}\n\n"
		f"🎈 <i>桃气值不足不能采菊，但不会被逐出桃花村</i>"
		
	)
	return status_text
@dp.message(F.chat.type == "private", Command("me"))
async def cmd_me(message: Message) -> None:
	if not message.from_user:
		return
	status_text = await get_user_status(message.from_user.id)
	await message.reply(status_text, parse_mode="HTML")
		


# 同时监听 /home 和 /rule 命令
@dp.message(F.chat.type == "private", Command("home", "rule"))
async def cmd_rule(message: Message) -> None:
	
	media_upload_extend_text = FormatUtils.minutes_to_day_hour(MEDIA_UPLOAD_EXTEND_MINUTES)[0]

	view_cost_text = FormatUtils.minutes_to_day_hour(MEDIA_VIEW_CONSUMPTION_MINUTES)[0]
	message_extend_text = FormatUtils.minutes_to_day_hour(MESSAGE_REWARD_MINUTES)[0]
	max_duration_text = FormatUtils.minutes_to_day_hour(MAX_HP_CAPACITY_MINUTES)[0]

	reply_markup = InlineKeyboardMarkup(
		inline_keyboard=[
			[
				InlineKeyboardButton(
					text="🌸 加入桃花林",
					url=(
						f"{CHAT_PUBLIC_LINK}"
					),
				)
			]
		]
	)


	await message.reply(
		"🌸 桃花村的传说与生存法则\n\n"
		"<i>传说，在群山深处有一座神奇的桃花村。村中天地灵气汇聚，孕育出一种特殊的能量——「桃气值」。\n\n"
		"桃气值是村民在桃花村生活、交流与探索的重要能量。村民可以通过日常交流与分享资源获得桃气值，而查看其他村民分享的资源，以及维持日常生活，都需要消耗桃气值。\n\n"
		"为了维持桃花村的秩序与能量平衡，村中流传着以下法则。</i>\n\n"

		"<blockquote expandable>🌸 一、共鸣生息\n"
		"桃花村中生长着一棵神奇的「共鸣树」。村民之间的发言交流，会让共鸣树感受到村庄的生机与活力，并将这份共鸣转化为桃气值。\n"
		"村民在桃花村（桃花林和桃花源）进行符合条件的交流，即可获得桃气值。</blockquote>"

		f"✨ 每次有效交流，可获得 {MESSAGE_REWARD_QUANTITY} 点桃气值。\n"
		"每分钟最多计算一次有效交流。重复刷屏、无意义的消息或不符合条件的内容，不会触发共鸣。\n\n"
		
		"<blockquote expandable>🌿 二、灵韵馈赠\n"
		"桃花村鼓励村民分享有价值的视频与文件，让更多村民能够发现和探索不同的内容。\n"
		"每当村民成功分享符合条件的资源，共鸣树便会感知到新的灵韵，并将部分灵韵转化为桃气值，作为对分享者的馈赠。</blockquote>"
		
		f"🎁 每次成功分享资源，可获 {MEDIA_UPLOAD_EXTEND_QUANTITY} 点桃气值。\n"
		"重复分享相同资源或发送无效文件，不会重复获得奖励。\n\n"
		
		"<blockquote expandable>🌼 三、采菊寻芳\n"
		"在桃花村，「采菊」是查看其他村民已经分享的视频或文件的专用说法。每一份资源都像藏在桃花林中的一朵菊花，等待有缘的村民前来发现。\n"
		"「采菊」探秘需要消耗桃气值。村民可以根据自己的桃气储量，决定何时采菊，以及探索多少资源。</blockquote>"
		f"🔍 每次采菊，需要消耗 {MEDIA_VIEW_CONSUMPTION_QUANTITY} 点桃气值。\n"
		"当桃气值不足时，将无法继续采菊。\n\n"

		"<blockquote expandable>⏳ 四、岁时流转\n"
		"桃花村遵循着独有的天地运行法则。日月更替，四时流转，村民即使没有进行其他活动，也需要定期消耗桃气值，以维持日常生活所需的能量。</blockquote>"
		f"⏰ 每小时需要消耗 {HOURLY_CONSUMPTION_QUANTITY} 点桃气值。\n\n"
		

		"<blockquote expandable>🌀 五、桃气上限\n"
		"桃花村的天地法则规定，每位村民能够累积的桃气值都有一定上限。桃气上限决定了村民最多能够持有多少桃气值，是规划日常活动与资源获取的重要依据。</blockquote>"
		f"📊 桃气上限：最多累积 {MAX_HP_CAPACITY_QUANTITY} 点桃气值。\n"
		"当桃气值达到上限后，超出上限的部分将不再累加。即使继续触发共鸣或分享资源，也无法使桃气值超过规定的上限。\n\n"
		

		"<blockquote>📜 六、桃花村生存指南</blockquote>\n"
		f"🌸 共鸣生息：每次有效交流，获得 {MESSAGE_REWARD_QUANTITY} 点桃气值。\n"
		f"🌿 灵韵馈赠：每次成功分享资源，获得 {MEDIA_UPLOAD_EXTEND_QUANTITY} 点桃气值。\n"
		f"🌼 采菊寻芳：每次查看资源，消耗 {MEDIA_VIEW_CONSUMPTION_QUANTITY} 点桃气值。\n"
		f"⏳ 岁时流转：每小时消耗 {HOURLY_CONSUMPTION_QUANTITY} 点桃气值。\n"
		f"🌀 桃气上限：最多累积 {MAX_HP_CAPACITY_QUANTITY} 点桃气值。\n\n"

		"<b>🌸 欢迎来到桃花村！在这里，交流能够引动共鸣，分享能够获得灵韵，而探索资源与日常生活都需要消耗桃气值。遵循村庄的天地法则，合理规划桃气值的获取与使用，才能在桃花村中持续探索，发现更多精彩内容。</b>",
		parse_mode="HTML",
		reply_markup=reply_markup,
	)


@dp.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject) -> None:
	if not UserManager._is_admin_message(message, ADMIN_USER_IDS):
		return

	args = str(command.args or "").strip()
	parts = args.split(maxsplit=1)
	explicit_user_id = ParseUtils._parse_positive_user_id(parts[0]) if parts else None

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
		target_chats=TARGET_CHATS,
	)

	reply_text = f"✅ 已加入黑名单\n{_format_blacklist_entry(entry)}"
	if group_ban_error:
		reply_text += f"\n\n⚠️ 群组移除失败：{group_ban_error}"
	else:
		reply_text += "\n\n✅ 已从群组移除并禁止重新加入"
	await message.reply(reply_text)

@dp.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject) -> None:
	if not UserManager._is_admin_message(message, ADMIN_USER_IDS):
		return

	target_user_id = ParseUtils._parse_positive_user_id(str(command.args or ""))
	if target_user_id is None:
		await message.reply("用法：/unban [用户id]")
		return

	if not blacklist_store.is_blocked(target_user_id):
		await message.reply(f"ℹ️ 用户不在黑名单中：{target_user_id}")
		return
	group_unban_error = await _unban_user(target_user_id, target_chats=TARGET_CHATS)
	if group_unban_error:
		await message.reply(f"❌ 群组解除封禁失败：{group_unban_error}")
		return

	blacklist_store.unban(target_user_id)
	await message.reply(
		f"✅ 已从黑名单移除并解除群组封禁：{target_user_id}"
	)


@dp.message(Command("banlist"))
async def cmd_banlist(message: Message, command: CommandObject) -> None:
	if not UserManager._is_admin_message(message, ADMIN_USER_IDS):
		return

	page_text = str(command.args or "").strip()
	page = ParseUtils._parse_positive_user_id(page_text) if page_text else 1
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
		expiry_text = (
			FormatUtils.format_timestamp_utc8(entry.expires_at)
			if entry.expires_at > 0
			else "永久"
		)
		lines.append(
			f"{entry.user_id}｜{entry.reason}｜"
			f"{FormatUtils.format_timestamp_utc8(entry.created_at)}｜到期：{expiry_text}"
		)
	await message.reply("\n".join(lines))



@dp.message(F.chat.type == "private", Command("userinfo"))
async def cmd_userinfo(message: Message, command: CommandObject) -> None:
	if not UserManager._is_admin_message(message, ADMIN_USER_IDS):
		await message.reply("❌ 无效指令")
		return

	target_user_id = ParseUtils._parse_positive_user_id(str(command.args or ""))
	if target_user_id is None:
		await message.reply("用法：/userinfo [用户id]")
		return

	now_timestamp = int(app_now().timestamp())
	user_expire = user_expire_cache.get(target_user_id)
	lines = [
		"👤 用户资料",
		f"用户 ID：{target_user_id}",
		"",
		"🎫 飞行通行证",
	]

	if user_expire is None:
		lines.extend([
			"状态：❌ 无记录",			
		])
	else:
		remaining_seconds = user_expire.expire_timestamp - now_timestamp
		if remaining_seconds > 0:
			remaining_minutes = remaining_seconds // 60
			available_view_count = remaining_minutes // MEDIA_VIEW_CONSUMPTION_MINUTES
			lines.extend([
				"状态：✅ 有效",
				f"剩余时限：{FormatUtils.format_duration(remaining_seconds)}",
				f"目前可请求：{available_view_count} 个资源\n",
				
			])
		else:
			lines.extend([
				"状态：⌛ 已过期",
				f"已过期：{FormatUtils.format_duration(abs(remaining_seconds))}",
			])
		lines.extend([
			f"到期时间：{FormatUtils.format_timestamp_utc8(user_expire.expire_timestamp)}",
			f"资料更新时间：{FormatUtils.format_timestamp_utc8(user_expire.update_timestamp)}",
			(
				f"最近有效发言：{FormatUtils.format_timestamp_utc8(user_expire.group_message_timestamp)}"
				if user_expire.group_message_timestamp > 0
				else "最近有效发言：无记录"
			),
		])

	blacklist_entry = blacklist_store.get(target_user_id)
	lines.extend(["", "🚫 黑名单"])
	if blacklist_entry is None:
		lines.append("状态：✅ 不在黑名单中")
	else:
		lines.extend([
			"状态：⛔ 已列入黑名单",
			f"封禁原因：{blacklist_entry.reason}",
			f"操作管理员：{blacklist_entry.created_by}",
			f"封禁时间：{FormatUtils.format_timestamp_utc8(blacklist_entry.created_at)}",
			(
				f"到期时间：{FormatUtils.format_timestamp_utc8(blacklist_entry.expires_at)}"
				if blacklist_entry.expires_at > 0
				else "到期时间：永久"
			),
		])

	await message.reply("\n".join(lines))

# 常见良性 BadRequest：重复编辑同一内容、查询过期、消息已不存在等，静默处理即可
BENIGN_BAD_REQUEST_MARKERS = (
	"message is not modified",
	"query is too old",
	"message can't be deleted",
	"message to delete not found",
	"message not found",
	"message_id_invalid",
)


@dp.errors()
async def on_telegram_error(event: ErrorEvent) -> None:
	"""全局错误兜底：限流提示、良性错误静默，并保证 callback 不悬空（按钮不转圈）。"""
	exception = event.exception
	update = event.update
	callback = update.callback_query if update else None

	if isinstance(exception, TelegramRetryAfter):
		retry_after = int(getattr(exception, "retry_after", 0) or 0)
		print(
			f"[TELEGRAM_RATE_LIMIT] unhandled flood error, "
			f"retry_after={retry_after}s: {exception}",
			flush=True,
		)
		if callback is not None:
			try:
				await callback.answer("⚠️ 操作太频繁，请稍后再试", show_alert=True, cache_time=0)
			except Exception:
				pass
		return

	if isinstance(exception, TelegramBadRequest):
		error_text = str(exception).lower()
		if any(marker in error_text for marker in BENIGN_BAD_REQUEST_MARKERS):
			return

	print(f"[TG_UPDATE_ERROR] {type(exception).__name__}: {exception}", flush=True)
	if callback is not None:
		try:
			await callback.answer("❌ 操作失败，请稍后重试", show_alert=True, cache_time=0)
		except Exception:
			pass


async def main() -> None:
	global bot_name
	me = await bot.get_me()
	bot_name = str(getattr(me, "username", "") or "")
	print(f"🤖 Bot started as @{bot_name}", flush=True)

	await say_hello_to_x_man(bot_name)
	await _check_bot_group_admin_permissions()


	await bot.set_my_commands(
		[
			BotCommand(command="me", description="查询我的信息"),
			BotCommand(command="rule", description="查看村规"),
			BotCommand(command="home", description="回村"),
		],
		scope=BotCommandScopeAllPrivateChats(),
	)

	try:
		await dp.start_polling(bot)
	finally:
		await bot.session.close()


if __name__ == "__main__":
	asyncio.run(main())