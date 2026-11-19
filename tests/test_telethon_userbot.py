import asyncio
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
