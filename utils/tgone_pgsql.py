# tgone_pgsql.py
import asyncpg
import asyncio
from functools import wraps
from inspect import stack
from typing import Optional, Any, List


def _caller_info():
    frames = stack()
    if len(frames) > 2:
        frame = frames[2]
        return f"{frame.filename.split('/')[-1]}:{frame.function}:{frame.lineno}"
    return "unknown"


def reconnecting(func):
    """
    仅当调用者明确指定 retry_safe=True 时，对连接错误重试一次。
    调用者必须保证 SQL 可安全重复执行（只读或已实现业务幂等）。
    不重建整个连接池：失效连接由 asyncpg 池替换。
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        attempts = 2 if kwargs.get("retry_safe", False) else 1
        for attempt in range(1, attempts + 1):
            try:
                return await func(*args, **kwargs)
            except (
                asyncpg.exceptions.ConnectionDoesNotExistError,
                asyncpg.exceptions.PostgresConnectionError,
                asyncpg.exceptions.CannotConnectNowError,
                OSError,
            ) as e:
                if attempt == attempts:
                    print(f"❌ [PGPool] connection error: {type(e).__name__}: {e}", flush=True)
                    raise
                print(f"⚠️ [PGPool] 连接异常 → 重新获取连接并重试一次: {type(e).__name__}: {e}", flush=True)
    return wrapper


class PGPool:
    """
    PostgreSQL asyncpg 连接池工具：
    - init_pool/ensure_pool/close/_rebuild_pool
    - execute/fetchrow/fetch/fetchval 等统一入口
    """
    _pool: Optional[asyncpg.Pool] = None
    _lock = asyncio.Lock()

    # 你可以用 env 或 config.py 注入
    DSN: str = ""

    # pool params
    MIN_SIZE: int = 2
    MAX_SIZE: int = 20
    COMMAND_TIMEOUT: float = 30.0
    CLOSE_TIMEOUT: float = 10.0

    @classmethod
    async def _create_pool_locked(cls):
        """调用者必须持有 _lock；此方法不重复获取锁。"""
        if not cls.DSN:
            raise RuntimeError("PGPool.DSN is empty. Please pass dsn to init_pool() or set PGPool.DSN")
        cls._pool = await asyncpg.create_pool(
            dsn=cls.DSN,
            min_size=cls.MIN_SIZE,
            max_size=cls.MAX_SIZE,
            command_timeout=cls.COMMAND_TIMEOUT,
        )
        print("✅ PostgreSQL 连接池初始化完成", flush=True)
        return cls._pool

    @classmethod
    async def _close_pool_locked(cls):
        """关闭超时或失败时强制终止；取消仍传递给调用者。"""
        pool = cls._pool
        cls._pool = None
        if pool is None:
            return
        try:
            await asyncio.wait_for(pool.close(), timeout=cls.CLOSE_TIMEOUT)
        except asyncio.CancelledError:
            pool.terminate()
            raise
        except Exception as exc:
            pool.terminate()
            print(f"⚠️ [PGPool] 关闭连接池失败，已强制终止: {type(exc).__name__}: {exc}", flush=True)

    @classmethod
    async def init_pool(cls, dsn: Optional[str] = None):
        if dsn:
            cls.DSN = dsn

        if cls._pool is not None:
            return cls._pool

        async with cls._lock:
            if cls._pool is None:
                await cls._create_pool_locked()
        return cls._pool

    @classmethod
    async def ensure_pool(cls):
        # 经过同一把锁，避免借用正在关闭/重建的池。
        async with cls._lock:
            if cls._pool is None:
                await cls._create_pool_locked()
            return cls._pool

    @classmethod
    async def close(cls):
        async with cls._lock:
            await cls._close_pool_locked()
            print("🛑 PostgreSQL 连接池已关闭", flush=True)

    @classmethod
    async def _rebuild_pool(cls):
        async with cls._lock:
            await cls._close_pool_locked()
            print("🔄 [PGPool] 正在重建 PostgreSQL 连接池…", flush=True)
            return await cls._create_pool_locked()

    # -------------------------
    # Unified SQL helpers
    # -------------------------

    @classmethod
    @reconnecting
    async def execute(cls, sql: str, *params, error_tag: str = "", retry_safe: bool = False) -> str:
        """
        返回 asyncpg execute 的状态串，例如 'INSERT 0 1'。
        默认不重试；只有可安全重复执行的 SQL 才能设置 retry_safe=True。
        """
        pool = await cls.ensure_pool()
        try:
            async with pool.acquire() as conn:
                return await conn.execute(sql, *params)
        except Exception as e:
            tag = error_tag or _caller_info()
            print(f"⚠️ [{tag}] SQL 执行出错: {e} | sql={sql} | params={params}", flush=True)
            raise

    @classmethod
    @reconnecting
    async def fetchrow(cls, sql: str, *params, error_tag: str = "", retry_safe: bool = False) -> Optional[asyncpg.Record]:
        pool = await cls.ensure_pool()
        try:
            async with pool.acquire() as conn:
                return await conn.fetchrow(sql, *params)
        except Exception as e:
            tag = error_tag or _caller_info()
            print(f"⚠️ [{tag}] SQL fetchrow 出错: {e} | sql={sql} | params={params}", flush=True)
            raise

    @classmethod
    @reconnecting
    async def fetch(cls, sql: str, *params, error_tag: str = "", retry_safe: bool = False) -> List[asyncpg.Record]:
        pool = await cls.ensure_pool()
        try:
            async with pool.acquire() as conn:
                return await conn.fetch(sql, *params)
        except Exception as e:
            tag = error_tag or _caller_info()
            print(f"⚠️ [{tag}] SQL fetch 出错: {e} | sql={sql} | params={params}", flush=True)
            raise

    @classmethod
    @reconnecting
    async def fetchval(cls, sql: str, *params, error_tag: str = "", retry_safe: bool = False) -> Any:
        pool = await cls.ensure_pool()
        try:
            async with pool.acquire() as conn:
                return await conn.fetchval(sql, *params)
        except Exception as e:
            tag = error_tag or _caller_info()
            print(f"⚠️ [{tag}] SQL fetchval 出错: {e} | sql={sql} | params={params}", flush=True)
            raise

    # -------------------------
    # Transaction helper
    # -------------------------

    @classmethod
    async def in_tx(cls, fn, *args, **kwargs):
        """
        用法：
            async def work(conn): ...
            await PGPool.in_tx(work)
        回调内必须使用传入的 conn；事务不自动重试。
        """
        pool = await cls.ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await fn(conn, *args, **kwargs)
