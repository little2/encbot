"""Optional user-account listener sharing the application's asyncio loop."""
from __future__ import annotations

import asyncio
import getpass
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient, events, types

logger = logging.getLogger(__name__)


class TelethonUserbot:
    def __init__(self, api_id: int, api_hash: str, session: str,
                 on_message: Callable[..., Awaitable[None]], phone: str = ""):
        Path(session).parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(session, api_id, api_hash)
        self.phone = phone
        self.user_id: int | None = None
        self.on_message = on_message
        self.client.add_event_handler(self._handle_message, events.NewMessage(incoming=True))

    @classmethod
    def from_env(cls, on_message: Callable[..., Awaitable[None]]) -> TelethonUserbot:
        api_id = int(os.environ["USERBOT_API_ID"])
        api_hash = os.environ["USERBOT_API_HASH"].strip()
        if api_id <= 0 or not api_hash:
            raise ValueError("USERBOT_API_ID and USERBOT_API_HASH must be configured")
        session = os.getenv("USERBOT_SESSION", str(Path(__file__).parent / "data" / "userbot"))
        return cls(api_id, api_hash, session, on_message, os.getenv("USERBOT_PHONE", ""))

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

    async def run(self, notify_bot: str | None = None) -> None:
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
            print(f"[USERBOT] Logged in: {me.id}", flush=True)
            if notify_bot:
                try:
                    await self.client.send_message(
                        notify_bot, f"TelethonUserbot 已啟動並登入成功（user_id={me.id}）。"
                    )
                    print(f"[USERBOT] Startup notification sent to {notify_bot}", flush=True)
                    self.user_id = me.id
                except Exception:
                    logger.exception("Could not send userbot startup notification to %s", notify_bot)
            await self.client.run_until_disconnected()
        finally:
            self.user_id = None
            await self.client.disconnect()
