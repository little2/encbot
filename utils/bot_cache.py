"""Bounded bot sessions with reference-counted leases."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic

from aiogram import Bot


@dataclass
class _Entry:
    bot: Bot
    last_used: float
    users: int = 0


class BotCache:
    def __init__(self, max_size=5, idle_seconds=1800, factory=Bot):
        if max_size < 1 or idle_seconds <= 0:
            raise ValueError("Cache size and idle timeout must be positive")
        self.max_size = max_size
        self.idle_seconds = idle_seconds
        self.factory = factory
        self.entries = {}
        self.condition = asyncio.Condition()
        self.closed = False

    async def _remove(self, token):
        entry = self.entries[token]
        await entry.bot.session.close()
        del self.entries[token]

    @asynccontextmanager
    async def use(self, token):
        async with self.condition:
            while True:
                if self.closed:
                    raise RuntimeError("Bot cache is closed")
                for key, entry in list(self.entries.items()):
                    if not entry.users and monotonic() - entry.last_used >= self.idle_seconds:
                        await self._remove(key)
                if token in self.entries:
                    entry = self.entries[token]
                    break
                if len(self.entries) >= self.max_size:
                    idle = [key for key, value in self.entries.items() if not value.users]
                    if not idle:
                        await self.condition.wait()
                        continue
                    await self._remove(min(idle, key=lambda key: self.entries[key].last_used))
                entry = _Entry(self.factory(token=token), monotonic())
                self.entries[token] = entry
                break
            entry.users += 1
        try:
            yield entry.bot
        finally:
            async with self.condition:
                entry.users -= 1
                entry.last_used = monotonic()
                self.condition.notify_all()

    async def cleanup_worker(self):
        while True:
            await asyncio.sleep(min(60, self.idle_seconds))
            async with self.condition:
                if self.closed:
                    return
                for token, entry in list(self.entries.items()):
                    if not entry.users and monotonic() - entry.last_used >= self.idle_seconds:
                        await self._remove(token)
                self.condition.notify_all()

    async def close(self):
        async with self.condition:
            self.closed = True
            self.condition.notify_all()
            await self.condition.wait_for(lambda: all(not e.users for e in self.entries.values()))
            results = await asyncio.gather(
                *(e.bot.session.close() for e in self.entries.values()), return_exceptions=True
            )
            self.entries.clear()
            for result in results:
                if isinstance(result, BaseException):
                    raise result
