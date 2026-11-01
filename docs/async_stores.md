# 异步存储接口

`utils.async_stores` 提供 `UserExpireStore` 和 `BlacklistStore` 两个异步协议。
SQLite/PostgreSQL 使用相同的业务实现，通过数据库适配器处理连接和事务。
旧的 `utils.user_utils.UserExpireCache`、`utils.blacklist_utils.BlacklistStore`
仍然是同步接口，供尚未转换的其他调用方使用。
`main_taohua.py` 和 `main_airport.py` 已接入异步层，启动时初始化、
退出或启动失败时关闭存储。机场通过 `utils.airport_async_stores` 管理全部六张表。
切换后端不会自动复制 SQLite 数据到 PostgreSQL。

## 配置

SQLite 为默认后端：

```dotenv
DATABASE_BACKEND=sqlite
USER_EXPIRE_DB_PATH=data/user_expire.sqlite3
```

未指定文件路径时，优先使用 `RAILWAY_VOLUME_MOUNT_PATH/user_expire.sqlite3`，
否则使用项目的 `data/user_expire.sqlite3`。
SQLite 要求 3.35 或更高版本以支持 `RETURNING`。

PostgreSQL：

```dotenv
DATABASE_BACKEND=postgresql
DATABASE_URL=postgresql://user:password@host:5432/encbot
```

工厂在启动时创建两张表，并补充旧表缺失的 `group_message_timestamp` 和
`expires_at` 字段。PostgreSQL 的 ID 和时间戳使用 BIGINT。
已有 PostgreSQL 表若字段类型不符合要求，需要另行迁移。

### 机场使用独立数据库

机场主程序使用专属配置，默认保持 SQLite，不继承桃花的
`DATABASE_BACKEND` 或 `DATABASE_URL`。SQLite 文件路径仍沿用
`USER_EXPIRE_DB_PATH` / `RAILWAY_VOLUME_MOUNT_PATH` 的规则。

```dotenv
AIRPORT_DATABASE_BACKEND=sqlite
```

完成机场六表数据迁移及核对后，才能在机场服务中切换：

```dotenv
AIRPORT_DATABASE_BACKEND=postgresql
AIRPORT_DATABASE_URL=postgresql://user:password@host:5432/airport
```

`AIRPORT_DATABASE_URL` 必须指向机场独立数据库，不得指向桃花数据库。
未提供机场连接地址时 PostgreSQL 启动失败，不回退到桃花连接或 SQLite。
启动创建表结构不会导入旧数据；当前 `scripts/migrate_sqlite_to_postgres.py`
仅支持桃花两表，不能用来完成机场迁移。

## 生命周期和调用

```python
from utils.async_stores import create_stores

async def main():
    async with await create_stores() as stores:
        await stores.users.extend_minutes(123, 60)
        user = await stores.users.get(123)
        charged = await stores.users.consume_minutes(123, 10)
        await stores.blacklist.ban(456, "违规", created_by=123)
        blocked = await stores.blacklist.is_blocked(456)
```

也可以显式传入 `backend`、`sqlite_path`、`database_url` 覆盖环境变量。
应用启动只创建一次 bundle，注入各个处理器，停机时 `await stores.close()`。
两个 store 共用连接资源，由 bundle 统一关闭；重复关闭安全。
PostgreSQL 使用进程级 `PGPool`，不能同时创建多个独立 bundle 或另行管理同一个池。

所有业务方法都需要 `await`。`await stores.users.list_users()` 返回
`dict[int, UserExpire]` 快照，替代直接访问 `.users`；修改返回对象不会写入数据库。
新接口没有内存权威缓存或 `_load()`，恢复数据后下一次查询直接读取数据库。

SQLite 使用单独的工作线程和事务锁，不在线程间传递 connection 操作。
PostgreSQL 使用原生异步连接。奖励在事务内锁定用户行，扣减通过带余额条件的
UPDATE 完成，避免并发覆盖；数据库操作默认不自动重试。
这只保证数据库操作原子性，不提供 Telegram 消息去重或失败退款的业务幂等。
`stores.ban_user()` 在一个事务内写入黑名单并删除有效期；
`AsyncUserManager` 使用此入口，Telegram 群组封禁在数据库提交后执行。
`refund_minutes()` 原子加回失败发送扣除的时长，保留同时新增的奖励，
不会重建已被删除的用户；调用方仍需负责退款去重。
主程序的发言冷却检查通过本进程的用户级锁串行执行。

## 备份恢复

SQLite 模式继续支持 `/backup`、`/restore`、`/clear_media`。
恢复和失败回滚共用存储维护锁，阻止本进程处理器在恢复中途访问数据库；
恢复在 SQLite 工作线程执行，并补齐可选字段。回滚快照在获取维护锁后建立，
以保留备份上传期间产生的新写入。该锁不覆盖其他进程，其他共享该文件的
程序应在恢复期间停止写入。

PostgreSQL 模式下上述三个指令会提示不支持，且不会操作旧 SQLite 文件。
请使用 PostgreSQL 管理工具或托管平台执行备份恢复。

机场的手动维护与每日 SQLite 备份共用同一个维护任务锁。
机场 PostgreSQL 模式下，每日自动备份及依赖它的不活跃用户自动清理暂停，
并发送维护通知，不读取旧 SQLite 文件。上线前仍需接入 PostgreSQL 备份流程。
管理员主动执行的用户清理不受每日任务开关控制。

黑名单查询仅返回未过期记录，不会因读取而删除过期历史记录。
`list_by_reason_prefix()` 按字面前缀且区分大小写匹配，`%`、`_` 不作通配符，
以统一两个后端的行为；这与旧 SQLite `LIKE` 的细节不同。

## 验证

```sh
python -m unittest discover -s tests -p test_async_stores.py -v
```

默认运行 SQLite 契约测试。设置 `ASYNC_STORES_TEST_POSTGRES_DSN` 后，
相同契约测试也会运行于 PostgreSQL 的随机临时 schema，结束后删除该 schema。
请使用独立测试数据库及具备创建 schema 权限的测试账号。

机场主程序集成测试（临时 SQLite，模拟 Telegram，不启动真实机器人）：

```sh
.venv/bin/python -m unittest tests.test_airport_database tests.test_airport_async_stores -v
```
