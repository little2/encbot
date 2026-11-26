from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatMemberUnion


class BotKickedError(PermissionError):
    def __init__(self) -> None:
        super().__init__("機器人已被封鎖，需要更換機器人。")


async def get_chat_member(bot: Bot, chat_id: int, user_id: int) -> ChatMemberUnion:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        if "bot was kicked" in exc.message.lower():
            raise BotKickedError() from exc
        raise
    if member.status == "kicked" and user_id == bot.id:
        raise BotKickedError()
    return member
