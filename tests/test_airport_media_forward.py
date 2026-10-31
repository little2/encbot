import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, call


SOURCE = Path(__file__).resolve().parents[1] / "main_airport.py"


def load_function(name, namespace):
    tree = ast.parse(SOURCE.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"),
        namespace,
    )
    return namespace[name]


class AirportMediaForwardTests(unittest.TestCase):
    def test_x_man_reply_returns_media_file_id_or_na(self):
        def extract_media(message):
            if message == "media":
                return "video", "x-man-video-file-id"
            raise ValueError("not media")

        get_file_id = load_function(
            "_get_x_man_reply_file_id",
            {
                "Message": object,
                "_extract_media_info": extract_media,
            },
        )

        self.assertEqual(get_file_id("media"), "x-man-video-file-id")
        self.assertEqual(get_file_id("text"), "na")

    def test_forward_media_uses_matching_send_method(self):
        sent_message = object()
        bot = SimpleNamespace(
            send_document=AsyncMock(),
            send_photo=AsyncMock(),
            send_video=AsyncMock(return_value=sent_message),
            send_audio=AsyncMock(),
            send_voice=AsyncMock(),
            send_animation=AsyncMock(),
            send_sticker=AsyncMock(),
        )
        send_forward = load_function(
            "_send_forward_media_by_type",
            {
                "MediaForwardTask": object,
                "Message": object,
                "bot": bot,
                "_normalize_receiver_chat_id": lambda value: value,
            },
        )
        task = SimpleNamespace(file_type="video", file_id="video-file-id")

        result = asyncio.run(send_forward(task, -1001234567890))

        self.assertIs(result, sent_message)
        bot.send_video.assert_awaited_once_with(
            chat_id=-1001234567890,
            video="video-file-id",
        )
        bot.send_document.assert_not_awaited()

    def test_receiver_chat_id_normalizes_numbers_and_usernames(self):
        normalize = load_function(
            "_normalize_receiver_chat_id",
            {"re": __import__("re")},
        )

        self.assertEqual(normalize(123), 123)
        self.assertEqual(normalize("-1001234567890"), -1001234567890)
        self.assertEqual(normalize("airport_news"), "@airport_news")
        self.assertEqual(normalize("@airport_news"), "@airport_news")
        self.assertEqual(normalize(None, default_chat_id=456), 456)

    def test_receiver_chat_id_rejects_invalid_values(self):
        normalize = load_function(
            "_normalize_receiver_chat_id",
            {"re": __import__("re")},
        )

        for receiver_id in (0, "", "https://t.me/airport_news", "bad name"):
            with self.subTest(receiver_id=receiver_id):
                with self.assertRaises(ValueError):
                    normalize(receiver_id)

    def test_takeoff_batch_id_accepts_supported_bot_names(self):
        extract = load_function(
            "_extract_takeoff_batch_id", {"re": __import__("re")}
        )
        batch_id = "Ab3_kL9-xY2pQr7Z"

        self.assertEqual(extract(f"zttower12bot_{batch_id}"), batch_id)
        self.assertEqual(extract(f"afu12bot_{batch_id}"), batch_id)
        self.assertEqual(extract(f"AFU12BOT_{batch_id}"), batch_id)

    def test_takeoff_batch_id_rejects_unsupported_bot_names(self):
        extract = load_function(
            "_extract_takeoff_batch_id", {"re": __import__("re")}
        )

        self.assertIsNone(extract("other12bot_Ab3_kL9-xY2pQr7Z"))

    def test_backup_bot_username_is_sent_as_telegram_chat_username(self):
        send_to_destination = AsyncMock(return_value=True)
        namespace = {
            "asyncio": asyncio,
            "MediaForwardTask": object,
            "APRON_CHANNEL_IDS": [],
            "X_MAN_BOT_ID": 123,
            "BACKUP_BOT_NAME": "zttowerpoolbot",
            "MEDIA_FORWARD_INTERVAL_SECONDS": 0,
            "_send_media_forward_to_destination": send_to_destination,
        }
        forward = load_function("_forward_media_in_background", namespace)
        task = object()

        asyncio.run(forward(task))

        self.assertEqual(
            send_to_destination.await_args_list,
            [
                call(task, "X_MAN_BOT_ID", 123),
                call(task, "BACKUP_BOT_NAME", "@zttowerpoolbot"),
            ],
        )
