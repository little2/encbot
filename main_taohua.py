
import asyncio
import html
import os
import re

from aiogram import Dispatcher, Bot, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, CallbackQuery, ChatJoinRequest, CopyTextButton, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.types import ReplyKeyboardRemove

from utils.format_utils import FormatUtils
from utils.parse_utils import ParseUtils
from utils.user_manager import UserManager
from utils.blacklist_utils import BlacklistEntry, BlacklistStore
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

PEACH_CHANNEL_ID = os.getenv("PEACH_CHANNEL_ID")

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


if not BOT_TOKEN:
	raise RuntimeError("Missing bot token. Please set ENCBOT_TOKEN or BOT_TOKEN.")

from utils.time_utils import app_now

bot = Bot(
	token=BOT_TOKEN,
	default=DefaultBotProperties(link_preview_is_disabled=True),
)
dp = Dispatcher()



ENCODED_FORWARD_SEND_LOCK = asyncio.Lock()
INACTIVE_CLEANUP_LOCK = asyncio.Lock()
async def _telegram_call_with_retry(
	label: str,
	operation,
	max_attempts: int = 4,
	on_retry=None,
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
				if on_retry is not None:
					try:
						await on_retry(attempt + 1, max_attempts, delay, exc)
					except Exception as callback_exc:
						print(f"[TELEGRAM_RETRY_STATUS] {label}: {callback_exc}", flush=True)
				await asyncio.sleep(delay)
			except TelegramNetworkError as exc:
				if attempt + 1 >= max_attempts:
					raise

				delay = min(2 ** (attempt + 1), 10)
				print(
					f"[TELEGRAM_NETWORK] {label}: {exc}; "
					f"retry in {delay}s ({attempt + 1}/{max_attempts})",
					flush=True,
				)
				if on_retry is not None:
					try:
						await on_retry(attempt + 1, max_attempts, delay, exc)
					except Exception as callback_exc:
						print(f"[TELEGRAM_RETRY_STATUS] {label}: {callback_exc}", flush=True)
				await asyncio.sleep(delay)


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
	prompt = await message.reply(
		text,
		parse_mode=ParseMode.HTML,
		reply_markup=ForceReply(
			force_reply=True,
			input_field_placeholder=f"输入介绍内容（{INTRO_MIN_LEN}~{INTRO_MAX_LEN} 字）",
		),
	)
	await state.update_data(prompt_message_id=prompt.message_id)





def _extract_media_dict(message: Message) -> dict:
	file_type = None
	caption = None
	file_id = None
	file_name = None

	print(f"Extracting media from message: {message}")
	
	if message.video:
		file_type = "video"
		file_id = message.video.file_id
	elif message.document:
		mime_type = str(message.document.mime_type or "").lower()
		file_type = "video" if mime_type == "video/mp4" else "document"
		file_id = message.document.file_id

	file_name = str((message.video or message.document).file_name or "").strip()

	
	if message.caption:
		caption = message.caption.strip()
		if EmojiUtils._extract_consecutive_emojis(caption):
			print(f"Caption contains consecutive emojis: {caption}")
			caption = EmojiUtils.strip_consecutive_emojis(caption)
			print(f"Caption cleaned: {caption}")

	return {"file_type": file_type, "caption": caption, "file_id": file_id, "file_name": file_name}

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

		

	had_pending = await state.get_state() == IntroStates.waiting_intro.state
	await state.set_state(IntroStates.waiting_intro)
	await state.set_data(
		{
			"media_message_id": message.message_id,
			"file_type": file_type,
			"file_id": file_id,
			"uploader_id": int(message.from_user.id),
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



		await bot.send_message(
			message.chat.id,
			f"<a href=\"https://t.me/{bot_name}?text={html.escape(chinese_name)}\">{html.escape(chinese_name)}</a>\n\n",
			parse_mode="HTML",
			reply_markup=InlineKeyboardMarkup(
				inline_keyboard=[
					[
						InlineKeyboardButton(
							text="📋 复制",
							copy_text=CopyTextButton(text=f"{html.escape(chinese_name)}")
							
						)
					]
				]
			)
		)

		lines.append(
			"请回复此消息并输入介绍内容（2~100 字）。"
		)
	else:
		lines.append("请回复此消息并自行输入介绍内容（2~100 字）。")

	await _ask_intro(message, state, "\n\n".join(lines))


def encode_file_url(data: dict) -> str:
	return f"https://peach.{data['file_type']}/{data['uploader_id']}/{data['file_id']}"

def parse_file_url(url: str) -> dict:
	parts = url.split("/")
	if len(parts) < 5:
		return {}
	file_type = parts[2].split(".")[-1]
	uploader_id = parts[3]
	file_id = parts[4]
	return {"file_type": file_type, "uploader_id": uploader_id, "file_id": file_id}

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
	url = encode_file_url(data)
	
	await bot.send_message(
		CHAT_PUBLIC_GROUP_ID,
		f'<a href="{html.escape(url, quote=True)}">🌼</a> {html.escape(text)}',
		message_thread_id=CHAT_PUBLIC_THREAD_ID or None,
		parse_mode=ParseMode.HTML,
		reply_markup=InlineKeyboardMarkup(
			inline_keyboard=[[InlineKeyboardButton(text="🍑", callback_data="peach:link")]]
		),
	)



	if data['file_type'] == "video":
		send_result = await bot.send_video(
			chat_id = PEACH_CHANNEL_ID,
			video =data['file_id'],
			parse_mode="HTML",					
			caption=f"{html.escape(text)}",
		)
	elif data['file_type'] == "document":
		send_result = await bot.send_document(
			chat_id = PEACH_CHANNEL_ID,
			document =data['file_id'],
			parse_mode="HTML",					
			caption=f"{html.escape(text)}",
		)


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



		
		await bot.send_message(
			chat_id=from_user_id,
			text=notify_text,
			reply_markup=ReplyKeyboardRemove()
			
		)

		print(
			f"[ENCODED_FORWARD] granted {actual_added_minutes}/{requested_minutes} "
			f"minutes to user {from_user_id}",
			flush=True,
		)
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




@dp.callback_query(F.data.startswith("peach:link"))
async def on_peach_link(callback: CallbackQuery) -> None:
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return
	message = callback.message

	print(f"{callback.message.text}")

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


		notify_text = (
			f"{callback.message.text}\n\n"
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
				send_result = await bot.send_video(
					chat_id = callback.from_user.id,
					video =file_id,
					parse_mode="HTML",					
					reply_markup=notify_markup,
					caption=notify_text,
				)
			elif file_type == "document":
				send_result = await bot.send_document(
					chat_id = callback.from_user.id,
					document =file_id,
					parse_mode="HTML",					
					reply_markup=notify_markup,
					caption=notify_text,
				)

			# print(f"send_result: {send_result}")
			# if not send_result.get("ok", False):
				
			# 	await callback.answer("answer_text", show_alert=True, cache_time=0)
			# 	return

			# user_expire_cache.extend_minutes(
			# 	reader_user_id,
			# 	MEDIA_VIEW_CONSUMPTION_MINUTES,
			# )

			await callback.answer(
				url=f"https://t.me/{bot_name}?start=fly_{chat_id}_{message_id}",
				cache_time=0,
			)


		except Exception as exc:
			user_expire_cache.update(reader_user_id, original_expire_timestamp)
			print(f"[TAKEOFF] media delivery failed: {exc}", flush=True)
			await callback.answer("❌ 媒体发送失败，请稍后重试", show_alert=True, cache_time=0)
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
	print(
		f"[MESSAGE_REWARD] user {user_id} granted "
		f"{actual_added_minutes}/{MESSAGE_REWARD_MINUTES} minutes",
		flush=True,
	)


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
			await bot.send_message(chat_id=join_request.from_user.id, text=text)
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


	try:
		if args != "":
			await message.delete()
	except Exception as exc:
		print(f"[START] failed to delete parameterized command: {exc}", flush=True)


	if "fly_" in args:
		await message.delete()
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
			"<blockquote>📊 村民状态</blockquote>\n\n"
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
		f"🕒 预计耗尽：{expire_text}"
		
	)
	return status_text
@dp.message(F.chat.type == "private", Command("me"))
async def cmd_me(message: Message) -> None:
	if not message.from_user:
		return
	status_text = await get_user_status(message.from_user.id)
	await message.reply(status_text)
		


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
				f"目前可请求：{available_view_count} 个资源",
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