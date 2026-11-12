"""简单的文字、媒体回复机器人。

设置 GUIDER_BOT_TOKEN 和 DATABASE_URL 后运行：python guider.py
黑名单及上传批次存储通过 aiogram 的依赖注入提供给消息处理函数。
消息会查询已有的 user_blacklist、batch 表，不创建表或保存消息。
"""

import asyncio
import logging
import os

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

from shared_config import SharedConfig
from utils.airport_async_stores import BatchStore, batch_store_from_pool
from utils.async_stores import BlacklistStore, blacklist_store_from_pool

SharedConfig.load(True)

dp = Dispatcher()


@dp.message(F.photo | F.video | F.document | F.audio | F.voice | F.animation | F.sticker | F.video_note)
async def on_media(
    message: Message, blacklist_store: BlacklistStore, batch_store: BatchStore,
) -> None:
    if not message.from_user:
        logging.warning("文字消息没有发送者，无法检查黑名单")
        return
    if await blacklist_store.is_blocked(message.from_user.id):
        return
    upload_count = await batch_store.count_by_uploader(message.from_user.id)
    if upload_count >=30 :
        tower = "zttower12bot"
    else:
        tower = "zttower18bot"


    text = f'''
📢 各位旅客请注意：

⚠️ 鉴于近期战事频繁，为确保飞行安全，请各位旅客改由指定塔台进行起飞作业。✈️

🔒 为避免塔台位置及相关设施遭受攻击，请勿对外泄露、传播或讨论任何塔台相关信息。

🛡️ 感谢各位旅客的理解与配合，祝您飞行平安！

🗼 @{tower}
'''
    

    await message.reply(f"{text}",protect_content=True)


@dp.message(F.text)
async def on_text(
    message: Message, blacklist_store: BlacklistStore, batch_store: BatchStore,
) -> None:
    if not message.from_user:
        logging.warning("文字消息没有发送者，无法检查黑名单")
        return
    if await blacklist_store.is_blocked(message.from_user.id):
        return
    upload_count = await batch_store.count_by_uploader(message.from_user.id)
    text = f'''
📢 各位旅客请注意：

✈️ 您的起飞机场信息尚未解锁。请先提供一份资源，即可获取起飞机场的相关情报。🔓

💺 感谢您的理解与配合，祝您旅途愉快！🌤️
'''
    await message.reply(text)


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
            batch_store = batch_store_from_pool(db)
            await dp.start_polling(
                bot, blacklist_store=blacklist_store, batch_store=batch_store,
                close_bot_session=False,
            )
        finally:
            await db.close()
    finally:
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
