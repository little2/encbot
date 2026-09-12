from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from video_bot import _build_dispatcher


class VideoBotCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = Mock()
        self.dispatcher = _build_dispatcher(
            store=self.store,
            bot_name="video_test_bot",
            video_bot=Mock(),
            airport_lobby_group_id=-1001234567890,
            paid_invite_lifetime_hours=24,
        )

    def _handler(self, name: str):
        return next(
            handler.callback
            for handler in self.dispatcher.message.handlers
            if handler.callback.__name__ == name
        )

    async def test_board_replies_with_fixed_channel_url(self) -> None:
        message = SimpleNamespace(reply=AsyncMock())

        await self._handler("board")(message)

        message.reply.assert_awaited_once_with("https://t.me/ztflybot")

    async def test_check_reports_when_user_has_no_videos(self) -> None:
        self.store.get_top_videos.return_value = []
        self.store.get_total_view_count.return_value = 0
        self.store.get_recent_view_count.return_value = 0
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=123456),
            reply=AsyncMock(),
        )

        await self._handler("check")(message)

        self.store.get_top_videos.assert_called_once_with(123456, limit=10)
        message.reply.assert_awaited_once_with("你还没有上传任何视频。")


if __name__ == "__main__":
    unittest.main()
