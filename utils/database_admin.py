from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message


class DatabaseAdminCommands:
    def __init__(
        self,
        *,
        bot: Bot,
        backup_lock: asyncio.Lock,
        database_path: Path,
        key_man_id: int,
        admin_user_ids: set[int] | list[int],
        telegram_call: Callable[..., Any],
        app_now: Callable[..., Any],
        user_expire_cache: Any | None = None,
        blacklist_store: Any | None = None,
    ) -> None:
        self.bot = bot
        self.backup_lock = backup_lock
        self.database_path = database_path
        self.key_man_id = key_man_id
        self.admin_user_ids = set(admin_user_ids)
        self.telegram_call = telegram_call
        self.app_now = app_now
        self.user_expire_cache = user_expire_cache
        self.blacklist_store = blacklist_store

    @staticmethod
    def _format_file_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size)
        unit_index = 0
        while value >= 1024 and unit_index < len(units) - 1:
            value /= 1024.0
            unit_index += 1
        return f"{value:.2f} {units[unit_index]}" if unit_index else f"{value:.0f} {units[unit_index]}"

    @staticmethod
    def _create_sqlite_backup(source_path: Path, destination_path: Path) -> None:
        source = sqlite3.connect(source_path)
        destination = sqlite3.connect(destination_path)
        try:
            with destination:
                source.backup(destination)
                check_result = destination.execute("PRAGMA quick_check").fetchone()
                if not check_result or str(check_result[0]).lower() != "ok":
                    raise RuntimeError("SQLite backup integrity check failed")
        finally:
            destination.close()
            source.close()

    @staticmethod
    def _clear_media_tables(database_path: Path) -> tuple[int, int]:
        connection = sqlite3.connect(database_path)
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            received_media_count = int(
                connection.execute("SELECT COUNT(*) FROM received_media").fetchone()[0]
            )
            batch_count = int(connection.execute("SELECT COUNT(*) FROM batch").fetchone()[0])
            connection.execute("DELETE FROM received_media")
            connection.execute("DELETE FROM batch")
            connection.commit()
            return received_media_count, batch_count
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    RESTORE_REQUIRED_SCHEMA = {
        "user_expire": {
            "user_id", "expire_timestamp", "update_timestamp", "group_message_timestamp",
        },
        # "user_blacklist": {"user_id", "reason", "created_by", "created_at"},
        # # "received_media": {
        # #     "file_unique_id", "file_id", "file_type", "first_user_id",
        # #     "source_chat_id", "source_message_id", "status", "created_at",
        # #     "accepted_at", "batch_id",
        # # },
        # "batch": {
        #     "batch_id", "channel_chat_id", "channel_message_id",
        #     "discussion_chat_id", "discussion_message_id", "batch_content",
        #     "created_at", "updated_at",
        # },
        # "shared_invite_link": {
        #     "link_key", "chat_id", "invite_link", "name", "created_at", "validated_at",
        # },
    }

    @classmethod
    def _validate_restore_database(cls, database_path: Path) -> dict[str, int]:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        try:
            check_result = connection.execute("PRAGMA quick_check").fetchone()
            if not check_result or str(check_result[0]).lower() != "ok":
                raise ValueError("SQLite 完整性检查未通过")

            table_counts: dict[str, int] = {}
            for table_name, required_columns in cls.RESTORE_REQUIRED_SCHEMA.items():
                table_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table_name,),
                ).fetchone()
                if not table_exists:
                    raise ValueError(f"缺少必要数据表：{table_name}")

                columns = {
                    str(row[1])
                    for row in connection.execute(f'PRAGMA table_info("{table_name}")')
                }
                missing_columns = sorted(required_columns - columns)
                if missing_columns:
                    raise ValueError(
                        f"数据表 {table_name} 缺少字段：{', '.join(missing_columns)}"
                    )
                table_counts[table_name] = int(
                    connection.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
                )
            return table_counts
        finally:
            connection.close()

    @staticmethod
    def _restore_sqlite_database(source_path: Path, destination_path: Path) -> None:
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination)
            check_result = destination.execute("PRAGMA quick_check").fetchone()
            if not check_result or str(check_result[0]).lower() != "ok":
                raise RuntimeError("恢复后的 SQLite 完整性检查未通过")
        finally:
            destination.close()
            source.close()

    def _is_allowed(self, message: Message) -> bool:
        if not message.from_user:
            return False
        requester_user_id = int(message.from_user.id)
        return requester_user_id == self.key_man_id or requester_user_id in self.admin_user_ids

    async def backup(self, message: Message) -> None:
        if not self._is_allowed(message):
            await message.reply("❌ 无效指令")
            return

        status_message = await message.reply(
            "🗄️ 已收到数据库备份请求。\n"
            "⏳ 正在等待备份任务开始……"
        )

        async def update_status(text: str) -> None:
            try:
                await status_message.edit_text(text)
            except Exception as exc:  # pragma: no cover - status update should not crash the command
                print(f"[BACKUP] status update failed: {exc}", flush=True)

        async with self.backup_lock:
            started_at = asyncio.get_running_loop().time()
            timestamp = self.app_now().strftime("%Y%m%d-%H%M%S")
            backup_filename = f"encbot-backup-{timestamp}.sqlite3"
            backup_stage = "建立 SQLite 快照"
            try:
                await update_status(
                    "🗄️ 数据库备份进行中\n\n"
                    "⏳ 阶段 1/2：正在建立 SQLite 一致性快照……"
                )
                with tempfile.TemporaryDirectory(prefix="encbot-backup-") as temp_dir:
                    backup_path = Path(temp_dir) / backup_filename
                    await asyncio.to_thread(
                        self._create_sqlite_backup,
                        self.database_path,
                        backup_path,
                    )
                    backup_size_text = self._format_file_size(backup_path.stat().st_size)
                    await update_status(
                        "🗄️ 数据库备份进行中\n\n"
                        "✅ 阶段 1/2：SQLite 快照建立完成\n"
                        f"📦 文件大小：{backup_size_text}\n"
                        "⏳ 阶段 2/2：正在上传到 Telegram……"
                    )
                    backup_stage = "上传到 Telegram"

                    async def on_backup_retry(
                        attempt: int,
                        max_attempts: int,
                        delay: int,
                        exc: Exception,
                    ) -> None:
                        reason = (
                            "Telegram 要求暂时限流"
                            if isinstance(exc, TelegramRetryAfter)
                            else "Telegram 网络连接暂时失败"
                        )
                        await update_status(
                            "🗄️ 数据库备份进行中\n\n"
                            "✅ 阶段 1/2：SQLite 快照建立完成\n"
                            f"📦 文件大小：{backup_size_text}\n"
                            f"⚠️ 阶段 2/2：{reason}\n"
                            f"🔄 已尝试 {attempt}/{max_attempts} 次，"
                            f"将在 {delay} 秒后重试……"
                        )

                    await self.telegram_call(
                        "send sqlite backup",
                        lambda: self.bot.send_document(
                            chat_id=self.key_man_id,
                            document=FSInputFile(backup_path, filename=backup_filename),
                            caption=f"SQLite 数据库备份\n{timestamp} (UTC+8)",
                        ),
                        on_retry=on_backup_retry,
                    )
            except Exception as exc:
                print(f"[BACKUP] failed: {exc}", flush=True)
                elapsed_seconds = int(asyncio.get_running_loop().time() - started_at)
                await update_status(
                    "❌ 数据库备份失败\n\n"
                    f"阶段：{backup_stage}\n"
                    f"耗时：{elapsed_seconds} 秒\n"
                    f"原因：{exc}\n\n"
                    "临时备份文件已自动清理，可以稍后再次执行 /backup。"
                )
                return

            elapsed_seconds = int(asyncio.get_running_loop().time() - started_at)
            await update_status(
                "✅ 数据库备份完成\n\n"
                f"📄 文件：{backup_filename}\n"
                f"📦 大小：{backup_size_text}\n"
                f"📨 已发送给：{self.key_man_id}\n"
                f"⏱️ 耗时：{elapsed_seconds} 秒\n\n"
                "临时备份文件已自动清理。"
            )

    async def clear_media(self, message: Message) -> None:
        if not self._is_allowed(message):
            await message.reply("❌ 無權限")
            return

        status_message = await message.reply(
            "🗄️ 正在備份資料庫；備份成功送出後才會清空媒體資料……"
        )

        async def update_status(text: str) -> None:
            try:
                await status_message.edit_text(text)
            except Exception as exc:  # pragma: no cover - status update should not crash the command
                print(f"[CLEAR_MEDIA] status update failed: {exc}", flush=True)

        async with self.backup_lock:
            timestamp = self.app_now().strftime("%Y%m%d-%H%M%S")
            backup_filename = f"encbot-before-clear-media-{timestamp}.sqlite3"
            try:
                with tempfile.TemporaryDirectory(prefix="encbot-clear-media-") as temp_dir:
                    backup_path = Path(temp_dir) / backup_filename
                    await asyncio.to_thread(
                        self._create_sqlite_backup,
                        self.database_path,
                        backup_path,
                    )
                    backup_size_text = self._format_file_size(backup_path.stat().st_size)
                    await update_status("🗄️ 備份已建立，正在傳送給 KEY_MAN……")
                    await self.telegram_call(
                        "send pre-clear-media sqlite backup",
                        lambda: self.bot.send_document(
                            chat_id=self.key_man_id,
                            document=FSInputFile(backup_path, filename=backup_filename),
                            caption=(
                                "清空 received_media、batch 前的完整資料庫備份\n"
                                f"{timestamp} (UTC+8)"
                            ),
                        ),
                    )

                    await update_status("✅ 備份已送出，正在清空 received_media 和 batch……")
                    received_media_count, batch_count = await asyncio.to_thread(
                        self._clear_media_tables,
                        self.database_path,
                    )
            except Exception as exc:
                print(f"[CLEAR_MEDIA] failed: {exc}", flush=True)
                await update_status(
                    "❌ 清空失敗\n\n"
                    f"原因：{exc}\n"
                    "若備份未成功送出，資料表不會被清空。"
                )
                return

            await update_status(
                "✅ 媒體資料已清空\n\n"
                f"備份：{backup_filename}\n"
                f"大小：{backup_size_text}\n"
                f"received_media：刪除 {received_media_count} 筆\n"
                f"batch：刪除 {batch_count} 筆"
            )

    async def restore(self, message: Message) -> None:
        if not self._is_allowed(message):
            await message.reply("❌ 无效指令")
            return

        replied_message = message.reply_to_message
        document = replied_message.document if replied_message else None
        if not document:
            await message.reply(
                "❌ 请使用 /restore 回复一个由 /backup 产生的 .sqlite3 文件。"
            )
            return
        file_name = str(document.file_name or "").strip()
        if not file_name.lower().endswith(".sqlite3"):
            await message.reply("❌ 恢复文件必须使用 .sqlite3 扩展名。")
            return

        status_message = await message.reply(
            "♻️ 已收到数据库恢复请求。\n"
            "⏳ 正在等待数据库维护锁……"
        )

        async def update_status(text: str) -> None:
            try:
                await status_message.edit_text(text)
            except Exception as exc:  # pragma: no cover - status update should not crash the command
                print(f"[RESTORE] status update failed: {exc}", flush=True)

        async with self.backup_lock:
            started_at = asyncio.get_running_loop().time()
            timestamp = self.app_now().strftime("%Y%m%d-%H%M%S")
            restore_stage = "下载恢复文件"
            try:
                with tempfile.TemporaryDirectory(prefix="encbot-restore-") as temp_dir:
                    temp_path = Path(temp_dir)
                    uploaded_path = temp_path / "uploaded-restore.sqlite3"
                    current_backup_path = temp_path / f"pre-restore-{timestamp}.sqlite3"

                    await update_status(
                        "♻️ 数据库恢复进行中\n\n"
                        "⏳ 阶段 1/4：正在从 Telegram 下载恢复文件……"
                    )

                    async def on_download_retry(
                        attempt: int,
                        max_attempts: int,
                        delay: int,
                        exc: Exception,
                    ) -> None:
                        await update_status(
                            "♻️ 数据库恢复进行中\n\n"
                            f"⚠️ 阶段 1/4：下载连接暂时失败 ({attempt}/{max_attempts})\n"
                            f"🔄 将在 {delay} 秒后重试……"
                        )

                    await self.telegram_call(
                        "download sqlite restore file",
                        lambda: self.bot.download(document, destination=uploaded_path),
                        on_retry=on_download_retry,
                    )
                    uploaded_size_text = self._format_file_size(uploaded_path.stat().st_size)

                    restore_stage = "验证恢复文件"
                    await update_status(
                        "♻️ 数据库恢复进行中\n\n"
                        "✅ 阶段 1/4：恢复文件下载完成\n"
                        f"📦 文件大小：{uploaded_size_text}\n"
                        "⏳ 阶段 2/4：正在检查 SQLite 完整性与数据表……"
                    )
                    table_counts = await asyncio.to_thread(
                        self._validate_restore_database,
                        uploaded_path,
                    )

                    restore_stage = "建立并上传恢复前备份"
                    await update_status(
                        "♻️ 数据库恢复进行中\n\n"
                        "✅ 阶段 1/4：恢复文件下载完成\n"
                        "✅ 阶段 2/4：文件验证通过\n"
                        "⏳ 阶段 3/4：正在备份当前数据库……"
                    )
                    await asyncio.to_thread(
                        self._create_sqlite_backup,
                        self.database_path,
                        current_backup_path,
                    )
                    current_backup_size_text = self._format_file_size(
                        current_backup_path.stat().st_size
                    )

                    async def on_backup_retry(
                        attempt: int,
                        max_attempts: int,
                        delay: int,
                        exc: Exception,
                    ) -> None:
                        await update_status(
                            "♻️ 数据库恢复进行中\n\n"
                            "✅ 阶段 1/4：恢复文件下载完成\n"
                            "✅ 阶段 2/4：文件验证通过\n"
                            f"⚠️ 阶段 3/4：恢复前备份上传失败 ({attempt}/{max_attempts})\n"
                            f"🔄 将在 {delay} 秒后重试……"
                        )

                    await self.telegram_call(
                        "send pre-restore sqlite backup",
                        lambda: self.bot.send_document(
                            chat_id=self.key_man_id,
                            document=FSInputFile(
                                current_backup_path,
                                filename=current_backup_path.name,
                            ),
                            caption=(
                                "⚠️ 数据库恢复前自动备份\n"
                                f"{timestamp} (UTC+8)"
                            ),
                        ),
                        on_retry=on_backup_retry,
                    )

                    restore_stage = "写入恢复数据库"
                    await update_status(
                        "♻️ 数据库恢复进行中\n\n"
                        "✅ 阶段 1/4：恢复文件下载完成\n"
                        "✅ 阶段 2/4：文件验证通过\n"
                        f"✅ 阶段 3/4：当前数据库已备份并发送 ({current_backup_size_text})\n"
                        "⏳ 阶段 4/4：正在写入数据库，请勿重复操作……"
                    )

                    try:
                        self._restore_sqlite_database(uploaded_path, self.database_path)
                        if self.user_expire_cache is not None:
                            self.user_expire_cache._load()
                        if self.blacklist_store is not None:
                            self.blacklist_store._load()
                    except Exception as restore_exc:
                        print(f"[RESTORE] restore failed, rolling back: {restore_exc}", flush=True)
                        try:
                            self._restore_sqlite_database(current_backup_path, self.database_path)
                            if self.user_expire_cache is not None:
                                self.user_expire_cache._load()
                            if self.blacklist_store is not None:
                                self.blacklist_store._load()
                        except Exception as rollback_exc:
                            raise RuntimeError(
                                f"恢复失败且自动回滚失败：{restore_exc}; "
                                f"rollback: {rollback_exc}"
                            ) from rollback_exc
                        raise RuntimeError(
                            f"恢复失败，已自动还原恢复前数据库：{restore_exc}"
                        ) from restore_exc

                restored_user_count = table_counts.get("user_expire", 0)
                restored_media_count = table_counts.get("received_media", 0)
                elapsed_seconds = int(asyncio.get_running_loop().time() - started_at)
                await update_status(
                    "✅ 数据库恢复完成\n\n"
                    f"📄 来源文件：{file_name}\n"
                    f"👤 通行证记录：{restored_user_count}\n"
                    f"📦 媒体记录：{restored_media_count}\n"
                    f"🛡️ 恢复前备份已发送给：{self.key_man_id}\n"
                    f"⏱️ 耗时：{elapsed_seconds} 秒\n\n"
                    "内存缓存已经重新载入，临时文件已自动清理。"
                )
            except Exception as exc:
                print(f"[RESTORE] failed at {restore_stage}: {exc}", flush=True)
                elapsed_seconds = int(asyncio.get_running_loop().time() - started_at)
                await update_status(
                    "❌ 数据库恢复失败\n\n"
                    f"阶段：{restore_stage}\n"
                    f"耗时：{elapsed_seconds} 秒\n"
                    f"原因：{exc}\n\n"
                    "如果恢复前备份未成功发送，原数据库不会被替换。"
                )

    def register(self, dispatcher: Dispatcher) -> None:
        dispatcher.message.register(
            self.backup,
            F.chat.type == "private",
            Command("backup"),
        )
        dispatcher.message.register(
            self.clear_media,
            F.chat.type == "private",
            Command("clear_media"),
        )
        dispatcher.message.register(
            self.restore,
            F.chat.type == "private",
            Command("restore"),
        )
