from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

from utils.video_store import VideoStore


PROJECT_ROOT = Path(__file__).resolve().parent
VIDEO_ID_PATTERN = r"[0-9a-fA-F]{16}"


def _safe_print(message: str) -> None:
    encoding = str(getattr(sys.stdout, "encoding", "") or "utf-8")
    printable_message = str(message).encode(
        encoding,
        errors="replace",
    ).decode(encoding)
    print(printable_message, flush=True)


def _load_settings() -> tuple[str, Path]:
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)
    token = str(os.getenv("VIDEO_BOT_TOKEN", "") or "").strip()
    if not token:
        raise RuntimeError("缺少 VIDEO_BOT_TOKEN")

    volume_mount_path = str(
        os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "") or ""
    ).strip()
    default_db_path = (
        Path(volume_mount_path) / "video_bot.sqlite3"
        if volume_mount_path
        else PROJECT_ROOT / "data" / "video_bot.sqlite3"
    )
    configured_db_path = str(
        os.getenv("VIDEO_BOT_DB_PATH", str(default_db_path))
        or default_db_path
    ).strip()
    db_path = Path(configured_db_path)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    return token, db_path


def _display_bot_name(username: str) -> str:
    normalized_username = str(username or "").strip().removeprefix("@")
    if normalized_username.lower().endswith("bot"):
        normalized_username = normalized_username[:-3]
    return normalized_username


def _extract_record_ids(text: str, bot_name: str) -> list[str]:
    normalized_bot_name = str(bot_name or "").strip().removeprefix("@")
    prefixed_id = (
        rf"{re.escape(normalized_bot_name)}_({VIDEO_ID_PATTERN})"
        if normalized_bot_name
        else rf"({VIDEO_ID_PATTERN})"
    )
    pattern = re.compile(
        rf"(?<![0-9a-zA-Z_])(?:{prefixed_id}|({VIDEO_ID_PATTERN}))"
        rf"(?![0-9a-zA-Z_])",
        re.IGNORECASE,
    )
    return [
        next(group for group in match.groups() if group is not None).lower()
        for match in pattern.finditer(str(text or ""))
    ]


