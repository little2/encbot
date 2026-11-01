# 桃花机器人：SQLite → PostgreSQL

迁移仅处理 `user_expire` 和 `user_blacklist`，包括已过期的封禁记录，
保留 ID、原因和所有时间戳。本文描述默认的桃花模式；
机场使用独立的 `--app airport` 六表模式，见 [机场迁移流程](airport_postgres_migration.md)。

连接地址从进程环境或 `.env` 的 `DATABASE_URL` 读取（进程环境优先），不通过命令行传递或写入报告。
源文件默认沿用 `USER_EXPIRE_DB_PATH` / Railway 挂载路径 / 项目 data 路径。
可以用 `--source /absolute/path/user_expire.sqlite3` 指定生产源文件，
`--schema public` 指定目标 schema（schema 必须已存在）。

## 预检查（只读）

```sh
.venv/bin/python scripts/migrate_sqlite_to_postgres.py
```

预检查不会创建目标表或写入数据。检查 SQLite 完整性、必要字段、数据类型，
比较目标的缺失、相同、冲突和多余记录。旧 SQLite 缺少最近发言时间或
黑名单到期时间时补 0；必要表缺失会拒绝执行。
发现冲突时仍输出记录数和冲突字段名，不输出用户原始行内容；退出码为 1。

## 导入

先停止桃花机器人的业务写入，确认源文件是需要迁移的生产数据，再执行：

```sh
.venv/bin/python scripts/migrate_sqlite_to_postgres.py --apply
```

导入期间通过 SQLite `BEGIN IMMEDIATE` 暂时阻止源文件的新写入，不改源表数据。
PostgreSQL 的建表、插入和核对在同一事务中执行，对目标两表加排他锁，
阻止并发写入干扰核对。等待锁超时会失败，事务回滚。

SQLite 是唯一权威数据源：缺失记录会插入，同一 `user_id` 的不同记录会被
SQLite 覆盖，PostgreSQL 中 SQLite 不存在的多余记录会被删除；相同记录跳过。
完成后要求两张表与 SQLite 逐字段完全一致。
目标存在的表必须已具备 BIGINT/TEXT 字段类型及 user_id 唯一约束。

写入前创建只读来源的一致性 SQLite 备份并逐字段验证，默认存放于
源文件旁的 `migration-backups/`。备份和成功报告权限为 0600。
`--backup-dir` 可以指定备份目录；项目 data 下的默认备份目录已加入 gitignore。
报告只记录行数、SHA-256、比较结果及本地路径，不包含原始行或连接密码。

核对失败时两张表整体回滚，已创建的本地备份保留。若提交时网络断开，
不要直接假设成功或失败，先运行下面的只读核对。

程序在目标表锁定期间，先将两张
PostgreSQL 表的迁移前字段值保存为 `*.postgres-before.json`（0600 权限），
写入并同步到磁盘成功后，才执行覆盖、插入和多余记录删除。
覆盖后逐字段核对不一致时整个数据库事务回滚。

该 JSON 包含列名、原始行值和摘要，是这两张表的迁移前数据快照，
不是完整 PostgreSQL 数据库备份。请保留它与 SQLite 备份及迁移报告。
报告只包含统计与路径，原始行仅存在于受保护的快照文件。

## 独立核对

```sh
.venv/bin/python scripts/migrate_sqlite_to_postgres.py --verify
```

要求两张表的每条记录、每个字段完全相同，包括空表必须存在。
SHA-256 使用按 user_id 排序后的标准化内容计算，供人工复核；
实际判定同时使用逐字段比较，而不只比较数量或摘要。

迁移后再启动 PostgreSQL 后端。脚本不会修改 `.env`、启动或停止机器人。
一旦 PostgreSQL 开始产生新数据，旧 SQLite 备份不包含这些增量，
不能直接切回旧文件作为无损回滚。

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -p test_sqlite_pg_migration.py -v
```

设置 `ASYNC_STORES_TEST_POSTGRES_DSN` 后执行实库测试；
每项测试使用随机临时 schema，结束后删除，仅使用临时 SQLite 数据。
测试不向目标 `public` 导入生产数据。
