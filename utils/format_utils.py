from datetime import datetime
from html import escape
from typing import Union

from aiogram import Bot
from aiogram.types import User

from config import MEDIA_VIEW_CONSUMPTION_MINUTES
from utils.time_utils import APP_TIMEZONE, app_fromtimestamp


class FormatUtils:
	@staticmethod
	def format_duration(seconds: int) -> str:
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

	@staticmethod
	def format_datetime_utc8(value: datetime) -> str:
		if value.tzinfo is None:
			value = value.replace(tzinfo=APP_TIMEZONE)
		return value.astimezone(APP_TIMEZONE).strftime("%m-%d %H:%M")

	@staticmethod
	def format_timestamp_utc8(timestamp: int) -> str:
		return FormatUtils.format_datetime_utc8(app_fromtimestamp(timestamp))

	@staticmethod
	def minutes_to_day_hour(minutes: int):
		minutes = max(0, int(minutes))
		total_hours = minutes // 60
		days = total_hours // 24
		hours = total_hours % 24
		remaining_minutes = minutes % 60
		view_count = minutes // MEDIA_VIEW_CONSUMPTION_MINUTES

		parts = []
		if days:
			parts.append(f"{days} 天")
		if hours:
			parts.append(f"{hours} 小时")
		if remaining_minutes or not parts:
			parts.append(f"{remaining_minutes} 分钟")
		text = " ".join(parts)

		return text, view_count

	@staticmethod
	async def get_user_hyperlink(
		bot: Bot,
		user_info: Union[dict, User],
		show_uid: bool = False,
	) -> str:
		"""取得 Telegram User hyperlink；没有 first_name 时会透过 Telegram API 查询。"""
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

		if not first_name.strip() and user_id:
			user = await bot.get_chat(user_id)
			first_name = user.first_name or ""
			last_name = user.last_name or ""
			username = user.username
			user_id = user.id

		user_title = first_name
		if last_name:
			user_title += f" {last_name}"
		if not user_title.strip():
			user_title = str(user_id)
		user_title = escape(user_title)

		if username:
			text = f"<a href='https://t.me/{username}'>{user_title}</a>"
		else:
			text = f"<a href='tg://user?id={user_id}'>{user_title}</a>"

		if show_uid:
			text += f" <code>{user_id}</code>"

		return text
