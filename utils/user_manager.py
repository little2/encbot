from typing import Any, Awaitable, Callable

from aiogram import Bot
from aiogram.types import Message


from utils.blacklist_utils import BlacklistEntry, BlacklistStore
from utils.user_utils import UserExpireCache


class UserManager:
	"""机场用户管理：封禁、解封、清理不活跃用户。"""

	def __init__(
		self,
		*,
		bot: Bot,
		blacklist_store: BlacklistStore,
		user_expire_cache: UserExpireCache,
		telegram_call: Callable[..., Awaitable[Any]],
		lobby_group_id: int,
		terminal_channel_id: int,
		duty_free_group_id: int,
		flight_board_channel_id: int,
		user_state_stores: tuple[dict, ...] = (),
	) -> None:
		self._bot = bot
		self._blacklist_store = blacklist_store
		self._user_expire_cache = user_expire_cache
		self._telegram_call = telegram_call
		self._lobby_group_id = lobby_group_id
		self._terminal_channel_id = terminal_channel_id
		self._duty_free_group_id = duty_free_group_id
		self._flight_board_channel_id = flight_board_channel_id
		# 以 user_id 为键的内存状态，清理用户时一并移除。
		self._user_state_stores = user_state_stores

	@staticmethod
	def is_current_chat_member(status: Any) -> bool:
		return (
			status.status in ("member", "administrator", "creator")
			or (status.status == "restricted" and status.is_member is True)
		)

	@staticmethod
	def _is_admin_message(message: Message, admin_user_ids: set[int]) -> bool:
		return bool(
			message.from_user
			and int(message.from_user.id) in admin_user_ids
		)


	@staticmethod
	def _is_participant_id_error(exc: Exception) -> bool:
		return "PARTICIPANT_ID" in str(exc).upper()

	async def ban_user(
		self,
		user_id: int,
		reason: str,
		created_by: int,
		expires_at: int = 0,
		target_chats: list[tuple[str, int]] | None = None,
	) -> tuple[BlacklistEntry, str]:
		entry = self._blacklist_store.ban(user_id, reason, created_by, expires_at)
		self._user_expire_cache.remove(user_id)
		if target_chats is None:
			target_chats = [
				("AIRPORT_LOBBY_GROUP_ID", self._lobby_group_id),
				("TERMINAL_CHANNEL_ID", self._terminal_channel_id),
				("AIRPORT_DUTY_FREE_GROUP_ID", self._duty_free_group_id),
				("AIRPORT_FLIGHT_BOARD_CHANNEL_ID", self._flight_board_channel_id),
			]
		configured_chat_ids = [chat_id for _, chat_id in target_chats if chat_id != 0]
		if not configured_chat_ids:
			return entry, "机场群组尚未配置"

		errors: list[str] = []

		for chat_name, chat_id in target_chats:
			if chat_id == 0:
				continue
			try:
				await self._telegram_call(
					f"ban member from {chat_name}",
					lambda chat_id=chat_id: self._bot.ban_chat_member(
						chat_id=chat_id,
						user_id=user_id,
						until_date=expires_at or None,
					),
				)
			except Exception as exc:
				print(
					f"[BLACKLIST] failed to ban user {user_id} "
					f"from {chat_name} ({chat_id}): {exc}",
					flush=True,
				)
				errors.append(f"{chat_name} {chat_id} {str(exc)}")

		return entry, "；\n".join(errors)

	async def unban_user(self, user_id: int, target_chats: list[tuple[str, int]] | None = None) -> str:
		errors: list[str] = []

		if target_chats is None:
			target_chats = [
				("航站大厅", self._lobby_group_id),
				("镇泰飞机场", self._terminal_channel_id),
				("机场免税店", self._duty_free_group_id),
				("机场航班看板", self._flight_board_channel_id),
			]

		for chat_name, chat_id in target_chats:
			if chat_id == 0:
				continue
			try:
				await self._telegram_call(
					f"unban {chat_name} member",
					lambda chat_id=chat_id: self._bot.unban_chat_member(
						chat_id=chat_id,
						user_id=user_id,
						only_if_banned=True,
					),
				)
			except Exception as exc:
				print(
					f"[BLACKLIST] failed to unban user {user_id} "
					f"from {chat_name} ({chat_id}): {exc}",
					flush=True,
				)
				errors.append(f"{chat_name}：{exc}")
		return "；".join(errors)

	async def remove_inactive_user_from_chat(
		self,
		chat_id: int,
		user_id: int,
		chat_name: str,
	) -> tuple[bool, str, bool]:
		if chat_id == 0:
			return False, f"{chat_name}未配置", False

		try:
			status = await self._telegram_call(
				f"lookup inactive member {user_id} in {chat_id}",
				lambda: self._bot.get_chat_member(chat_id=chat_id, user_id=user_id),
			)
		except Exception as exc:
			if self._is_participant_id_error(exc):
				return False, f"{chat_name}找不到成员：PARTICIPANT_ID", True
			return False, f"{chat_name}成员状态查询失败：{exc}", False

		status_name = str(getattr(status, "status", "unknown"))
		if status_name == "kicked":
			try:
				await self._telegram_call(
					f"unban inactive member {user_id} from {chat_id}",
					lambda: self._bot.unban_chat_member(
						chat_id=chat_id,
						user_id=user_id,
						only_if_banned=True,
					),
				)
			except Exception as exc:
				if self._is_participant_id_error(exc):
					return False, f"{chat_name}找不到成员：PARTICIPANT_ID", True
				return False, f"{chat_name}解除封禁失败：{exc}", False
			return True, f"{chat_name}已解除旧封禁", False

		if not self.is_current_chat_member(status):
			return True, f"{chat_name}原本不在群内", False

		try:
			await self._telegram_call(
				f"remove inactive member {user_id} from {chat_id}",
				lambda: self._bot.ban_chat_member(chat_id=chat_id, user_id=user_id),
			)
			await self._telegram_call(
				f"unban removed inactive member {user_id} from {chat_id}",
				lambda: self._bot.unban_chat_member(
					chat_id=chat_id,
					user_id=user_id,
					only_if_banned=True,
				),
			)
		except Exception as exc:
			if self._is_participant_id_error(exc):
				return False, f"{chat_name}找不到成员：PARTICIPANT_ID", True
			return False, f"{chat_name}移出失败：{exc}", False
		return True, f"{chat_name}已移出", False

	def delete_inactive_user_data(self, user_id: int) -> None:
		self._user_expire_cache.remove(user_id)
		for store in self._user_state_stores:
			store.pop(user_id, None)
