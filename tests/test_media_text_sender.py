import ast
import asyncio
import time
import unittest
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram import Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMediaGroup
from aiogram.types import InputMediaPhoto, Message

from utils.media_text_sender import MediaTextSender, RECOVERY_CAPTION_PREFIX, media_batches
from utils.telegram_gate import TelegramGate


class MediaTextSenderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sender = MediaTextSender()
        self.sender.gate = TelegramGate(send_min_interval=0)
        self.bot = SimpleNamespace(**{
            name: AsyncMock() for name in (
                "send_media_group", "send_photo", "send_video",
                "send_document", "send_message",
            )
        })
        self.bot.id = 100
        self.backup_bot = SimpleNamespace(
            id=200, send_photo=AsyncMock(), send_video=AsyncMock(),
            send_document=AsyncMock(),
        )
        self.sender.backup_gate = TelegramGate(send_min_interval=0)

    def recovery_message(self, caption, kind="p", file_id="main_file", sender_id=200):
        media = SimpleNamespace(file_id=file_id)
        return SimpleNamespace(
            caption=caption, from_user=SimpleNamespace(id=sender_id),
            video=media if kind == "v" else None,
            document=media if kind == "d" else None,
            photo=[media] if kind == "p" else [],
        )

    def relay_backup_updates(self):
        async def relay(kind, **kwargs):
            self.assertEqual(kwargs["chat_id"], self.bot.id)
            # Use the main bot's received file ID, not the backup send response ID.
            received = self.recovery_message(
                kwargs["caption"], kind, "main_" + kwargs[
                    {"p": "photo", "v": "video", "d": "document"}[kind]
                ],
            )
            self.assertTrue(self.sender.accept_recovery(received))
            self.assertFalse(self.sender.accept_recovery(received))
            return self.recovery_message(kwargs["caption"], kind, "backup_only_id")

        for kind, method in (("p", "send_photo"), ("v", "send_video"), ("d", "send_document")):
            getattr(self.backup_bot, method).side_effect = partial(relay, kind)

    def test_batches_preserve_ids_order_and_separate_documents(self):
        batches = media_batches("v_ABC- p_DEF_ d_file1 d_file2 p_last")
        self.assertEqual(batches, [
            [("v", "ABC-"), ("p", "DEF_")],
            [("d", "file1"), ("d", "file2")],
            [("p", "last")],
        ])
        self.assertEqual(media_batches("prefixv_ABC -p_ABC v_"), [])

    async def test_large_request_sends_small_albums_then_single(self):
        await self.sender.send(self.bot, 42, " ".join(f"p_id{i}" for i in range(11)))
        groups = self.bot.send_media_group.await_args_list
        self.assertEqual([len(call.kwargs["media"]) for call in groups], [5, 5])
        self.assertEqual(
            [item.media for call in groups for item in call.kwargs["media"]],
            [f"id{i}" for i in range(10)],
        )
        self.bot.send_photo.assert_awaited_once_with(chat_id=42, photo="id10")

    async def test_single_types_use_correct_methods(self):
        await self.sender.send(self.bot, 42, "v_video d_document p_photo")
        self.bot.send_video.assert_awaited_once_with(chat_id=42, video="video")
        self.bot.send_document.assert_awaited_once_with(chat_id=42, document="document")
        self.bot.send_photo.assert_awaited_once_with(chat_id=42, photo="photo")
        self.bot.send_media_group.assert_not_awaited()

    async def test_bad_album_does_not_stop_later_batch(self):
        method = SendMediaGroup(chat_id=42, media=[
            InputMediaPhoto(media="bad"), InputMediaPhoto(media="good"),
        ])
        self.bot.send_media_group.side_effect = TelegramBadRequest(method, "wrong file ID")
        await self.sender.send(self.bot, 42, "p_bad p_good d_document")
        self.bot.send_document.assert_awaited_once()
        self.assertIn("2", self.bot.send_message.await_args.kwargs["text"])

    async def test_uncertain_network_failure_does_not_replay_album(self):
        method = SendMediaGroup(chat_id=42, media=[
            InputMediaPhoto(media="a"), InputMediaPhoto(media="b"),
        ])
        self.bot.send_media_group.side_effect = [
            None, TelegramNetworkError(method, "offline"), None,
        ]
        with self.assertRaises(TelegramNetworkError):
            await self.sender.send(
                self.bot, 42, " ".join(f"p_id{i}" for i in range(10)), self.backup_bot
            )
        attempts = self.bot.send_media_group.await_args_list
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].kwargs["media"][0].media, "id0")
        self.assertEqual(attempts[1].kwargs["media"][0].media, "id5")
        self.backup_bot.send_photo.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_concurrent_requests_share_chat_pacing(self):
        self.sender.gate = TelegramGate(send_min_interval=0.03)
        sent_at = []

        async def send_photo(**kwargs):
            sent_at.append(time.monotonic())

        self.bot.send_photo.side_effect = send_photo
        await asyncio.gather(
            self.sender.send(self.bot, 42, "p_first"),
            self.sender.send(self.bot, 42, "p_second"),
        )
        self.assertEqual(len(sent_at), 2)
        self.assertGreaterEqual(sent_at[1] - sent_at[0], 0.025)

    async def test_no_codes_sends_nothing(self):
        await self.sender.send(self.bot, 42, "普通文字")
        for method in vars(self.bot).values():
            if isinstance(method, AsyncMock):
                method.assert_not_awaited()

    async def test_single_recovery_all_types_uses_main_received_file_id(self):
        self.relay_backup_updates()
        for kind, method, parameter in (
            ("p", "send_photo", "photo"), ("v", "send_video", "video"),
            ("d", "send_document", "document"),
        ):
            with self.subTest(kind=kind):
                send = getattr(self.bot, method)
                send.side_effect = [
                    TelegramBadRequest(SendMediaGroup(chat_id=42, media=[]), "wrong file ID"),
                    None,
                ]
                await self.sender.send(self.bot, 42, f"{kind}_old", self.backup_bot)
                self.assertEqual(send.await_count, 2)
                self.assertEqual(send.await_args.kwargs, {"chat_id": 42, parameter: "main_old"})
                self.assertFalse(self.sender._pending)
        self.bot.send_message.assert_not_awaited()

    async def test_failed_album_recovered_then_resent_once(self):
        self.relay_backup_updates()
        self.bot.send_media_group.side_effect = [
            TelegramBadRequest(SendMediaGroup(chat_id=42, media=[]), "wrong file ID"), None,
        ]
        await self.sender.send(self.bot, 42, "p_one v_two", self.backup_bot)
        self.assertEqual(self.bot.send_media_group.await_count, 2)
        recovered = self.bot.send_media_group.await_args.kwargs["media"]
        self.assertEqual([item.media for item in recovered], ["main_one", "main_two"])
        self.bot.send_message.assert_not_awaited()

    async def test_second_send_failure_does_not_start_another_recovery(self):
        self.relay_backup_updates()
        self.bot.send_photo.side_effect = TelegramBadRequest(
            SendMediaGroup(chat_id=42, media=[]), "wrong file ID",
        )
        await self.sender.send(self.bot, 42, "p_old", self.backup_bot)
        self.assertEqual(self.bot.send_photo.await_count, 2)
        self.backup_bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()

    async def test_backup_failure_is_reported_without_resend(self):
        error = TelegramBadRequest(SendMediaGroup(chat_id=42, media=[]), "wrong file ID")
        self.bot.send_photo.side_effect = error
        self.backup_bot.send_photo.side_effect = error
        await self.sender.send(self.bot, 42, "p_old", self.backup_bot)
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()
        self.assertFalse(self.sender._pending)

    async def test_recovery_timeout_cleans_up_and_ignores_late_update(self):
        self.bot.send_photo.side_effect = TelegramBadRequest(
            SendMediaGroup(chat_id=42, media=[]), "wrong file ID",
        )
        with patch("utils.media_text_sender.RECOVERY_TIMEOUT_SECONDS", 0.01):
            await self.sender.send(self.bot, 42, "p_old", self.backup_bot)
        caption = self.backup_bot.send_photo.await_args.kwargs["caption"]
        self.assertFalse(self.sender._pending)
        self.assertFalse(self.sender.accept_recovery(self.recovery_message(caption)))
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()

    async def test_wrong_sender_or_media_type_cannot_complete_recovery(self):
        recovery = asyncio.create_task(self.sender._recover_file(
            self.bot, self.backup_bot, "p", "old",
        ))
        await asyncio.sleep(0)
        caption = self.backup_bot.send_photo.await_args.kwargs["caption"]
        self.assertFalse(self.sender.accept_recovery(self.recovery_message(caption, sender_id=999)))
        self.assertFalse(self.sender.accept_recovery(self.recovery_message(caption, kind="d")))
        self.assertTrue(self.sender.accept_recovery(self.recovery_message(caption)))
        self.assertEqual(await recovery, "main_file")
        self.assertFalse(self.sender._pending)

    async def test_cancelled_recovery_cleans_pending_state(self):
        recovery = asyncio.create_task(self.sender._recover_file(
            self.bot, self.backup_bot, "p", "old",
        ))
        await asyncio.sleep(0)
        self.assertTrue(self.sender._pending)
        recovery.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await recovery
        self.assertFalse(self.sender._pending)

    async def test_late_recovery_update_does_not_reach_upload_handler(self):
        source = Path(__file__).resolve().parents[1] / "main_taohua.py"
        tree = ast.parse(source.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "on_backup_media_recovery")
        dispatcher = Dispatcher()
        receiver = SimpleNamespace(accept_recovery=Mock(return_value=False))
        namespace = {
            "dp": dispatcher, "F": F, "Message": Message,
            "bkbot": self.backup_bot, "media_text_sender": receiver,
            "RECOVERY_CAPTION_PREFIX": RECOVERY_CAPTION_PREFIX,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
        upload = AsyncMock()

        async def normal_upload(message):
            await upload(message)

        dispatcher.message.register(normal_upload)
        message = Message(
            message_id=1, date=datetime.now(timezone.utc),
            chat={"id": 200, "type": "private"},
            from_user={"id": 200, "is_bot": True, "first_name": "backup"},
            caption=RECOVERY_CAPTION_PREFIX + "expired",
            photo=[{"file_id": "main_id", "file_unique_id": "unique", "width": 10, "height": 10}],
        )
        await dispatcher.message.trigger(message)
        receiver.accept_recovery.assert_called_once_with(message)
        upload.assert_not_awaited()
