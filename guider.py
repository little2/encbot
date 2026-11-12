"""简单的文字、媒体回复机器人。

设置 GUIDER_BOT_TOKEN 和 DATABASE_URL 后运行：python guider.py。
默认使用轮询；设置 BOT_MODE=webhook、WEBHOOK_HOST 和 WEBHOOK_PATH
以在 Render 使用 webhook。Webhook 请求不进行密钥验证。
WEBHOOK_HOST 为公网 HTTPS 来源地址，WEBHOOK_PATH 只能包含字母、数字、
下划线、连字符和斜线。PORT 默认 8080，监听地址 GUIDER_WEBHOOK_HOST
默认 0.0.0.0；GET / 可用于 Render 健康检查。
黑名单及上传批次存储通过 aiogram 的依赖注入提供给消息处理函数。
消息会查询已有的 user_blacklist 表，媒体消息还会查询 batch 表；不创建表或保存消息。
"""

import asyncio
import logging
import os
from pathlib import Path
import re
import signal

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv
from yarl import URL

if __name__ == "__main__":
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)

from shared_config import SharedConfig
from utils.airport_async_stores import BatchStore, batch_store_from_pool
from utils.async_stores import BlacklistStore, blacklist_store_from_pool

SharedConfig.load(True)

dp = Dispatcher()


def webhook_settings() -> tuple[str, str, str, int] | None:
    mode = os.getenv("BOT_MODE", "").strip().lower()
    if mode and mode not in ("polling", "webhook"):
        raise ValueError("BOT_MODE 必须是 polling 或 webhook")
    if mode == "polling":
        return None

    public_host = os.getenv("WEBHOOK_HOST", "").strip()
    webhook_path = os.getenv("WEBHOOK_PATH", "").strip()
    if not (public_host or webhook_path or mode == "webhook"):
        return None
    if not public_host or not webhook_path:
        raise ValueError("webhook 模式必须设置 WEBHOOK_HOST 和 WEBHOOK_PATH")
    try:
        host_url = URL(public_host)
        if (
            host_url.scheme != "https" or not host_url.host
            or host_url.user is not None or host_url.password is not None
            or host_url.path not in ("", "/") or host_url.query_string or host_url.fragment
        ):
            raise ValueError
    except ValueError as exc:
        raise ValueError("WEBHOOK_HOST 必须是无路径、凭据、查询参数或片段的 HTTPS 地址") from exc
    if not re.fullmatch(r"/[A-Za-z0-9_/-]*", webhook_path) or "//" in webhook_path:
        raise ValueError("WEBHOOK_PATH 必须以 / 开头，且只能包含字母、数字、下划线、连字符和斜线")
    host = os.getenv("GUIDER_WEBHOOK_HOST", "0.0.0.0").strip()
    if not host:
        raise ValueError("GUIDER_WEBHOOK_HOST 不能为空")
    try:
        port = int(os.getenv("PORT", "8080"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise ValueError("PORT 必须是 1–65535 的整数") from exc
    return public_host.rstrip("/") + webhook_path, webhook_path, host, port


def webhook_app(
    bot: Bot, path: str,
    blacklist_store: BlacklistStore, batch_store: BatchStore,
) -> web.Application:
    app = web.Application()

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app.router.add_get("/", health)
    handler = SimpleRequestHandler(
        dispatcher=dp, bot=bot, handle_in_background=False,
        blacklist_store=blacklist_store, batch_store=batch_store,
    )
    handler.register(app, path=path)
    setup_application(app, dp, bot=bot, blacklist_store=blacklist_store, batch_store=batch_store)
    return app


async def run_webhook(
    bot: Bot, settings: tuple[str, str, str, int],
    blacklist_store: BlacklistStore, batch_store: BatchStore,
) -> None:
    url, path, host, port = settings
    runner = web.AppRunner(webhook_app(bot, path, blacklist_store, batch_store))
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    try:
        await runner.setup()
        await web.TCPSite(runner, host=host, port=port).start()
        registered = await bot.set_webhook(
            url=url, allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        if not registered:
            raise RuntimeError("Telegram 未确认 webhook 注册")
        logging.info("Webhook 服务已启动")
        await stopped.wait()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        await runner.cleanup()


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
    tower = "zttower12bot" if upload_count >= 30 else "zttower18bot"

    text = f'''
📢 各位旅客请注意：

⚠️ 鉴于近期战事频繁，为确保飞行安全，请各位旅客改由指定塔台进行起飞作业。✈️

🔒 为避免塔台位置及相关设施遭受攻击，请勿对外泄露、传播或讨论任何塔台相关信息。

🛡️ 感谢各位旅客的理解与配合，祝您飞行平安！

🗼 @{tower}
'''
    await message.reply(text, protect_content=True)


@dp.message(F.text)
async def on_text(message: Message, blacklist_store: BlacklistStore) -> None:
    if not message.from_user:
        logging.warning("文字消息没有发送者，无法检查黑名单")
        return
    if await blacklist_store.is_blocked(message.from_user.id):
        return
    text = '''
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
    settings = webhook_settings()

    bot = Bot(token=token)
    try:

        await bot.set_my_commands(
			[
				BotCommand(command="start", description="开始"),
			],
			scope=BotCommandScopeAllPrivateChats(),
		)


        db = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=5)
        try:
            await db.fetchval("SELECT 1")
            logging.info("PostgreSQL 已连接，机器人开始运行")
            blacklist_store = blacklist_store_from_pool(db)
            batch_store = batch_store_from_pool(db)

            if settings:
                await run_webhook(bot, settings, blacklist_store, batch_store)
            else:
                await bot.delete_webhook(drop_pending_updates=False)
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
