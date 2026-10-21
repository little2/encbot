"""Telegram 出站调用闸门：全局并发上限 + 按 chat 限速 + 429 冷却。

背景：aiogram 收到 429（TelegramRetryAfter）不会自动重试，需要调用方 sleep 后重来。
旧实现用一把全局锁包住「重试 + sleep」，一次被限流的调用会阻塞所有其它请求；
而 Telegram 的限流实际是「按 chat + 全局」维度的。

本模块策略：
- 全局 asyncio.Semaphore 限制并发，并限制 Bot Token 的总体请求速率；
- 同一 chat 的连续调用保持最小间隔，群组发送使用更严格的每分钟额度；
- 收到 TelegramRetryAfter 后完整遵守等待时间，并保守应用 Bot 全局冷却；
- 网络错误默认向上抛出，只有明确可安全重复的操作才启用退避重试。

仅依赖标准库与 aiogram 异常类型。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Awaitable, Callable, Optional
from weakref import WeakKeyDictionary

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

Operation = Callable[[], Awaitable[Any]]

DEFAULT_MAX_CONCURRENT = 20
# Telegram recommends no more than one message per second in one chat, no
# more than 20 messages per minute in a group, and roughly 30 broadcasts per
# second.  Keep a little headroom below the global free-broadcast ceiling.
DEFAULT_SEND_MIN_INTERVAL = 1.0
DEFAULT_GROUP_SEND_MIN_INTERVAL = 3.1
DEFAULT_EDIT_DELETE_MIN_INTERVAL = 0.7
DEFAULT_GLOBAL_MIN_INTERVAL = 1.0 / 29.0
DEFAULT_MAX_ATTEMPTS = 4
STATE_PRUNE_THRESHOLD = 64


class TelegramGate:
    """按 chat 维度限速、冷却的 Telegram 调用闸门。"""

    def __init__(
        self,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        send_min_interval: float = DEFAULT_SEND_MIN_INTERVAL,
        group_send_min_interval: float = DEFAULT_GROUP_SEND_MIN_INTERVAL,
        edit_delete_min_interval: float = DEFAULT_EDIT_DELETE_MIN_INTERVAL,
        global_min_interval: float = DEFAULT_GLOBAL_MIN_INTERVAL,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if max_concurrent < 1 or max_attempts < 1:
            raise ValueError("max_concurrent and max_attempts must be positive")
        if min(send_min_interval, group_send_min_interval,
               edit_delete_min_interval, global_min_interval) < 0:
            raise ValueError("rate limit intervals must not be negative")
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._send_min_interval = send_min_interval
        self._group_send_min_interval = group_send_min_interval
        self._edit_delete_min_interval = edit_delete_min_interval
        self._global_min_interval = global_min_interval
        self._max_attempts = max_attempts
        # key = chat_id；chat 维度未知时为 None
        self._next_allowed: dict[Any, float] = {}
        self._cooldown_until: dict[Any, float] = {}
        self._global_next_allowed = 0.0
        self._global_cooldown_until = 0.0

    @staticmethod
    def _key(chat_id: Any) -> Any:
        if chat_id is None:
            return None
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            return chat_id.lower() if isinstance(chat_id, str) else chat_id

    def _min_interval(self, kind: str, key: Any) -> float:
        if kind in {"edit", "delete"}:
            return self._edit_delete_min_interval
        if kind != "send":
            return 0.0
        # Telegram numeric group, supergroup and channel IDs are negative.
        if (isinstance(key, int) and key < 0) or (
            isinstance(key, str) and key.startswith("@")
        ):
            return self._group_send_min_interval
        return self._send_min_interval

    def cooldown_remaining(self, chat_id: Any = None) -> float:
        """返回该 chat 的剩余冷却秒数（测试与诊断用）。"""
        deadline = max(
            self._global_cooldown_until,
            self._cooldown_until.get(self._key(chat_id), 0.0),
        )
        return max(0.0, deadline - time.monotonic())

    def _set_cooldown(self, key: Any, delay: float) -> None:
        # retry_after is the number of seconds Telegram requires us to wait.
        # Never truncate it.  Treat an unclassified 429 conservatively as both
        # bot-global and chat-local so traffic to other chats cannot aggravate
        # a global broadcast flood limit.
        deadline = time.monotonic() + float(delay)
        self._global_cooldown_until = max(self._global_cooldown_until, deadline)
        if key is not None:
            self._cooldown_until[key] = max(
                self._cooldown_until.get(key, 0.0), deadline
            )
        self._maybe_prune()

    def _maybe_prune(self) -> None:
        if max(len(self._cooldown_until), len(self._next_allowed)) <= STATE_PRUNE_THRESHOLD:
            return
        now = time.monotonic()
        for store in (self._cooldown_until, self._next_allowed):
            for key, deadline in list(store.items()):
                if deadline <= now:
                    store.pop(key, None)

    async def _wait_turn(
        self, key: Any, min_interval: float, global_weight: int = 1
    ) -> None:
        # 检查与占位之间没有 await，asyncio 单线程下天然原子，无需额外加锁
        while True:
            now = time.monotonic()
            cooldown = max(
                self._global_cooldown_until,
                self._cooldown_until.get(key, 0.0),
            )
            if cooldown > now:
                await asyncio.sleep(cooldown - now)
                continue
            next_allowed = max(
                self._global_next_allowed,
                self._next_allowed.get(key, 0.0),
            )
            if next_allowed > now:
                await asyncio.sleep(next_allowed - now)
                continue
            self._global_next_allowed = (
                now + self._global_min_interval * max(1, int(global_weight))
            )
            # Unknown send destinations share a conservative bucket as well.
            self._next_allowed[key] = (
                now + min_interval * max(1, int(global_weight))
            )
            self._maybe_prune()
            return

    async def call(
        self,
        label: str,
        operation: Operation,
        *,
        chat_id: Any = None,
        kind: str = "send",
        global_weight: int = 1,
        max_attempts: Optional[int] = None,
        on_retry: Optional[Callable[..., Any]] = None,
        retry_network_errors: bool = False,
    ) -> Any:
        attempts = (
            max_attempts
            if max_attempts is not None and max_attempts > 0
            else self._max_attempts
        )
        key = self._key(chat_id)
        for attempt in range(attempts):
            try:
                async with self._semaphore:
                    # Reserve the rate slot only after acquiring capacity.
                    # No await may intervene between this check and dispatch.
                    await self._wait_turn(
                        key, self._min_interval(kind, key),
                        global_weight=global_weight,
                    )
                    try:
                        return await operation()
                    except TelegramRetryAfter as exc:
                        # Publish cooldown before releasing capacity to queued
                        # calls. Already in-flight HTTP requests cannot be recalled.
                        self._set_cooldown(key, max(1, int(exc.retry_after)) + 1)
                        raise
            except TelegramRetryAfter as exc:
                delay = max(1, int(exc.retry_after)) + 1
                # 等待统一交给下一轮 _wait_turn。
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
                # A timeout does not prove that a message was not delivered.
                if not retry_network_errors:
                    raise
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

# Shared by clients using the same token and event loop, including independent
# Bot/session objects. This is in-process only, not a distributed limiter.
_token_gates = WeakKeyDictionary()


def _get_token_gate(bot) -> TelegramGate:
    loop = asyncio.get_running_loop()
    gates = _token_gates.setdefault(loop, {})
    token_key = hashlib.sha256(bot.token.encode("utf-8")).digest()
    if token_key not in gates:
        gates[token_key] = TelegramGate()
    return gates[token_key]


class TelegramRateLimitMiddleware:
    """Apply one transport-level gate to every request made by a Bot token."""

    def __init__(self, gate: Optional[TelegramGate] = None) -> None:
        self.gate = gate

    async def __call__(self, make_request, bot, method):
        method_name = type(method).__name__
        lowered = method_name.lower()
        chat_id = getattr(method, "chat_id", None)
        if lowered.startswith("delete"):
            kind = "delete"
        elif lowered.startswith(("edit", "pin", "unpin")):
            kind = "edit"
        elif lowered.startswith(("send", "copy", "forward")):
            kind = "send"
        else:
            kind = "other"

        media = getattr(method, "media", None)
        message_ids = getattr(method, "message_ids", None)
        if lowered == "sendmediagroup" and media:
            weight = len(media)
        elif lowered in {"copymessages", "forwardmessages"} and message_ids:
            weight = len(message_ids)
        else:
            weight = 1
        gate = self.gate if self.gate is not None else _get_token_gate(bot)
        return await gate.call(
            method_name,
            lambda: make_request(bot, method),
            chat_id=chat_id,
            kind=kind,
            global_weight=weight,
            max_attempts=1,
        )


async def telegram_call(
    label: str,
    operation: Operation,
    max_attempts: int = 4,
    on_retry: Optional[Callable[..., Any]] = None,
    *,
    chat_id: Any = None,
    kind: str = "send",
    global_weight: int = 1,
    retry_network_errors: bool = False,
) -> Any:
    """兼容旧 `_telegram_call_with_retry(label, operation, ...)` 的模块级入口。"""
    return await _default_gate.call(
        label,
        operation,
        chat_id=chat_id,
        kind=kind,
        global_weight=global_weight,
        max_attempts=max_attempts,
        on_retry=on_retry,
        retry_network_errors=retry_network_errors,
    )
