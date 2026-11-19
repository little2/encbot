import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest

from utils.format_utils import FormatUtils


class FormatUtilsTests(unittest.TestCase):
    def test_user_hyperlink_falls_back_to_id_when_chat_is_not_found(self):
        bot = SimpleNamespace(
            get_chat=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=SimpleNamespace(),
                    message="Bad Request: chat not found",
                )
            )
        )

        result = asyncio.run(
            FormatUtils.get_user_hyperlink(bot, {"id": 123456}, show_uid=True)
        )

        self.assertEqual(
            result,
            "<a href='tg://user?id=123456'>123456</a> <code>123456</code>",
        )

    def test_user_hyperlink_does_not_hide_other_bad_requests(self):
        bot = SimpleNamespace(
            get_chat=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=SimpleNamespace(),
                    message="Bad Request: another error",
                )
            )
        )

        with self.assertRaises(TelegramBadRequest):
            asyncio.run(FormatUtils.get_user_hyperlink(bot, {"id": 123456}))


if __name__ == "__main__":
    unittest.main()
