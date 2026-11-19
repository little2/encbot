import ast
import asyncio
from contextlib import asynccontextmanager
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
    def test_send_all_media_returns_selected_bot_username(self):
        sent_message = object()
        send_with_bot = AsyncMock(return_value=([sent_message], []))
        main_bot = SimpleNamespace(token="main-token", get_me=AsyncMock())
        selected_bot = SimpleNamespace(
            token="selected-token",
            get_me=AsyncMock(return_value=SimpleNamespace(username="runway_bot")),
        )

        class Cache:
            @asynccontextmanager
            async def use(self, token):
                self.assertEqual(token, "selected-token")
                yield selected_bot

        cache = Cache()
        cache.assertEqual = self.assertEqual
        namespace = {
            "Message": object,
            "Bot": object,
            "Any": object,
            "bot": main_bot,
            "bot_name": "main_bot",
            "SEND_BOT_CACHE": cache,
            "_send_all_media_with_bot": send_with_bot,
        }
        send_all = load_function("_send_all_media", namespace)

        result = asyncio.run(
            send_all(object(), {"file_id": "id", "file_type": "video"}, 123, "selected-token")
        )

        self.assertEqual(result, ([sent_message], [], "runway_bot"))
        selected_bot.get_me.assert_awaited_once_with()
        main_bot.get_me.assert_not_awaited()

    def test_media_recovery_failure_skips_item_and_continues(self):
        class BadRequest(Exception):
            message = "wrong file identifier"

        async def run_case(recovery_result, retry_fails=False, userbot_enabled=False):
            recovered_message = object()
            next_message = object()
            responses = [BadRequest()]
            if recovery_result and not userbot_enabled:
                responses.append(BadRequest() if retry_fails else recovered_message)
            responses.append(next_message)
            send_media = AsyncMock(side_effect=responses)
            lock = asyncio.Lock()

            async def request(*args):
                self.assertTrue(lock.locked())
                return recovery_result

            request_media = AsyncMock(side_effect=request)
            sending_bot = SimpleNamespace(send_message=AsyncMock())
            received_media_store = SimpleNamespace(
                get_by_media_identity=AsyncMock(return_value=[]),
            )
            namespace = {
                "Message": object, "Bot": object, "Any": object,
                "TelegramBadRequest": BadRequest,
                "_send_media_with_bot": send_media,
                "_is_invalid_media_reference_error": lambda exc: True,
                "_normalize_receiver_chat_id": lambda value, **kwargs: value,
                "_album_kind": lambda kind: None,
                "backup_bot": object(),
                "bot": SimpleNamespace(send_message=AsyncMock()),
                "os": SimpleNamespace(getenv=lambda *args: "true" if userbot_enabled else "false"),
                "BACKUP_FORWARD_SEND_LOCK": lock,
                "TelegramFileId": SimpleNamespace(file_id_to_unique_id=lambda value: "unique-id"),
                "_request_x_man": request_media,
                "received_media_store": received_media_store,
                "active_userbot": None,
                "media_identity": lambda file_id: {
                    "media_type": "document", "media_id": 123,
                },
            }
            send_all = load_function("_send_all_media_with_bot", namespace)
            items = [
                {"file_type": "sticker", "file_id": "bad-file"},
                {"file_type": "sticker", "file_id": "good-file"},
            ]
            sent, skipped = await asyncio.wait_for(
                send_all(SimpleNamespace(from_user=None), {"items": items}, 123, sending_bot),
                timeout=1,
            )
            recovered = bool(recovery_result) and not retry_fails and not userbot_enabled
            self.assertEqual(sent, [recovered_message, next_message] if recovered else [next_message])
            expected_skipped_item = (
                {**items[0], "_item_index": 1, "file_id": recovery_result}
                if recovery_result and retry_fails and not userbot_enabled
                else {**items[0], "_item_index": 1}
            )
            self.assertEqual(skipped, [] if recovered else [expected_skipped_item])
            self.assertEqual(request_media.await_count, 0 if userbot_enabled else 1)
            self.assertEqual(
                received_media_store.get_by_media_identity.await_count,
                1 if userbot_enabled else 0,
            )
            self.assertFalse(lock.locked())

            if recovery_result and not userbot_enabled:
                self.assertEqual(send_media.await_args_list[1].args[2], recovery_result)

        for result, retry_fails, enabled in (
            (None, False, False),
            ("recovered-file-id", False, False),
            ("recovered-file-id", True, False),
            (None, False, True),
        ):
            with self.subTest(result=result, retry_fails=retry_fails, userbot_enabled=enabled):
                asyncio.run(run_case(result, retry_fails, enabled))

    def test_userbot_recovery_uses_stored_message_location(self):
        class BadRequest(Exception):
            message = "wrong file identifier"

        async def run_case():
            delivered_message = SimpleNamespace(message_id=444)
            sending_bot = SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(id=77, username="runway_bot")),
                send_message=AsyncMock(),
                forward_message=AsyncMock(return_value=delivered_message),
            )
            active_userbot = SimpleNamespace(
                user_id=55,
                forward_stored_message=AsyncMock(return_value=333),
            )
            received_media_store = SimpleNamespace(
                get_by_media_identity=AsyncMock(return_value=[{
                    "file_unique_id": "unique-id",
                    "storage_chat_id": -100123,
                    "storage_message_id": 222,
                }]),
            )
            namespace = {
                "Message": object, "Bot": object, "Any": object,
                "TelegramBadRequest": BadRequest,
                "_send_media_with_bot": AsyncMock(side_effect=BadRequest()),
                "_is_invalid_media_reference_error": lambda exc: True,
                "_normalize_receiver_chat_id": lambda value, **kwargs: value,
                "_album_kind": lambda kind: None,
                "bot": SimpleNamespace(send_message=AsyncMock()),
                "os": SimpleNamespace(getenv=lambda *args: "true"),
                "received_media_store": received_media_store,
                "active_userbot": active_userbot,
                "media_identity": lambda file_id: {
                    "media_type": "document", "media_id": 123,
                },
            }
            send_all = load_function("_send_all_media_with_bot", namespace)

            sent, skipped = await send_all(
                SimpleNamespace(from_user=None),
                {"items": [{"file_type": "video", "file_id": "old-file-id"}]},
                123,
                sending_bot,
            )

            self.assertEqual(sent, [delivered_message])
            self.assertEqual(skipped, [])
            received_media_store.get_by_media_identity.assert_awaited_once_with(
                "document", 123,
            )
            active_userbot.forward_stored_message.assert_awaited_once_with(
                -100123, 222, "@runway_bot"
            )
            sending_bot.forward_message.assert_awaited_once_with(
                chat_id=123,
                from_chat_id=55,
                message_id=333,
                protect_content=False,
            )

        asyncio.run(run_case())

    def test_request_x_man_requires_media_reply(self):
        async def run_case(reply, expected, custom_bot=False, send_error=False):
            default_bot = SimpleNamespace(send_message=AsyncMock())
            selected_bot = (
                SimpleNamespace(send_message=AsyncMock(), get_updates=AsyncMock())
                if custom_bot else default_bot
            )
            namespace = {
                "Bot": object,
                "asyncio": asyncio,
                "bot": default_bot,
                "SharedConfig": SimpleNamespace(get=lambda *args: 123),
                "X_MAN_REQUEST_LOCK": asyncio.Lock(),
                "X_MAN_REPLY_TIMEOUT_SECONDS": 0.01,
            }

            async def send_message(*args):
                if send_error:
                    raise RuntimeError("send failed")
                if reply is not None and not custom_bot:
                    namespace["X_MAN_PENDING_REPLY"].set_result(reply)
                return SimpleNamespace(message_id=321)

            selected_bot.send_message.side_effect = send_message
            if custom_bot:
                namespace["_wait_for_x_man_reply_with_bot"] = AsyncMock(
                    return_value=reply
                )
            request = load_function("_request_x_man", namespace)
            result = await request("unique-id", selected_bot if custom_bot else None)

            self.assertEqual(result, expected)
            selected_bot.send_message.assert_awaited_once_with(123, "unique-id")
            if custom_bot:
                default_bot.send_message.assert_not_awaited()
            for key in (
                "X_MAN_PENDING_REPLY",
                "X_MAN_PENDING_REQUEST_MESSAGE_ID",
                "X_MAN_PENDING_FILE_UNIQUE_ID",
            ):
                self.assertIsNone(namespace[key])

        for reply, expected in (
            ("media-file-id", "media-file-id"),
            ("na", None),
            ("", None),
            (None, None),
        ):
            for custom_bot in (False, True):
                with self.subTest(reply=reply, custom_bot=custom_bot):
                    asyncio.run(run_case(reply, expected, custom_bot))
        asyncio.run(run_case(None, None, send_error=True))

    def test_dynamic_sending_bot_waits_for_matching_media_reply(self):
        matching_request = SimpleNamespace(
            message_id=321,
            text="unique-id",
            caption=None,
        )
        media_message = SimpleNamespace(
            from_user=SimpleNamespace(id=777),
            reply_to_message=matching_request,
        )
        text_message = SimpleNamespace(
            from_user=SimpleNamespace(id=777),
            reply_to_message=matching_request,
        )

        async def run_case(message, extracted_file_id):
            update = SimpleNamespace(update_id=10, message=message)
            active_bot = SimpleNamespace(get_updates=AsyncMock(return_value=[update]))
            namespace = {
                "Bot": object,
                "asyncio": asyncio,
                "SharedConfig": SimpleNamespace(get=lambda *args: 777),
                "X_MAN_REPLY_TIMEOUT_SECONDS": 1,
                "_get_x_man_reply_file_id": lambda incoming: extracted_file_id,
            }
            wait_for_reply = load_function(
                "_wait_for_x_man_reply_with_bot", namespace
            )

            result = await wait_for_reply(active_bot, 321, "unique-id")

            self.assertEqual(
                result,
                None if extracted_file_id == "na" else extracted_file_id,
            )
            active_bot.get_updates.assert_awaited_once()

        asyncio.run(run_case(media_message, "recovered-file-id"))
        asyncio.run(run_case(text_message, "na"))

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

    def test_disabled_backup_forwarding_only_sends_to_x_man(self):
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
            ],
        )
