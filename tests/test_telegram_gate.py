import asyncio
import inspect
import time
import unittest
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

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
            return await gate.call("net", flaky_network, chat_id=4)

        self.assertEqual(_run(scenario()), "ok")
        self.assertEqual(len(calls), 2)

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
