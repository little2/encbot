"""Telegram 出站调用闸门：全局并发上限 + 按 chat 限速 + 429 冷却。

背景：aiogram 收到 429（TelegramRetryAfter）不会自动重试，需要调用方 sleep 后重来。
旧实现用一把全局锁包住「重试 + sleep」，一次被限流的调用会阻塞所有其它请求；
而 Telegram 的限流实际是「按 chat + 全局」维度的。

本模块策略：
- 全局 asyncio.Semaphore 限制并发；
- 同一 chat 的连续调用保持最小间隔（发送 / 编辑删除两档）；
- 收到 TelegramRetryAfter 只给当前 chat 写入冷却时间，冷却期内该 chat 的后续
  调用自动等待，其它 chat 不受影响；
- TelegramNetworkError 指数退避重试。

仅依赖标准库与 aiogram 异常类型。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

Operation = Callable[[], Awaitable[Any]]

DEFAULT_MAX_CONCURRENT = 20
DEFAULT_SEND_MIN_INTERVAL = 0.3
DEFAULT_EDIT_DELETE_MIN_INTERVAL = 0.7
DEFAULT_MAX_ATTEMPTS = 4
MAX_COOLDOWN_SECONDS = 120.0
STATE_PRUNE_THRESHOLD = 64


class TelegramGate:
    """按 chat 维度限速、冷却的 Telegram 调用闸门。"""

    def __init__(
        self,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        send_min_interval: float = DEFAULT_SEND_MIN_INTERVAL,
        edit_delete_min_interval: float = DEFAULT_EDIT_DELETE_MIN_INTERVAL,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._send_min_interval = send_min_interval
        self._edit_delete_min_interval = edit_delete_min_interval
        self._max_attempts = max_attempts
        # key = chat_id；chat 维度未知时为 None
        self._next_allowed: dict[Any, float] = {}
        self._cooldown_until: dict[Any, float] = {}

    @staticmethod
    def _key(chat_id: Any) -> Any:
        if chat_id is None:
            return None
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            return chat_id

    def _min_interval(self, kind: str) -> float:
        if kind in {"edit", "delete"}:
            return self._edit_delete_min_interval
        return self._send_min_interval

    def cooldown_remaining(self, chat_id: Any = None) -> float:
        """返回该 chat 的剩余冷却秒数（测试与诊断用）。"""
        deadline = self._cooldown_until.get(self._key(chat_id), 0.0)
        return max(0.0, deadline - time.monotonic())

    def _set_cooldown(self, key: Any, delay: float) -> None:
        deadline = time.monotonic() + min(float(delay), MAX_COOLDOWN_SECONDS)
        self._cooldown_until[key] = deadline
        self._maybe_prune()

    def _maybe_prune(self) -> None:
        if len(self._cooldown_until) <= STATE_PRUNE_THRESHOLD:
            return
        now = time.monotonic()
        for store in (self._cooldown_until, self._next_allowed):
            for key, deadline in list(store.items()):
                if deadline <= now:
                    store.pop(key, None)

    async def _wait_turn(self, key: Any, min_interval: float) -> None:
        # 检查与占位之间没有 await，asyncio 单线程下天然原子，无需额外加锁
        while True:
            now = time.monotonic()
            cooldown = self._cooldown_until.get(key, 0.0)
            if cooldown > now:
                await asyncio.sleep(cooldown - now)
                continue
            next_allowed = self._next_allowed.get(key, 0.0)
            if next_allowed > now:
                await asyncio.sleep(next_allowed - now)
                continue
            self._next_allowed[key] = now + min_interval
            return

    async def call(
        self,
        label: str,
        operation: Operation,
        *,
        chat_id: Any = None,
        kind: str = "send",
        max_attempts: Optional[int] = None,
        on_retry: Optional[Callable[..., Any]] = None,
    ) -> Any:
        attempts = (
            max_attempts
            if max_attempts is not None and max_attempts > 0
            else self._max_attempts
        )
        key = self._key(chat_id)
        for attempt in range(attempts):
            await self._wait_turn(key, self._min_interval(kind))
            try:
                async with self._semaphore:
                    return await operation()
            except TelegramRetryAfter as exc:
                delay = max(1, int(exc.retry_after)) + 1
                # 只让当前 chat 冷却；等待统一交给下一轮 _wait_turn
                self._set_cooldown(key, delay)
                print(
                    f"[TELEGRAM_RATE_LIMIT] {label}: "
                    f"cooldown {delay}s ({attempt + 1}/{attempts})",
                    flush=True,
                )
                await self._notify_retry(
                    on_retry, label, attempt + 1, attempts, delay, exc
                )
                if attempt + 1 >= attempts:
                    raise
            except TelegramNetworkError as exc:
                delay = min(2 ** (attempt + 1), 10)
                print(
                    f"[TELEGRAM_NETWORK] {label}: {exc}; "
                    f"retry in {delay}s ({attempt + 1}/{attempts})",
                    flush=True,
                )
                await self._notify_retry(
                    on_retry, label, attempt + 1, attempts, delay, exc
                )
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(delay)
        raise RuntimeError(f"[TELEGRAM_GATE] {label}: no attempts remaining")

    @staticmethod
    async def _notify_retry(on_retry, label, current, attempts, delay, exc) -> None:
        if on_retry is None:
            return
        try:
            await on_retry(current, attempts, delay, exc)
        except Exception as callback_exc:
            print(f"[TELEGRAM_RETRY_STATUS] {label}: {callback_exc}", flush=True)


_default_gate = TelegramGate()


async def telegram_call(
    label: str,
    operation: Operation,
    max_attempts: int = 4,
    on_retry: Optional[Callable[..., Any]] = None,
    *,
    chat_id: Any = None,
    kind: str = "send",
) -> Any:
    """兼容旧 `_telegram_call_with_retry(label, operation, ...)` 的模块级入口。"""
    return await _default_gate.call(
        label,
        operation,
        chat_id=chat_id,
        kind=kind,
        max_attempts=max_attempts,
        on_retry=on_retry,
    )
