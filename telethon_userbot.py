"""Optional user-account listener sharing the application's asyncio loop."""
from __future__ import annotations

import asyncio
import getpass
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient, errors, events, functions, types, utils
from telethon.sessions import StringSession
from tgfileid import TelegramFileId
from utils.chat_membership import BotKickedError

logger = logging.getLogger(__name__)


class TelethonUserbot:
    def __init__(self, api_id: int, api_hash: str, session: str,
                 on_message: Callable[..., Awaitable[None]], phone: str = ""):
        resolved_session = self._resolve_session(session)
        self.client = TelegramClient(resolved_session, api_id, api_hash)
        self.phone = phone
        self.user_id: int | None = None
        self.account_user_id: int | None = None
        self.on_message = on_message
        self._upload_peer = None
        self.client.add_event_handler(self._handle_message, events.NewMessage(incoming=True))

    @staticmethod
    def _resolve_session(session: str):
        """Accept either a serialized StringSession or a SQLite session path."""
        session_value = str(session or "").strip()
        if not session_value:
            raise ValueError("USERBOT_SESSION must not be empty")

        is_explicit_path = (
            "/" in session_value
            or "\\" in session_value
            or session_value.endswith(".session")
        )
        if not is_explicit_path:
            try:
                return StringSession(session_value)
            except ValueError:
                pass

        session_path = Path(session_value).expanduser()
        session_path.parent.mkdir(parents=True, exist_ok=True)
        return str(session_path)

    @classmethod
    def from_env(cls, on_message: Callable[..., Awaitable[None]]) -> TelethonUserbot:
        api_id = int(os.environ["API_ID"])
        api_hash = os.environ["API_HASH"].strip()
        if api_id <= 0 or not api_hash:
            raise ValueError("API_ID and API_HASH must be configured")
        session = os.getenv("USERBOT_SESSION", str(Path(__file__).parent / "data" / "userbot"))
        userbot_phone = os.getenv("USERBOT_PHONE", "")
        
        return cls(api_id, api_hash, session, on_message, userbot_phone)

    @staticmethod
    def get_media_identity(event) -> tuple[str, int] | None:
        media = getattr(event.message, "media", None)
        if isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
            return "document", media.document.id
        if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
            return "photo", media.photo.id
        return None

    async def _handle_message(self, event):
        try:
            await self.on_message(event)
        except Exception:
            logger.exception("Userbot message callback failed (chat=%s, message=%s)",
                             event.chat_id, event.id)

    async def _resolve_chat(self, chat_id: int, username: str | None = None):
        if getattr(self, "_upload_peer", None) and self._upload_peer[0] == chat_id:
            return self._upload_peer[1]
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError as cache_error:
            if username:
                try:
                    peer = await self.client.get_input_entity(f"@{username.lstrip('@')}")
                except ValueError:
                    peer = None
                if peer is not None:
                    if utils.get_peer_id(peer) != chat_id:
                        raise ValueError(f"Username {username} does not match chat ID {chat_id}")
                    return peer

            async for dialog in self.client.iter_dialogs():
                if dialog.id == chat_id:
                    return dialog.input_entity
            raise ValueError(
                f"Userbot cannot resolve chat ID {chat_id} from its session or dialogs"
            ) from cache_error

    @staticmethod
    def _matches_upload(message, caption, sticker_uid):
        if sticker_uid is not None:
            media_id = getattr(message.sticker, "id", None)
            return media_id is not None and TelegramFileId.document_unique_id(media_id) == sticker_uid
        return message.media is not None and message.raw_text == caption

    @asynccontextmanager
    async def observe_bot_upload(self, bot_id, caption, sticker_uid):
        """Subscribe by sender ID without resolving B's old dialog before sending."""
        receipt = asyncio.get_running_loop().create_future()

        async def receive(event):
            if (event.is_private and event.sender_id == bot_id and event.chat_id == bot_id
                    and self._matches_upload(event.message, caption, sticker_uid)):
                peer = event.input_chat
                if peer is not None:
                    self._upload_peer = (bot_id, peer)
                if not receipt.done():
                    receipt.set_result((event.chat_id, event.id))

        self.client.add_event_handler(receive, events.NewMessage(incoming=True))
        try:
            yield receipt
        finally:
            self.client.remove_event_handler(receive)
            receipt.cancel()

    async def find_bot_upload(self, bot_id, username, caption, sticker_uid, since):
        """Check incoming history back to the first send, including missed updates."""
        peer = await self._resolve_chat(bot_id, username)
        async for message in self.client.iter_messages(peer):
            if message.date.timestamp() < since - 2:
                break
            if (not message.out and message.sender_id == bot_id
                    and self._matches_upload(message, caption, sticker_uid)):
                return message.chat_id, message.id
        return None

    async def forward_stored_message(
        self,
        source_chat_id: int,
        source_message_id: int,
        destination: int | str,
        *,
        source_username: str | None = None,
        receipt_tag: str | None = None,
    ) -> int:
        """Forward one stored Telegram message and return its destination message ID."""
        source_peer = await self._resolve_chat(int(source_chat_id), source_username)
        try:
            forwarded = await self.client.forward_messages(
                entity=destination,
                messages=int(source_message_id),
                from_peer=source_peer,
            )
        except (errors.MessageIdInvalidError, errors.MsgIdInvalidError) as exc:
            raise FileNotFoundError("Stored Telegram message was deleted or is invalid") from exc
        if isinstance(forwarded, (list, tuple)):
            forwarded = forwarded[0] if forwarded else None
        if forwarded is None:
            raise FileNotFoundError("Stored Telegram message is missing")
        forwarded_message_id = getattr(forwarded, "id", None)
        if forwarded_message_id is None:
            raise RuntimeError("Telethon did not return the forwarded message ID")
        if receipt_tag:
            # D sees the reply target in its own message-ID namespace.
            await self.client.send_message(destination, receipt_tag, reply_to=forwarded_message_id)
        return int(forwarded_message_id)

    async def get_group_member_ids(
        self,
        chat_id: int,
        username: str | None = None,
    ) -> list[int]:
        """Return unique active human member IDs visible to the user account."""
        peer = await self._resolve_chat(int(chat_id), username)
        member_ids: set[int] = set()
        async for participant in self.client.iter_participants(peer):
            if getattr(participant, "bot", False) or getattr(participant, "deleted", False):
                continue
            user_id = int(getattr(participant, "id", 0) or 0)
            if user_id > 0 and user_id != int(getattr(self, "user_id", 0) or 0):
                member_ids.add(user_id)
        return sorted(member_ids)

    async def join_chat(self, invite_link: str) -> None:
        """Join a private chat invite generated by the main bot."""
        normalized_link = str(invite_link or "").strip().rstrip("/")
        invite_hash = normalized_link.rsplit("/", 1)[-1].removeprefix("+")
        if not invite_hash or invite_hash == normalized_link:
            raise ValueError("Invalid Telegram invite link")
        try:
            await self.client(functions.messages.ImportChatInviteRequest(invite_hash))
        except errors.UserAlreadyParticipantError:
            return

    async def ensure_bot_admin(self, chat_id: int, bot_id: int, bot_name: str) -> None:
        """Invite the application bot if needed and promote it in this chat."""
        chat = await self._resolve_chat(chat_id)
        if not isinstance(chat, (types.InputPeerChat, types.InputPeerChannel)):
            raise ValueError(f"{chat_id} is not a group or channel")
        bot_peer = await self._resolve_chat(bot_id, bot_name)
        try:
            await self._promote_bot(chat, bot_peer)
        except errors.UserKickedError as exc:
            raise BotKickedError() from exc

    async def _promote_bot(self, chat, bot_peer) -> None:
        try:
            member = await self.client.get_permissions(chat, bot_peer)
        except errors.UserNotParticipantError:
            needs_invite = True
        else:
            if member.is_admin:
                return
            if (isinstance(member.participant, types.ChannelParticipantBanned)
                    and member.participant.banned_rights.view_messages):
                raise BotKickedError()
            needs_invite = member.has_left or getattr(member.participant, "left", False)

        if isinstance(chat, types.InputPeerChat):
            if needs_invite:
                try:
                    await self.client(functions.messages.AddChatUserRequest(chat.chat_id, bot_peer, fwd_limit=0))
                except errors.UserAlreadyParticipantError:
                    pass
            await self.client.edit_admin(chat, bot_peer, is_admin=True)
            return

        channel = await self.client.get_entity(chat)
        granted = channel.admin_rights
        if not channel.creator and not (granted and granted.add_admins):
            raise PermissionError("userbot 没有添加管理员的权限")
        # Broadcast channels accept bots directly as admins. Supergroups also allow a member invite.
        if needs_invite and not channel.broadcast:
            try:
                await self.client(functions.channels.InviteToChannelRequest(chat, [bot_peer]))
            except errors.UserAlreadyParticipantError:
                pass

        rights = types.ChatAdminRights(
            other=True,
            change_info=channel.creator or granted.change_info,
            delete_messages=channel.creator or granted.delete_messages,
            ban_users=channel.creator or granted.ban_users,
            invite_users=channel.creator or granted.invite_users,
            post_messages=channel.broadcast and (channel.creator or granted.post_messages),
        )
        await self.client(functions.channels.EditAdminRequest(chat, bot_peer, rights, rank=""))

    async def run(self, notify_bot: str | None = None, *,
                  on_ready: Callable[[], Awaitable[None]] | None = None) -> None:
        async def phone():
            return self.phone or await asyncio.to_thread(input, "Userbot phone: ")

        async def code():
            return await asyncio.to_thread(input, "Userbot login code: ")

        async def password():
            return await asyncio.to_thread(getpass.getpass, "Userbot 2FA password: ")

        try:
            await self.client.start(phone=phone, code_callback=code, password=password)
            me = await self.client.get_me()
            if me.bot:
                raise ValueError("USERBOT_SESSION must belong to a user account")
            self.user_id = me.id
            self.account_user_id = me.id
            print(f"👦 [USERBOT] Logged in: {me.id}", flush=True)
            if on_ready is not None:
                try:
                    await on_ready()
                except Exception:
                    logger.exception("Userbot startup callback failed")
            if notify_bot:
                try:
                    await self.client.send_message(
                        notify_bot, f"/start"
                    )
                    print(f"👦 [USERBOT] Startup notification sent to {notify_bot}", flush=True)
                except Exception:
                    logger.exception("Could not send userbot startup notification to %s", notify_bot)
            await self.client.run_until_disconnected()
        finally:
            self.user_id = None
            self._upload_peer = None
            await self.client.disconnect()

    async def run_until_shutdown(self, notify_bot: str, *,
                                 on_ready: Callable[[], Awaitable[None]] | None = None) -> None:
        while True:
            try:
                await self.run(notify_bot, on_ready=on_ready)
            except OSError:
                logger.exception("Userbot connection failed")
            await asyncio.sleep(5)
