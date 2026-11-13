import ast
import asyncio
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from utils.emoji_utils import EmojiUtils


class AirportCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = Path(__file__).resolve().parents[1] / "main_airport.py"
        tree = ast.parse(source.read_text())
        names = {"_extract_caption", "on_media", "_finish_upload"}
        nodes = [node for node in tree.body if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name in names]
        for node in nodes:
            node.decorator_list = []
        self.ns = {
            "Message": object, "Any": object, "asyncio": asyncio,
            "EmojiUtils": EmojiUtils,
            "blacklist_store": SimpleNamespace(is_blocked=AsyncMock(return_value=False)),
            "received_media_store": SimpleNamespace(is_accepted=AsyncMock(return_value=False)),
            "UPLOAD_SESSIONS": {}, "USER_MEDIA_PENDING": {}, "ENCODER_UI_STATE": {},
            "MAX_USER_PENDING": 10, "MAX_BATCH_MEDIA": 10, "MEDIA_QUEUE": asyncio.Queue(),
            "_extract_media_info": lambda message: ("video", str(message.message_id)),
            "_extract_media_unique_id": lambda message: str(message.message_id),
            "_notify_media_limit": AsyncMock(),
            "_build_token_and_encoded": lambda state: ("token", "encoded", {}),
            "_build_display": AsyncMock(return_value="preview"),
            "_build_controls_keyboard": AsyncMock(return_value=None),
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), self.ns)

    def message(self, caption, message_id=1):
        return SimpleNamespace(
            caption=caption, message_id=message_id,
            chat=SimpleNamespace(id=123), from_user=SimpleNamespace(id=456),
            reply=AsyncMock(return_value=SimpleNamespace(message_id=99)),
        )

    async def upload(self, message):
        with contextlib.redirect_stdout(io.StringIO()):
            await self.ns["on_media"](message)

    async def finish(self, message):
        key = (123, 456)
        session = self.ns["UPLOAD_SESSIONS"][key]
        session["items"] = [{"file_id": "1", "file_type": "video", "duration": 10}]
        await self.ns["_finish_upload"](key, message, session)
        return self.ns["ENCODER_UI_STATE"][(123, 99)]

    async def test_caption_reaches_batch_state_without_prompting_for_input(self):
        message = self.message("自动提取的内容介绍\n💽媒体详情")
        await self.upload(message)
        state = await self.finish(message)
        self.assertEqual(state["batch_content"], "自动提取的内容介绍")
        self.assertFalse(state["editing_content"])

    async def test_emoji_brackets_are_removed_from_batch_content(self):
        message = self.message("[👒🎤🚋📢🌀🕞👁️🤼‍♂️][番外篇]网调\n💽详情")
        await self.upload(message)
        state = await self.finish(message)
        self.assertEqual(state["batch_content"], "[番外篇]网调")

    async def test_no_caption_keeps_manual_content_flow(self):
        message = self.message(None)
        await self.upload(message)
        state = await self.finish(message)
        self.assertEqual(state["batch_content"], "")

    async def test_first_nonempty_caption_is_kept(self):
        await self.upload(self.message(None))
        await self.upload(self.message("第一份内容介绍📹详情", 2))
        message = self.message("另一份内容介绍💽详情", 3)
        await self.upload(message)
        state = await self.finish(message)
        self.assertEqual(state["batch_content"], "第一份内容介绍")

    async def test_rejected_duplicate_does_not_supply_batch_content(self):
        self.ns["received_media_store"].is_accepted.return_value = True
        await self.upload(self.message("重复媒体介绍💽详情"))
        self.assertEqual(self.ns["UPLOAD_SESSIONS"], {})
        self.assertTrue(self.ns["MEDIA_QUEUE"].empty())
