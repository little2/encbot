"""Parse media codes and deliver small, paced Telegram albums."""

import asyncio
import re
from functools import partial
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo, Message

from utils.telegram_gate import TelegramGate


MEDIA_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])([vdp])_([A-Za-z0-9_-]+)(?![A-Za-z0-9_-])"
)
ALBUM_BATCH_SIZE = 5
ALBUM_INTERVAL_SECONDS = 5.0
RECOVERY_CAPTION_PREFIX = "media-recovery:"
RECOVERY_TIMEOUT_SECONDS = 30.0


def media_batches(text: str) -> list[list[tuple[str, str]]]:
    batches: list[list[tuple[str, str]]] = []
    for media_type, file_id in MEDIA_CODE_PATTERN.findall(text):
        # Photos and videos may share an album; documents must be separate.
        is_document = media_type == "d"
        if (
            not batches
            or len(batches[-1]) >= ALBUM_BATCH_SIZE
            or (batches[-1][0][0] == "d") != is_document
        ):
            batches.append([])
        batches[-1].append((media_type, file_id))
    return batches


class MediaTextSender:
    def __init__(self) -> None:
        # Shared across requests so repeated DMs cannot bypass the chat pacing.
        self.gate = TelegramGate(
            max_concurrent=3,
            send_min_interval=ALBUM_INTERVAL_SECONDS,
        )
        self.backup_gate = TelegramGate(max_concurrent=3, send_min_interval=1.0)
        self._pending: dict[str, tuple[int, str, asyncio.Future[str]]] = {}

    @staticmethod
    def _operation(bot: Bot, chat_id: int, batch: list[tuple[str, str]]):
        media_classes = {
            "v": InputMediaVideo,
            "d": InputMediaDocument,
            "p": InputMediaPhoto,
        }
        if len(batch) == 1:
            media_type, file_id = batch[0]
            method, parameter = {
                "v": (bot.send_video, "video"),
                "d": (bot.send_document, "document"),
                "p": (bot.send_photo, "photo"),
            }[media_type]
            return partial(method, chat_id=chat_id, **{parameter: file_id})
        media = [media_classes[kind](media=file_id) for kind, file_id in batch]
        return partial(bot.send_media_group, chat_id=chat_id, media=media)

    def accept_recovery(self, message: Message) -> bool:
        caption = message.caption or ""
        if not caption.startswith(RECOVERY_CAPTION_PREFIX):
            return False
        token = caption[len(RECOVERY_CAPTION_PREFIX):]
        pending = self._pending.get(token)
        if pending is None or message.from_user is None:
            return False
        backup_id, media_type, future = pending
        if message.from_user.id != backup_id or future.done():
            return False
        media = {
            "v": message.video,
            "d": message.document,
            "p": message.photo[-1] if message.photo else None,
        }[media_type]
        if media is None:
            return False
        future.set_result(media.file_id)
        return True

    async def _recover_file(
        self, bot: Bot, bkbot: Bot, media_type: str, file_id: str,
    ) -> str:
        token = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[token] = (bkbot.id, media_type, future)
        try:
            operation = self._operation(bkbot, bot.id, [(media_type, file_id)])
            # Register before sending: the incoming update may beat the API response.
            operation.keywords["caption"] = RECOVERY_CAPTION_PREFIX + token
            await self.backup_gate.call(
                "backup media to main bot", operation, chat_id=bot.id,
            )
            return await asyncio.wait_for(future, timeout=RECOVERY_TIMEOUT_SECONDS)
        finally:
            self._pending.pop(token, None)
            if not future.done():
                future.cancel()

    async def _recover_batch(
        self, bot: Bot, bkbot: Bot, batch: list[tuple[str, str]],
    ) -> list[tuple[str, str]] | None:
        recovered = []
        changed = False
        for media_type, file_id in batch:
            try:
                new_id = await self._recover_file(bot, bkbot, media_type, file_id)
            except (TelegramAPIError, asyncio.TimeoutError) as exc:
                print(
                    f"[MEDIA_TEXT_RECOVERY_FAILED] type={media_type}: "
                    f"{type(exc).__name__}: {exc}", flush=True,
                )
                # The original ID may already belong to the main bot.
                recovered.append((media_type, file_id))
            else:
                recovered.append((media_type, new_id))
                changed = True
        return recovered if changed else None

    async def send(self, bot: Bot, chat_id: int, text: str, bkbot: Bot | None = None) -> None:
        failed_count = 0
        for batch in media_batches(text):
            operation = self._operation(bot, chat_id, batch)
            try:
                await self.gate.call("send parsed media", operation, chat_id=chat_id)
            except TelegramAPIError as exc:
                print(f"[MEDIA_TEXT_SEND_FAILED] chat_id={chat_id}: {exc}", flush=True)
                if bkbot is not None:
                    recovered = await self._recover_batch(bot, bkbot, batch)
                    if recovered is not None:
                        try:
                            await self.gate.call(
                                "resend recovered media",
                                self._operation(bot, chat_id, recovered),
                                chat_id=chat_id,
                                max_attempts=1,
                            )
                        except TelegramAPIError as retry_exc:
                            print(
                                f"[MEDIA_TEXT_RESEND_FAILED] chat_id={chat_id}: {retry_exc}",
                                flush=True,
                            )
                        else:
                            continue
                failed_count += len(batch)

        if failed_count:
            await self.gate.call(
                "parsed media failure notice",
                lambda: bot.send_message(
                    chat_id=chat_id,
                    text=f"有 {failed_count} 个媒体未能发送，请检查对应的类型与 file_id。",
                ),
                chat_id=chat_id,
            )
