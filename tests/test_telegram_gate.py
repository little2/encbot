import asyncio
import inspect
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import CopyMessages, ForwardMessages, SendMediaGroup, SendMessage
from aiogram.types import InputMediaPhoto

from utils.telegram_gate import (
    TelegramGate,
    TelegramRateLimitMiddleware,
    telegram_call,
)


def _retry_after(retry_after: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        SendMessage(chat_id=1, text="x"), "Flood control exceeded", retry_after
    )


def _network_error() -> TelegramNetworkError:
    return TelegramNetworkError(SendMessage(chat_id=1, text="x"), "boom")


def _run(coro):
    return asyncio.run(coro)


class TelegramGateTests(unittest.TestCase):
    def _fast_gate(self, **kwargs) -> TelegramGate:
        params = dict(
            max_concurrent=5,
            send_min_interval=0.0,
            group_send_min_interval=0.0,
            edit_delete_min_interval=0.0,
            global_min_interval=0.0,
            max_attempts=3,
        )
        params.update(kwargs)
        return TelegramGate(**params)

    def test_success_returns_value(self) -> None:
        gate = self._fast_gate()

        async def scenario():
            return await gate.call("ok", lambda: _async_value(42), chat_id=10)

        self.assertEqual(_run(scenario()), 42)

    def test_retry_after_sets_cooldown_then_retries(self) -> None:
        gate = self._fast_gate(max_attempts=3)
        calls: list[float] = []
        observed: dict[str, float] = {}

        async def operation():
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise _retry_after(1)
            return "ok"

        async def on_retry(current, attempts, delay, exc):
            # 写入冷却后、重试等待前，同 chat 应处于冷却状态
            observed["remaining"] = gate.cooldown_remaining(111)
            observed["delay"] = delay

        async def scenario():
            return await gate.call(
                "flaky", operation, chat_id=111, on_retry=on_retry
            )

        self.assertEqual(_run(scenario()), "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(observed["delay"], 2)  # max(1, retry_after) + 1
        self.assertGreater(observed["remaining"], 0)
        self.assertEqual(gate.cooldown_remaining(111), 0.0)

    def test_retry_after_sets_global_cooldown(self) -> None:
        gate = self._fast_gate(max_attempts=1)

        async def failing():
            raise _retry_after(30)

        async def ok():
            return "ok"

        async def scenario():
            # max_attempts=1：设置冷却后立即抛出，不做重试等待
            with self.assertRaises(TelegramRetryAfter):
                await gate.call("limited", failing, chat_id=1)
            self.assertGreater(gate.cooldown_remaining(1), 0)

            # 被限流的 chat 后续调用要等冷却结束
            blocked = gate.call("blocked", ok, chat_id=1)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(blocked, timeout=0.3)

            # 429 的作用域无法可靠判断；其它 chat 也必须服从 bot 全局冷却。
            blocked_other = gate.call("other chat", ok, chat_id=2)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(blocked_other, timeout=0.3)
            self.assertGreater(gate.cooldown_remaining(2), 0)

        _run(scenario())

    def test_retry_exhaustion_raises(self) -> None:
        gate = self._fast_gate(max_attempts=2)
        calls: list[int] = []

        async def always_limited():
            calls.append(1)
            raise _retry_after(1)

        async def scenario():
            with self.assertRaises(TelegramRetryAfter):
                await gate.call("always limited", always_limited, chat_id=3)

        _run(scenario())
        self.assertEqual(len(calls), 2)

    def test_retry_after_is_never_truncated(self) -> None:
        gate = self._fast_gate(max_attempts=1)

        async def scenario():
            with self.assertRaises(TelegramRetryAfter):
                await gate.call(
                    "long cooldown", lambda: _raise(_retry_after(300)), chat_id=8
                )

        _run(scenario())
        self.assertGreater(gate.cooldown_remaining(999), 300)

    def test_transport_middleware_paces_direct_group_sends(self) -> None:
        gate = TelegramGate(
            send_min_interval=0.0,
            group_send_min_interval=0.03,
            edit_delete_min_interval=0.0,
            global_min_interval=0.0,
        )
        middleware = TelegramRateLimitMiddleware(gate)
        make_request = AsyncMock(return_value="ok")
        method = SendMessage(chat_id=-100123, text="x")

        async def scenario():
            start = time.monotonic()
            await middleware(make_request, object(), method)
            await middleware(make_request, object(), method)
            return time.monotonic() - start

        self.assertGreaterEqual(_run(scenario()), 0.025)
        self.assertEqual(make_request.await_count, 2)

    def test_network_error_backoff_then_success(self) -> None:
        gate = self._fast_gate(max_attempts=3)
        calls: list[int] = []

        async def flaky_network():
            calls.append(1)
            if len(calls) == 1:
                raise _network_error()
            return "ok"

        async def scenario():
            return await gate.call(
                "net", flaky_network, chat_id=4, retry_network_errors=True
            )

        self.assertEqual(_run(scenario()), "ok")
        self.assertEqual(len(calls), 2)

    def test_network_failure_does_not_replay_send_by_default(self):
        gate = self._fast_gate()
        operation = AsyncMock(side_effect=_network_error())

        async def scenario():
            with self.assertRaises(TelegramNetworkError):
                await gate.call("uncertain send", operation, chat_id=1)

        _run(scenario())
        operation.assert_awaited_once()

    def test_queued_requests_keep_actual_dispatch_spacing(self):
        async def scenario():
            gate = self._fast_gate(max_concurrent=1, global_min_interval=0.02)
            entered, release = asyncio.Event(), asyncio.Event()
            sent = []

            async def first():
                entered.set()
                await release.wait()

            async def queued():
                sent.append(time.monotonic())

            first_task = asyncio.create_task(gate.call("first", first, chat_id=1))
            await entered.wait()
            pending = [asyncio.create_task(gate.call("queued", queued, chat_id=i))
                       for i in range(2, 6)]
            # Longer than all rate reservations in the old implementation.
            await asyncio.sleep(0.12)
            release.set()
            await asyncio.gather(first_task, *pending)
            for before, after in zip(sent, sent[1:]):
                self.assertGreaterEqual(after - before, 0.018)

        _run(scenario())

    def test_queued_request_obeys_new_429_cooldown(self):
        async def scenario():
            gate = self._fast_gate(max_concurrent=1, max_attempts=1)
            entered, release = asyncio.Event(), asyncio.Event()
            operation = AsyncMock()

            async def first():
                entered.set()
                await release.wait()
                raise _retry_after(300)

            first_task = asyncio.create_task(gate.call("first", first, chat_id=1))
            await entered.wait()
            pending = asyncio.create_task(gate.call("queued", operation, chat_id=2))
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(TelegramRetryAfter):
                await first_task
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(pending, timeout=0.03)
            operation.assert_not_awaited()
            self.assertGreater(gate.cooldown_remaining(2), 300)
            # Cancellation while cooling must release concurrency capacity.
            self.assertFalse(gate._semaphore.locked())

        _run(scenario())

    def test_username_and_unknown_destinations_are_paced(self):
        async def scenario():
            gate = self._fast_gate(
                send_min_interval=0.02, group_send_min_interval=0.03
            )
            for first, second, minimum in (
                ("@Example", "@example", 0.025), (None, None, 0.018)
            ):
                start = time.monotonic()
                await gate.call("first", lambda: _async_value(None), chat_id=first)
                await gate.call("second", lambda: _async_value(None), chat_id=second)
                self.assertGreaterEqual(time.monotonic() - start, minimum)

        _run(scenario())

    def test_clients_using_same_token_share_cooldown(self):
        async def scenario():
            first, second = TelegramRateLimitMiddleware(), TelegramRateLimitMiddleware()
            bot1 = SimpleNamespace(token="123:test-only-token")
            bot2 = SimpleNamespace(token="123:test-only-token")
            method = SendMessage(chat_id=1, text="x")
            failing = AsyncMock(side_effect=_retry_after(300))
            with self.assertRaises(TelegramRetryAfter):
                await first(failing, bot1, method)
            next_request = AsyncMock()
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(second(next_request, bot2, method), timeout=0.03)
            next_request.assert_not_awaited()
            # A different token has independent quotas and cooldowns.
            await second(next_request, SimpleNamespace(token="456:other-token"), method)
            next_request.assert_awaited_once()

        _run(scenario())

    def test_batch_methods_charge_each_message(self):
        async def scenario():
            gate = self._fast_gate()
            gate.call = AsyncMock(return_value="ok")
            middleware = TelegramRateLimitMiddleware(gate)
            methods = [
                CopyMessages(chat_id=1, from_chat_id=2, message_ids=[1, 2, 3]),
                ForwardMessages(chat_id=1, from_chat_id=2, message_ids=[1, 2, 3]),
                SendMediaGroup(chat_id=1, media=[InputMediaPhoto(media=str(i))
                                               for i in range(3)]),
            ]
            for method in methods:
                await middleware(AsyncMock(), object(), method)
                self.assertEqual(gate.call.await_args.kwargs["global_weight"], 3)

        _run(scenario())

    def test_min_interval_and_kind_tiers(self) -> None:
        gate = TelegramGate(
            max_concurrent=5,
            send_min_interval=0.0,
            group_send_min_interval=0.0,
            edit_delete_min_interval=0.4,
            global_min_interval=0.0,
            max_attempts=2,
        )

        async def ok():
            return "ok"

        async def scenario():
            start = time.monotonic()
            await gate.call("send 1", ok, chat_id=5, kind="send")
            await gate.call("send 2", ok, chat_id=5, kind="send")
            send_elapsed = time.monotonic() - start

            start = time.monotonic()
            await gate.call("delete 1", ok, chat_id=5, kind="delete")
            await gate.call("delete 2", ok, chat_id=5, kind="delete")
            delete_gap = time.monotonic() - start
            return send_elapsed, delete_gap

        send_elapsed, delete_gap = _run(scenario())
        self.assertLess(send_elapsed, 0.3)
        self.assertGreaterEqual(delete_gap, 0.4)

    def test_telegram_call_accepts_legacy_signature(self) -> None:
        sig = inspect.signature(telegram_call)
        sig.bind("label", lambda: None, 4, None)  # 旧版位置参数
        sig.bind("label", lambda: None)  # 旧版最常见用法
        sig.bind("label", lambda: None, max_attempts=2, on_retry=None)
        sig.bind("label", lambda: None, chat_id=1, kind="delete")  # 新增参数

        async def ok():
            return "ok"

        self.assertEqual(
            _run(telegram_call("legacy call", ok, chat_id=987654321)), "ok"
        )


async def _async_value(value):
    return value


async def _raise(exc):
    raise exc