def _build_dispatcher(
    store: VideoStore,
    bot_name: str,
    video_bot: Bot,
    airport_lobby_group_id: int,
    paid_invite_lifetime_hours: int,
) -> Dispatcher:
    dispatcher = Dispatcher()

    @dispatcher.message(F.chat.type == "private", Command("check"))
    async def check(message: Message) -> None:
        if message.from_user is None:
            return
        records = store.get_top_videos(message.from_user.id, limit=10)
        total_view_count = store.get_total_view_count(message.from_user.id)
        recent_view_count = store.get_recent_view_count(
            message.from_user.id,
            days=7,
        )
        if not records:
            await message.reply("你还没有上传任何视频。")
            return

        lines = ["你的视频观看排行", ""]
        lines.insert(1, f"总观看数：{total_view_count}")
        lines.insert(2, f"近 7 天观看数：{recent_view_count}")
        lines.extend(
            f"{index}. <code>{record['id']}</code> — "
            f"{record['view_count']} 人观看"
            for index, record in enumerate(records, start=1)
        )
        reply_markup = None
        if recent_view_count > 99:
            try:
                invite = await video_bot.create_chat_invite_link(
                    chat_id=airport_lobby_group_id,
                    name=f"video-check-{message.from_user.id}",
                    expire_date=datetime.now(timezone.utc) + timedelta(
                        hours=paid_invite_lifetime_hours
                    ),
                    creates_join_request=True,
                )
                reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="申请加入机场群",
                        url=invite.invite_link,
                    )
                ]])
            except Exception as exc:
                _safe_print(
                    "[VIDEO_BOT] 为用户 "
                    f"{message.from_user.id} 创建审核邀请链接失败：{exc}"
                )
        await message.reply(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    @dispatcher.message(F.chat.type == "private", F.video)
    async def receive_video(message: Message) -> None:
        if message.from_user is None or message.video is None:
            return
        try:
            record_id = store.add_video(
                file_id=message.video.file_id,
                file_type="video",
                uploader_id=message.from_user.id,
            )
        except Exception as exc:
            _safe_print(f"[VIDEO_BOT] 保存视频失败：{exc}")
            await message.reply("视频保存失败，请稍后再试。")
            return

        await message.reply(
            f"视频 ID：\n<code>{bot_name}_{record_id}</code>",
            parse_mode="HTML",
        )

    @dispatcher.message(F.chat.type == "private", F.text)
    async def receive_video_id(message: Message) -> None:
        if message.from_user is None:
            return
        record_ids = _extract_record_ids(message.text or "", bot_name)
        if not record_ids:
            await message.reply("请输入有效的密文。")
            return

        records = [
            (record_id, store.get_video(record_id))
            for record_id in record_ids
        ]
        if not any(record is not None for _, record in records):
            await message.reply("找不到这个视频。")
            return

        for record_id, record in records:
            if record is None:
                continue
            try:
                await message.reply_video(video=str(record["file_id"]))
            except Exception as exc:
                _safe_print(
                    f"[VIDEO_BOT] 发送视频 {record_id} 失败：{exc}"
                )
                await message.reply("视频发送失败，请稍后再试。")
                continue

            try:
                store.record_unique_view(record_id, message.from_user.id)
            except Exception as exc:
                _safe_print(
                    f"[VIDEO_BOT] 记录视频 {record_id} 的观看数据失败：{exc}"
                )

    @dispatcher.message(F.chat.type == "private")
    async def unsupported_message(message: Message) -> None:
        await message.reply("不支持此消息类型，目前只支持视频及视频 ID。")

    return dispatcher


async def _check_airport_lobby_admin(
    video_bot: Bot,
    airport_lobby_group_id: int,
) -> None:
    if airport_lobby_group_id == 0:
        raise RuntimeError("缺少 AIRPORT_LOBBY_GROUP_ID")

    try:
        bot_status = await video_bot.get_chat_member(
            chat_id=airport_lobby_group_id,
            user_id=video_bot.id,
        )
    except Exception as exc:
        raise RuntimeError(
            "无法检查 VIDEO_BOT 是否为 "
            f"AIRPORT_LOBBY_GROUP_ID={airport_lobby_group_id} 的管理员：{exc}"
        ) from exc

    if bot_status.status not in ("administrator", "creator"):
        raise RuntimeError(
            "VIDEO_BOT 不是 "
            f"AIRPORT_LOBBY_GROUP_ID={airport_lobby_group_id} 的管理员，"
            f"当前状态：{bot_status.status}"
        )

    _safe_print(
        "[VIDEO_BOT] 已确认是 "
        f"AIRPORT_LOBBY_GROUP_ID={airport_lobby_group_id} 的管理员"
    )


async def start_video_bot(
    airport_lobby_group_id: int | None = None,
    paid_invite_lifetime_hours: int | None = None,
) -> None:
    token, db_path = _load_settings()
    if airport_lobby_group_id is None:
        airport_lobby_group_id = int(
            os.getenv("AIRPORT_LOBBY_GROUP_ID", "0") or 0
        )
    if paid_invite_lifetime_hours is None:
        paid_invite_lifetime_hours = int(
            os.getenv("PAID_INVITE_LIFETIME_HOURS", "24") or 24
        )
    paid_invite_lifetime_hours = max(1, int(paid_invite_lifetime_hours))
    store = VideoStore(db_path)
    video_bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        me = await video_bot.get_me()
        await _check_airport_lobby_admin(
            video_bot,
            int(airport_lobby_group_id),
        )
        username = str(getattr(me, "username", "") or "")
        bot_name = username
        dispatcher = _build_dispatcher(
            store,
            bot_name,
            video_bot,
            int(airport_lobby_group_id),
            paid_invite_lifetime_hours,
        )
        await video_bot.set_my_commands([
            BotCommand(command="check", description="查看資源分享情況"),
        ])
        _safe_print(f"[VIDEO_BOT] 已启动：@{username}")
        await dispatcher.start_polling(video_bot)
    finally:
        store.close()
        await video_bot.session.close()


if __name__ == "__main__":
    asyncio.run(start_video_bot())
