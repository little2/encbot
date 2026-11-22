import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import telethon_userbot
from telethon_userbot import TelethonUserbot


class TelethonUserbotTests(unittest.TestCase):
    def test_serialized_session_uses_string_session(self):
        resolved = object()
        with patch.object(telethon_userbot, "StringSession", return_value=resolved) as factory:
            result = TelethonUserbot._resolve_session("serialized-session-value")

        self.assertIs(result, resolved)
        factory.assert_called_once_with("serialized-session-value")

    def test_file_session_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "sessions" / "userbot"

            result = TelethonUserbot._resolve_session(str(session_path))

            self.assertEqual(result, str(session_path))
            self.assertTrue(session_path.parent.is_dir())

    def test_user_id_is_available_even_if_startup_notification_fails(self):
        userbot = TelethonUserbot.__new__(TelethonUserbot)
        userbot.phone = ""
        userbot.user_id = None
        userbot.client = SimpleNamespace(
            start=AsyncMock(),
            get_me=AsyncMock(return_value=SimpleNamespace(id=123, bot=False)),
            send_message=AsyncMock(side_effect=RuntimeError("unavailable")),
            run_until_disconnected=AsyncMock(),
            disconnect=AsyncMock(),
        )

        observed_user_id = None

        async def observe():
            nonlocal observed_user_id
            observed_user_id = userbot.user_id

        userbot.client.run_until_disconnected.side_effect = observe
        asyncio.run(userbot.run(notify_bot="@example_bot"))

        self.assertEqual(observed_user_id, 123)
        self.assertIsNone(userbot.user_id)
        userbot.client.disconnect.assert_awaited_once_with()

    def test_forward_stored_message_returns_destination_message_id(self):
        userbot = TelethonUserbot.__new__(TelethonUserbot)
        userbot.client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value=-100123),
            forward_messages=AsyncMock(return_value=SimpleNamespace(id=987)),
        )

        result = asyncio.run(
            userbot.forward_stored_message(-100123, 456, "@receiving_bot")
        )

        self.assertEqual(result, 987)
        userbot.client.forward_messages.assert_awaited_once_with(
            entity="@receiving_bot",
            messages=456,
            from_peer=-100123,
        )


    def test_deleted_message_is_distinct_from_forwarding_errors(self):
        userbot = TelethonUserbot.__new__(TelethonUserbot)
        forward = AsyncMock(side_effect=telethon_userbot.errors.MessageIdInvalidError(request=None))
        userbot.client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value=101), forward_messages=forward,
        )
        with self.assertRaises(FileNotFoundError):
            asyncio.run(userbot.forward_stored_message(101, 17, "@receiving_bot"))
        forward.side_effect = ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            asyncio.run(userbot.forward_stored_message(101, 17, "@receiving_bot"))

    def test_missing_source_entity_is_resolved_from_dialogs(self):
        async def run_case():
            peer = telethon_userbot.types.InputPeerUser(101, 999)
            userbot = TelethonUserbot.__new__(TelethonUserbot)

            async def dialogs():
                yield SimpleNamespace(id=42, input_entity=object())
                yield SimpleNamespace(id=101, input_entity=peer)

            userbot.client = SimpleNamespace(
                get_input_entity=AsyncMock(side_effect=ValueError("not cached")),
                iter_dialogs=dialogs,
                forward_messages=AsyncMock(return_value=SimpleNamespace(id=987)),
            )
            result = await userbot.forward_stored_message(101, 17, "@receiving_bot")
            self.assertEqual(result, 987)
            userbot.client.forward_messages.assert_awaited_once_with(
                entity="@receiving_bot", messages=17, from_peer=peer,
            )

        asyncio.run(run_case())

    def test_missing_source_entity_can_use_current_bot_username(self):
        async def run_case():
            peer = telethon_userbot.types.InputPeerUser(101, 999)
            userbot = TelethonUserbot.__new__(TelethonUserbot)
            userbot.client = SimpleNamespace(
                get_input_entity=AsyncMock(side_effect=[ValueError("not cached"), peer]),
                forward_messages=AsyncMock(return_value=SimpleNamespace(id=987)),
            )
            result = await userbot.forward_stored_message(
                101, 17, "@receiving_bot", source_username="example_bot",
            )
            self.assertEqual(result, 987)
            userbot.client.forward_messages.assert_awaited_once_with(
                entity="@receiving_bot", messages=17, from_peer=peer,
            )

        asyncio.run(run_case())

    def test_missing_source_entity_without_dialog_is_explicit(self):
        async def run_case():
            userbot = TelethonUserbot.__new__(TelethonUserbot)

            async def dialogs():
                if False:
                    yield

            userbot.client = SimpleNamespace(
                get_input_entity=AsyncMock(side_effect=ValueError("not cached")),
                iter_dialogs=dialogs,
                forward_messages=AsyncMock(),
            )
            with self.assertRaisesRegex(ValueError, "cannot resolve chat ID 101"):
                await userbot.forward_stored_message(101, 17, "@receiving_bot")
            userbot.client.forward_messages.assert_not_awaited()

        asyncio.run(run_case())

    def test_missing_bot_entity_can_be_resolved_by_username(self):
        async def run_case():
            peer = telethon_userbot.types.InputPeerUser(101, 999)
            userbot = TelethonUserbot.__new__(TelethonUserbot)
            conversation = SimpleNamespace(get_response=AsyncMock())

            @asynccontextmanager
            async def open_conversation(entity, *, exclusive):
                self.assertEqual(entity, peer)
                self.assertFalse(exclusive)
                yield conversation

            userbot.client = SimpleNamespace(
                get_input_entity=AsyncMock(side_effect=[ValueError("not cached"), peer]),
                conversation=open_conversation,
            )
            async with userbot.observe_bot_upload(101, bot_username="example_bot"):
                pass
            self.assertEqual(
                userbot.client.get_input_entity.await_args_list,
                [unittest.mock.call(101), unittest.mock.call("@example_bot")],
            )

        asyncio.run(run_case())

    def test_wrong_username_does_not_resolve_another_bot(self):
        async def run_case():
            userbot = TelethonUserbot.__new__(TelethonUserbot)
            userbot.client = SimpleNamespace(
                get_input_entity=AsyncMock(side_effect=[
                    ValueError("not cached"), telethon_userbot.types.InputPeerUser(102, 999),
                ]),
            )
            with self.assertRaisesRegex(ValueError, "101"):
                async with userbot.observe_bot_upload(101, bot_username="wrong_bot"):
                    pass

        asyncio.run(run_case())

    def test_observe_upload_matches_the_message_in_the_userbots_private_chat(self):
        async def run_case(sticker_id):
            userbot = TelethonUserbot.__new__(TelethonUserbot)
            unrelated = SimpleNamespace(media=object(), raw_text="other", sticker=SimpleNamespace(id=99))
            incoming = SimpleNamespace(media=object(), raw_text="tag" if sticker_id is None else "",
                                       sticker=SimpleNamespace(id=42), chat_id=101, id=222)
            conversation = SimpleNamespace(get_response=AsyncMock(side_effect=[unrelated, incoming]))

            @asynccontextmanager
            async def open_conversation(bot_id, *, exclusive):
                self.assertEqual(bot_id, 101)
                self.assertFalse(exclusive)
                yield conversation

            userbot.client = SimpleNamespace(
                get_input_entity=AsyncMock(return_value=101),
                conversation=open_conversation,
            )
            async with userbot.observe_bot_upload(101) as locate:
                self.assertEqual(await locate("tag", sticker_id), (101, 222))
            self.assertEqual(conversation.get_response.await_count, 2)

        for sticker_id in (None, 42):
            with self.subTest(sticker_id=sticker_id):
                asyncio.run(run_case(sticker_id))


if __name__ == "__main__":
    unittest.main()
