"""简单的文字、媒体回复机器人。

设置 GUIDER_BOT_TOKEN 和 DATABASE_URL 后运行：python guider.py
黑名单存储通过 aiogram 的依赖注入提供给文字处理函数。
文字消息会查询数据库中已有的 user_blacklist 表，不创建表或保存消息。
"""

import asyncio
import logging
import os

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

from shared_config import SharedConfig
from utils.async_stores import BlacklistStore, blacklist_store_from_pool

SharedConfig.load(True)

dp = Dispatcher()


@dp.message(F.photo | F.video | F.document | F.audio | F.voice | F.animation | F.sticker | F.video_note)
async def on_media(message: Message) -> None:
    await message.reply("收到媒体信息。")


@dp.message(F.text)
async def on_text(message: Message, blacklist_store: BlacklistStore) -> None:
    if not message.from_user:
        logging.warning("文字消息没有发送者，无法检查黑名单")
        return
    if await blacklist_store.is_blocked(message.from_user.id):
        print(f"用户 {message.from_user.id} 在黑名单中，消息被阻止")
        return
    await message.reply("收到文字信息。")


async def main() -> None:
    token = os.getenv("GUIDER_BOT_TOKEN", SharedConfig.get("my_bot_token")).strip()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not token:
        raise RuntimeError("请设置 GUIDER_BOT_TOKEN")
    if not database_url:
        raise RuntimeError("请设置 DATABASE_URL")

    bot = Bot(token=token)
    try:
        db = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=5)
        try:
            await db.fetchval("SELECT 1")
            logging.info("PostgreSQL 已连接，机器人开始运行")
            blacklist_store = blacklist_store_from_pool(db)
            await dp.start_polling(
                bot, blacklist_store=blacklist_store, close_bot_session=False
            )
        finally:
            await db.close()
    finally:
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())