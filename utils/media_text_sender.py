"""Parse media codes and deliver small, paced Telegram albums."""

import re
from functools import partial

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo

from utils.telegram_gate import TelegramGate


MEDIA_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])([vdp])_([A-Za-z0-9_-]+)(?![A-Za-z0-9_-])"
)
ALBUM_BATCH_SIZE = 5
ALBUM_INTERVAL_SECONDS = 5.0


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

    async def send(self, bot: Bot, chat_id: int, text: str) -> None:
        media_classes = {
            "v": InputMediaVideo,
            "d": InputMediaDocument,
            "p": InputMediaPhoto,
        }
        single_methods = {
            "v": (bot.send_video, "video"),
            "d": (bot.send_document, "document"),
            "p": (bot.send_photo, "photo"),
        }
        failed_count = 0
        for batch in media_batches(text):
            if len(batch) == 1:
                media_type, file_id = batch[0]
                method, parameter = single_methods[media_type]
                operation = partial(method, chat_id=chat_id, **{parameter: file_id})
            else:
                media = [
                    media_classes[media_type](media=file_id)
                    for media_type, file_id in batch
                ]
                operation = partial(bot.send_media_group, chat_id=chat_id, media=media)
            try:
                await self.gate.call("send parsed media", operation, chat_id=chat_id)
            except TelegramBadRequest as exc:
                # An invalid ID must not discard later, independent batches.
                failed_count += len(batch)
                print(f"[MEDIA_TEXT_SEND_FAILED] chat_id={chat_id}: {exc}", flush=True)

        if failed_count:
            await self.gate.call(
                "parsed media failure notice",
                lambda: bot.send_message(
                    chat_id=chat_id,
                    text=f"有 {failed_count} 个媒体未能发送，请检查对应的类型与 file_id。",
                ),
                chat_id=chat_id,
            )
