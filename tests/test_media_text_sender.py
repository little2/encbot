import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMediaGroup
from aiogram.types import InputMediaPhoto

from utils.media_text_sender import MediaTextSender, media_batches
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

    async def test_network_retry_does_not_replay_successful_album(self):
        method = SendMediaGroup(chat_id=42, media=[
            InputMediaPhoto(media="a"), InputMediaPhoto(media="b"),
        ])
        self.bot.send_media_group.side_effect = [
            None, TelegramNetworkError(method, "offline"), None,
        ]
        with patch("utils.telegram_gate.asyncio.sleep", new=AsyncMock()):
            await self.sender.send(self.bot, 42, " ".join(f"p_id{i}" for i in range(10)))
        attempts = self.bot.send_media_group.await_args_list
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts[0].kwargs["media"][0].media, "id0")
        self.assertEqual(attempts[1].kwargs["media"][0].media, "id5")
        self.assertEqual(attempts[2].kwargs["media"][0].media, "id5")

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
            method.assert_not_awaited()
