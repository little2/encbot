import asyncio
import inspect
import time
import unittest

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from utils.telegram_gate import TelegramGate, telegram_call


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
            edit_delete_min_interval=0.0,
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

    def test_cooldown_blocks_same_chat_but_not_others(self) -> None:
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

            # 其它 chat 不受影响
            started = time.monotonic()
            result = await asyncio.wait_for(
                gate.call("other chat", ok, chat_id=2), timeout=1.0
            )
            elapsed = time.monotonic() - started
            self.assertEqual(result, "ok")
            self.assertLess(elapsed, 0.3)
            self.assertEqual(gate.cooldown_remaining(2), 0.0)

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
            edit_delete_min_interval=0.4,
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
