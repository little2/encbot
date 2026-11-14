import ast
import asyncio
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from utils.emoji_utils import EmojiUtils


class AirportCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        source = Path(__file__).resolve().parents[1] / "main_airport.py"
        tree = ast.parse(source.read_text())
        names = {
            "parse_bj_tag_for_file", "_extract_caption", "_normalize_tag_list",
            "_ordered_tags", "_update_upload_panel", "on_media", "_finish_upload",
            "_prepare_queued_media_thumbnail", "_wait_for_pending_thumbnails",
            "_process_queued_media",
        }
        nodes = [node for node in tree.body if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name in names]
        for node in nodes:
            node.decorator_list = []
        class MediaForwardTask:
            def __init__(self, **values):
                self.__dict__.update(values)

        self.ns = {
            "Message": object, "Any": object, "asyncio": asyncio,
            "MediaForwardTask": MediaForwardTask,
            "EmojiUtils": EmojiUtils,
            "blacklist_store": SimpleNamespace(is_blocked=AsyncMock(return_value=False)),
            "received_media_store": SimpleNamespace(is_accepted=AsyncMock(return_value=False)),
            "UPLOAD_SESSIONS": {}, "USER_MEDIA_PENDING": {}, "ENCODER_UI_STATE": {},
            "PENDING_THUMBNAIL_TASKS": {},
            "USER_MEDIA_LOCKS": {}, "DEFAULT_TAG_CHOICES": [],
            "MAX_USER_PENDING": 10, "MAX_BATCH_MEDIA": 10, "MEDIA_QUEUE": asyncio.Queue(),
            "bot": SimpleNamespace(edit_message_text=AsyncMock()),
            "_upload_keyboard": lambda *, processing=False: {"processing": processing},
            "_extract_media_info": lambda message: ("video", str(message.message_id)),
            "_extract_media_unique_id": lambda message: str(message.message_id),
            "_extract_preview_info": lambda message, file_type, file_id: {
                "thumb_file_id": f"thumb-{file_id}",
            },
            "_extract_media_metadata": lambda message, file_type: {"duration": 10},
            "_prepare_thumbnail": AsyncMock(return_value=("phash", b"thumb")),
            "_enqueue_media_forward": Mock(),
            "_notify_media_limit": AsyncMock(),
            "_build_token_and_encoded": lambda state: ("token", "encoded", {}),
            "_build_display": AsyncMock(return_value="preview"),
            "_build_controls_keyboard": AsyncMock(return_value=None),
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), self.ns)

    def message(self, caption, message_id=1):
        return SimpleNamespace(
            text=None, entities=[], caption=caption, caption_entities=[], message_id=message_id,
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

    async def test_concurrent_album_items_share_one_upload_session(self):
        all_checked = asyncio.Event()
        check_count = 0

        async def is_accepted(_file_unique_id):
            nonlocal check_count
            check_count += 1
            if check_count == 3:
                all_checked.set()
            await all_checked.wait()
            return False

        self.ns["received_media_store"].is_accepted.side_effect = is_accepted
        messages = [self.message(None, message_id) for message_id in (1, 2, 3)]
        with contextlib.redirect_stdout(io.StringIO()):
            await asyncio.gather(*(self.ns["on_media"](message) for message in messages))

        session = self.ns["UPLOAD_SESSIONS"][(123, 456)]
        self.assertEqual(session["accepted_count"], 3)
        self.assertEqual(session["file_unique_ids"], {"1", "2", "3"})
        self.assertEqual(sum(message.reply.await_count for message in messages), 1)
        self.assertEqual(self.ns["bot"].edit_message_text.await_count, 2)
        queued_sessions = [
            (await self.ns["MEDIA_QUEUE"].get())[1]
            for _ in range(3)
        ]
        self.assertTrue(all(queued_session is session for queued_session in queued_sessions))

    async def test_single_media_shows_panel_before_background_processing(self):
        message = self.message(None)
        await self.upload(message)

        session = self.ns["UPLOAD_SESSIONS"][(123, 456)]
        message.reply.assert_awaited_once()
        panel_text = message.reply.await_args.args[0]
        self.assertIn("📥 已接收 1 个媒体", panel_text)
        self.assertIn("正在处理 1 个媒体", panel_text)
        self.assertEqual(
            message.reply.await_args.kwargs["reply_markup"],
            {"processing": True},
        )
        self.assertEqual(session["items"], [])

    async def test_processed_media_updates_existing_panel(self):
        message = self.message(None)
        await self.upload(message)
        session = self.ns["UPLOAD_SESSIONS"][(123, 456)]
        session["items"].append({"file_id": "1", "file_type": "video"})

        await self.ns["_update_upload_panel"](message, session)

        update_text = self.ns["bot"].edit_message_text.await_args.kwargs["text"]
        self.assertIn("📥 已接收 1 个媒体", update_text)
        self.assertIn("继续发送媒体", update_text)
        self.assertNotIn("正在处理", update_text)
        self.assertEqual(
            self.ns["bot"].edit_message_text.await_args.kwargs["reply_markup"],
            {"processing": False},
        )

    async def test_thumbnail_processing_does_not_block_upload_done_button(self):
        message = self.message(None)
        await self.upload(message)
        queued_message, session = await self.ns["MEDIA_QUEUE"].get()
        thumbnail_started = asyncio.Event()
        release_thumbnail = asyncio.Event()

        async def prepare_thumbnail(_file_id):
            thumbnail_started.set()
            await release_thumbnail.wait()
            return "late-phash", b"late-thumb"

        self.ns["_prepare_thumbnail"].side_effect = prepare_thumbnail
        await self.ns["_process_queued_media"](queued_message, session)

        panel_update = self.ns["bot"].edit_message_text.await_args.kwargs
        self.assertEqual(panel_update["reply_markup"], {"processing": False})
        self.assertEqual(session["processed_count"], 1)
        await thumbnail_started.wait()
        item = session["items"][0]
        self.assertEqual(item["thumb_phash"], "")

        wait_task = asyncio.create_task(
            self.ns["_wait_for_pending_thumbnails"]([item])
        )
        await asyncio.sleep(0)
        self.assertFalse(wait_task.done())

        release_thumbnail.set()
        await wait_task
        self.assertEqual(item["thumb_phash"], "late-phash")
        self.ns["_enqueue_media_forward"].assert_called_once()
