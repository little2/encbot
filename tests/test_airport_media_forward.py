import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable
import unittest
from unittest.mock import AsyncMock, call

from tgfileid import TelegramFileId


SOURCE = Path(__file__).resolve().parents[1] / "main_airport.py"


def load_function(name, namespace):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    function.decorator_list = []
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"),
        namespace,
    )
    return namespace[name]


class AirportMediaForwardTests(unittest.TestCase):
    def test_final_group_failure_does_not_return_empty_success(self):
        send = AsyncMock(side_effect=ConnectionError("offline"))
        ns = dict(Any=object, Message=object, Bot=object, _send_media_with_bot=send,
                  TelegramBadRequest=ValueError, _album_kind=lambda _: "document",
                  _normalize_receiver_chat_id=lambda *args, **kwargs: 123)
        send_all = load_function("_send_all_media_with_bot", ns)
        with self.assertRaisesRegex(ConnectionError, "offline"):
            asyncio.run(send_all(SimpleNamespace(from_user=None), {"file_type": "document", "file_id": "file"},
                                 123, object()))

    def test_exhausts_copies_before_exactly_one_refill(self):
        async def run(results, expected_uploads, expected_positions):
            userbot = SimpleNamespace(user_id=55)
            job = {"file_unique_id": "unique"}
            store = SimpleNamespace(
                get_backup_locations=AsyncMock(return_value=[(101, 1), (101, 2)]),
                reserve_backup=AsyncMock(return_value=job),
            )
            forward = AsyncMock(side_effect=results)
            upload = AsyncMock(return_value=(101, 3))
            ns = dict(asyncio=asyncio, Callable=Callable, Awaitable=Awaitable, Bot=object,
                      bot=SimpleNamespace(id=101), active_userbot=userbot, received_media_store=store,
                      _upload_userbot_backup=upload, _forward_userbot_media=forward)
            deliver = AsyncMock(side_effect=lambda file_id: file_id == "good")
            request = load_function("_request_backup_man", ns)
            self.assertEqual(await request(dict(file_id="old", file_unique_id="unique"),
                                           object(), accept_file_id=deliver), "good")
            self.assertEqual(upload.await_count, expected_uploads)
            self.assertEqual(store.reserve_backup.await_count, expected_uploads)
            self.assertEqual([args.args[-1] for args in forward.await_args_list], expected_positions)
            self.assertEqual(deliver.await_args_list[-1], call("good"))

        for failure in [FileNotFoundError(), RuntimeError("forward failed"), TimeoutError(), None, "unusable"]:
            with self.subTest(failure=failure):
                asyncio.run(run([failure, "good"], 0, [1, 2]))
                asyncio.run(run([failure, failure, "good"], 1, [1, 2, 3]))

    def test_explicit_rejection_uses_x_once_uncertain_sends_reconcile_first(self):
        class Rejected(Exception):
            pass

        async def run(first_send, receipt_mode, expected_x, restarted=False):
            sent = SimpleNamespace(uid="unique")
            job = dict(file_unique_id="unique", file_type="document", file_id="old", userbot_id=55,
                       receipt_tag="nonce", first_attempt=10 if restarted else 0, since=10)
            receipt = asyncio.get_running_loop().create_future()
            route = (101, 222)

            @asynccontextmanager
            async def observe(*args):
                if receipt_mode == "live":
                    receipt.set_result(route)
                yield receipt

            history = AsyncMock(return_value=route if receipt_mode == "history" else None)
            userbot = SimpleNamespace(observe_bot_upload=observe, find_bot_upload=history)
            store = SimpleNamespace(complete_backup=AsyncMock(), release_rejected_backup=AsyncMock(), save_backup_file=AsyncMock(),
                                    get_backup_receipt=AsyncMock(return_value=route if receipt_mode == "db" else None))
            send = AsyncMock(side_effect=[first_send or sent, sent])
            request_x = AsyncMock(return_value="replacement")
            ns = dict(asyncio=asyncio, bot=SimpleNamespace(id=101), bot_name="tower",
                      _send_media_with_bot=send, _request_x_man=request_x, _extract_media_unique_id=lambda m: m.uid,
                      received_media_store=store, TelegramAPIError=Rejected, TelegramEntityTooLarge=ValueError, TelegramNetworkError=ConnectionError,
                      TelegramServerError=ConnectionError, BACKUP_MAN_REPLY_TIMEOUT_SECONDS=0.005)
            upload = load_function("_upload_userbot_backup", ns)
            result = await upload(userbot, job, AsyncMock())
            self.assertEqual(result, None if receipt_mode == "unknown" else route)
            self.assertEqual(send.await_count, 0 if restarted else 1 + expected_x)
            self.assertEqual(request_x.await_count, expected_x)
            if expected_x:
                request_x.assert_awaited_once_with("unique", ns["bot"])
            if receipt_mode in {"history", "unknown"}:
                history.assert_awaited_once_with(101, "tower", "nonce", None, 10)
            if result:
                store.complete_backup.assert_awaited_once_with(job, *route)
            else:
                store.complete_backup.assert_not_awaited()
                store.release_rejected_backup.assert_not_awaited()

        asyncio.run(run(Rejected("chat not found"), "live", 1))
        asyncio.run(run(TimeoutError(), "history", 0))
        asyncio.run(run(None, "db", 0))
        asyncio.run(run(None, "unknown", 0))
        asyncio.run(run(None, "history", 0, restarted=True))

    def test_wrong_x_man_attachment_is_ignored_by_b_and_d(self):
        async def run():
            request = SimpleNamespace(message_id=10, text="wanted", caption=None)
            def reply(uid):
                return SimpleNamespace(from_user=SimpleNamespace(id=88), reply_to_message=request,
                                       uid=uid, file_id="file-" + uid)
            wrong, right = reply("other"), reply("wanted")
            receipt = asyncio.get_running_loop().create_future()
            ns = dict(asyncio=asyncio, Bot=object, Message=object, X_MAN_PENDING_REPLY=receipt,
                      X_MAN_PENDING_FILE_UNIQUE_ID="wanted", X_MAN_PENDING_REQUEST_MESSAGE_ID=10,
                      X_MAN_REPLY_TIMEOUT_SECONDS=1, SharedConfig={"x_man_bot_id": 88},
                      _extract_media_info=lambda m: ("video", m.file_id), _extract_media_unique_id=lambda m: m.uid)
            load_function("_get_x_man_reply_file_id", ns)
            handle = load_function("on_x_man_reply", ns)
            await handle(wrong)
            self.assertFalse(receipt.done())
            await handle(right)
            self.assertEqual(receipt.result(), "file-wanted")
            poll = load_function("_wait_for_x_man_reply_with_bot", ns)
            updates = [SimpleNamespace(update_id=i, message=m) for i, m in enumerate([wrong, right], 1)]
            self.assertEqual(await poll(SimpleNamespace(get_updates=AsyncMock(return_value=updates)), 10, "wanted"),
                             "file-wanted")
        asyncio.run(run())

    def test_confirmation_requires_a_logged_in_userbot_only_when_enabled(self):
        for setting, userbot, should_block in (
            ("true", None, True),
            ("true", SimpleNamespace(user_id=None), True),
            ("true", SimpleNamespace(user_id=123), False),
            ("false", None, False),
        ):
            with self.subTest(setting=setting, userbot=userbot):
                # Stop at the existing duplicate check when confirmation is allowed.
                store = SimpleNamespace(claim_batch=AsyncMock(return_value=["duplicate"]))
                handle = load_function("_handle_send_encoded", {
                    "CallbackQuery": object, "Any": object,
                    "os": SimpleNamespace(getenv=lambda *_: setting),
                    "active_userbot": userbot, "received_media_store": store, "bot": SimpleNamespace(id=101),
                    "_wait_for_pending_thumbnails": AsyncMock(),
                    "_build_controls_keyboard": AsyncMock(),
                })
                callback = SimpleNamespace(answer=AsyncMock(), message=AsyncMock())
                state = {"owner_user_id": 7, "batch_id": "batch", "encoded": "code",
                         "items": [{"file_unique_id": "test"}], "send_status": "idle"}
                asyncio.run(handle(callback, (7, 1), state))
                if should_block:
                    store.claim_batch.assert_not_awaited()
                    self.assertTrue(state["send_confirm_pending"])
                    self.assertEqual(state["send_status"], "idle")
                    self.assertIn("尚未确认", callback.answer.await_args.args[0])
                else:
                    store.claim_batch.assert_awaited_once_with(
                        state["items"], 7, "batch", backup_route=(101, 123) if setting == "true" else None,
                    )

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
                get_by_file_id=AsyncMock(return_value=[]),
            )
            namespace = {
                "Message": object, "Bot": object, "Any": object,
                "Awaitable": Awaitable, "Callable": Callable,
                "TelegramBadRequest": BadRequest,
                "_send_media_with_bot": send_media,
                "_is_invalid_media_reference_error": lambda exc: True,
                "_normalize_receiver_chat_id": lambda value, **kwargs: value,
                "_album_kind": lambda kind: None,
                "bot": SimpleNamespace(send_message=AsyncMock()),
                "os": SimpleNamespace(getenv=lambda *args: "true" if userbot_enabled else "false"),
                "BACKUP_FORWARD_SEND_LOCK": lock,
                "TelegramFileId": SimpleNamespace(file_id_to_unique_id=lambda value: "unique-id"),
                "_request_x_man": request_media,
                "received_media_store": received_media_store,
                "active_userbot": None,
            }
            load_function("_request_backup_man", namespace)
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
            expected_skipped_item = {**items[0], "_item_index": 1}
            self.assertEqual(skipped, [] if recovered else [expected_skipped_item])
            self.assertEqual(request_media.await_count, 0 if userbot_enabled else 1)
            if userbot_enabled:
                received_media_store.get_by_file_id.assert_not_awaited()
            else:
                received_media_store.get_by_file_id.assert_awaited_once_with("bad-file")
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

    def test_userbot_delivers_from_storage_or_refills_from_b(self):
        class BadRequest(Exception):
            def __init__(self, message="wrong file identifier"):
                super().__init__(message)
                self.message = message

        async def run_case(
            storage_state, file_type, b_has_file, x_has_file=True,
            final_upload_fails=False, d_error_message="wrong file identifier",
        ):
            original_id, unique_id = {
                "sticker": ("CAADAgADKgAPBAQ", "AgADKgAH"),
                "photo": ("AgADAgADKgAHewAHAQADAgADeAADPQQ", "AQADKgAHfQ"),
                "document": ("original-file-id", "unique-id"),
            }[file_type]
            notice = SimpleNamespace(edit_text=AsyncMock())
            bot_b = SimpleNamespace(id=101, send_message=AsyncMock(return_value=notice))
            bot_d = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=77, username="runway_bot")))
            user_e = 123
            delivered = object()

            record = {"file_unique_id": unique_id}
            if storage_state in ("stored", "deleted"):
                record.update(storage_chat_id=101, storage_message_id=17)
            records = [] if storage_state == "no row" else [record]
            job = dict(file_unique_id=unique_id, file_type=file_type, file_id=original_id,
                       userbot_id=55, receipt_tag="tag", since=100, first_attempt=0)
            storage = SimpleNamespace(
                get_by_file_id=AsyncMock(return_value=records),
                get_backup_locations=AsyncMock(return_value=[(101, 17)] if "storage_chat_id" in record else []),
                reserve_backup=AsyncMock(return_value=job), complete_backup=AsyncMock(), save_backup_file=AsyncMock(),
                get_backup_receipt=AsyncMock(return_value=None), release_rejected_backup=AsyncMock(),
            )
            forward = AsyncMock(return_value=333)
            if storage_state == "deleted":
                forward.side_effect = [FileNotFoundError(), 333]
            receipt = asyncio.get_running_loop().create_future()
            observing = False

            @asynccontextmanager
            async def observe(bot_id, tag, sticker_uid):
                nonlocal observing
                self.assertEqual((bot_id, tag), (bot_b.id, "tag"))
                self.assertEqual(sticker_uid, unique_id if file_type == "sticker" else None)
                observing = True
                try:
                    yield receipt
                finally:
                    observing = False

            userbot = SimpleNamespace(user_id=55, forward_stored_message=forward, observe_bot_upload=observe,
                                      find_bot_upload=AsyncMock(return_value=None))
            request_x = AsyncMock(return_value="recovered-b-id" if x_has_file else None)
            wait_for_d = AsyncMock(return_value="runway-file-id")
            uploads = []
            captions = []
            received = SimpleNamespace(file_id=original_id, file_unique_id=unique_id)
            upload_result = SimpleNamespace(document=None, photo=[], video=None, audio=None, voice=None,
                                            animation=None, sticker=None)
            if file_type == "photo":
                upload_result.photo = [received]
            else:
                setattr(upload_result, file_type, received)

            async def send_media(bot, kind, file_id, chat_id, no_forward, if_spoiler, *, caption=None):
                self.assertEqual(kind, file_type)
                if bot is bot_b:
                    self.assertTrue(observing)
                    self.assertEqual(chat_id, userbot.user_id)
                    uploads.append(file_id)
                    captions.append(caption)
                    if file_id == original_id and not b_has_file:
                        raise BadRequest("wrong file identifier")
                    if final_upload_fails:
                        raise ConnectionError("B could not send the file to C")
                    receipt.set_result((101, 222))
                    return upload_result
                self.assertIs(bot, bot_d)
                self.assertEqual(chat_id, user_e)
                if file_id == original_id:
                    raise BadRequest(d_error_message)
                self.assertEqual(file_id, "runway-file-id")
                return delivered

            namespace = {
                "Message": object, "Bot": object, "Any": object, "asyncio": asyncio,
                "Awaitable": Awaitable, "Callable": Callable,
                "TelegramAPIError": BadRequest, "TelegramEntityTooLarge": ValueError, "TelegramNetworkError": ConnectionError,
                "TelegramServerError": OSError, "TelegramFileId": TelegramFileId,
                "TelegramBadRequest": BadRequest, "_send_media_with_bot": send_media,
                "_normalize_receiver_chat_id": lambda value, **kwargs: value,
                "_album_kind": lambda kind: None, "bot": bot_b, "bot_name": "main_bot",
                "os": SimpleNamespace(getenv=lambda *args: "true"),
                "BACKUP_FORWARD_SEND_LOCK": asyncio.Lock(), "BACKUP_MAN_REPLY_TIMEOUT_SECONDS": 0.01,
                "_request_x_man": request_x, "_wait_for_userbot_media_with_bot": wait_for_d,
                "received_media_store": storage, "active_userbot": userbot,
                "_is_invalid_media_reference_error": load_function(
                    "_is_invalid_media_reference_error",
                    {"TelegramBadRequest": BadRequest},
                ),
            }
            for name in ("_upload_userbot_backup", "_extract_media_unique_id",
                         "_forward_userbot_media", "_request_backup_man", "_send_all_media_with_bot"):
                load_function(name, namespace)
            sent, skipped = await namespace["_send_all_media_with_bot"](
                SimpleNamespace(from_user=None), {"file_type": file_type, "file_id": original_id},
                user_e, bot_d,
            )
            edits = [args.args[0] for args in notice.edit_text.await_args_list]
            self.assertTrue(any("正在停机坪寻觅班机" in text for text in edits))
            if storage_state == "stored":
                self.assertTrue(any("已找到备份" in text for text in edits))
            else:
                self.assertTrue(any("正在向塔台请求支援" in text for text in edits))
                if not b_has_file:
                    self.assertTrue(any("正在飞机工厂请求支援" in text for text in edits))
            self.assertIn("恢复处理结束", edits[-1])
            if b_has_file or storage_state == "stored":
                request_x.assert_not_awaited()
            else:
                request_x.assert_awaited_once_with(unique_id, bot_b)
            if storage_state == "stored":
                self.assertEqual(uploads, [])
                forward.assert_awaited_once_with(
                    101, 17, "@runway_bot", source_username="main_bot",
                )
            elif not x_has_file or final_upload_fails:
                expected_uploads = [original_id, "recovered-b-id"] if not b_has_file and x_has_file else [original_id]
                self.assertEqual(uploads, expected_uploads)
                self.assertEqual(sent, [])
                self.assertEqual(len(skipped), 1)
                storage.complete_backup.assert_not_awaited()
                self.assertEqual(userbot.find_bot_upload.await_count, int(final_upload_fails))
                wait_for_d.assert_not_awaited()
                return
            else:
                self.assertEqual(uploads, [original_id] if b_has_file else [original_id, "recovered-b-id"])
                self.assertEqual(captions[-1], "tag")
                storage.complete_backup.assert_awaited_once_with(job, 101, 222)
                expected = (
                    [call(101, 17, "@runway_bot", source_username="main_bot")]
                    if storage_state == "deleted" else []
                )
                self.assertEqual(
                    forward.await_args_list,
                    expected + [call(101, 222, "@runway_bot", source_username="main_bot")],
                )
            wait_for_d.assert_awaited_once_with(bot_d, unique_id, 55)
            self.assertEqual((sent, skipped), ([delivered], []))

        for case in (
            ("stored", "document", True), ("no row", "photo", True),
            ("deleted", "document", True), ("no row", "sticker", True),
            ("missing location", "document", False), ("deleted", "document", False),
            ("no row", "sticker", False), ("no row", "photo", False),
            ("missing location", "document", False, False),
            ("deleted", "document", True, True, True), ("no row", "sticker", False, True, True),
            ("stored", "document", True, True, False, "MEDIA_FILE_INVALID"),
        ):
            with self.subTest(case=case):
                asyncio.run(run_case(*case))

    def test_recovery_notice_tracks_multiple_items_and_survives_edit_errors(self):
        class BadRequest(Exception):
            message = "wrong file identifier"

        async def run_case(edit_fails, interrupt=False):
            notice = SimpleNamespace(
                edit_text=AsyncMock(side_effect=RuntimeError("edit failed") if edit_fails else None),
            )
            main_bot = SimpleNamespace(send_message=AsyncMock(return_value=notice))
            sending_bot = object()
            delivered = object()

            async def send_media(_bot, _kind, file_id, *_args, **_kwargs):
                if file_id == "bad-2" and interrupt:
                    raise RuntimeError("delivery interrupted")
                if file_id in {"bad-1", "bad-2"}:
                    raise BadRequest()
                self.assertEqual(file_id, "recovered-1")
                return delivered

            async def request(unique_id, active_bot):
                self.assertIs(active_bot, sending_bot)
                if not edit_fails:
                    text = notice.edit_text.await_args.args[0]
                    self.assertIn("正在向 X-MAN 索取媒体", text)
                return "recovered-1" if unique_id == "unique-bad-1" else None

            namespace = {
                "Message": object, "Bot": object, "Any": object,
                "TelegramBadRequest": BadRequest,
                "_send_media_with_bot": send_media,
                "_is_invalid_media_reference_error": lambda exc: True,
                "_normalize_receiver_chat_id": lambda value, **kwargs: value,
                "_album_kind": lambda kind: None,
                "bot": main_bot,
                "os": SimpleNamespace(getenv=lambda *args: "false"),
                "BACKUP_FORWARD_SEND_LOCK": asyncio.Lock(),
                "TelegramFileId": SimpleNamespace(
                    file_id_to_unique_id=lambda value: f"unique-{value}",
                ),
                "_request_x_man": request,
                "received_media_store": SimpleNamespace(get_by_file_id=AsyncMock(return_value=[])),
            }
            send_all = load_function("_send_all_media_with_bot", namespace)
            items = [
                {"file_type": "sticker", "file_id": "bad-1"},
                {"file_type": "sticker", "file_id": "bad-2"},
            ]
            if interrupt:
                sent, skipped = await send_all(
                    SimpleNamespace(from_user=None), {"items": items}, 123, sending_bot,
                )
                self.assertEqual(sent, [delivered])
                self.assertEqual(skipped, [{**items[1], "_item_index": 2}])
            else:
                sent, skipped = await send_all(
                    SimpleNamespace(from_user=None), {"items": items}, 123, sending_bot,
                )
                self.assertEqual(sent, [delivered])
                self.assertEqual(skipped, [{**items[1], "_item_index": 2}])
            main_bot.send_message.assert_awaited_once()
            edits = [args.args[0] for args in notice.edit_text.await_args_list]
            self.assertTrue(any("第 1/2 个资源" in text for text in edits))
            if interrupt:
                self.assertIn("发送中断，恢复处理未完成", edits[-1])
            else:
                self.assertTrue(any("第 2/2 个资源" in text for text in edits))
                self.assertIn("恢复处理结束", edits[-1])
                self.assertIn("已恢复并送出：1；恢复失败：1", edits[-1])
            self.assertNotIn("请稍候", edits[-1])

        for edit_fails, interrupt in ((False, False), (True, False), (False, True)):
            with self.subTest(edit_fails=edit_fails, interrupt=interrupt):
                asyncio.run(run_case(edit_fails, interrupt))

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
                "_get_x_man_reply_file_id": lambda incoming, expected_uid: extracted_file_id,
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
