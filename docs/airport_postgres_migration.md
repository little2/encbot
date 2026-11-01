# 机场机器人：独立 PostgreSQL 数据库迁移

本流程只适用于 `main_airport.py`。使用独立数据库及 `DATABASE_URL`，
不与桃花共用数据库。脚本不会修改 `.env`、启动或停止机器人。

## 范围和配置

迁移六表的全部字段：

| 表 | 唯一键 | 保留内容 |
| --- | --- | --- |
| `user_expire` | `user_id` | 通行证期限、更新时间、最近发言时间 |
| `user_blacklist` | `user_id` | 原因、管理员、封禁及到期时间，包括过期历史 |
| `batch` | `batch_id`，另有频道与消息 ID 唯一约束 | 频道及讨论区位置、内容、标签、上传者、发送设置和时间 |
| `batch_view` | `(batch_id, user_id)` | 每位用户对每个批次的浏览记录 |
| `received_media` | `file_unique_id` | 文件及缩略图 ID、时长、指纹、批次、pending/accepted 状态和时间 |
| `shared_invite_link` | `link_key` | 邀请链接、目标群、名称及验证时间 |

`NULL`、空字符串、BIGINT、中文和表情按原值保留。旧版本缺少的可选字段
按运行时约定补默认值，不覆盖已有字段。必要表或必要字段缺失会失败，
不会当作空表迁移。迁移工具不清理 pending 数据。

```dotenv
# 迁移和核对期间保持原后端
DATABASE_BACKEND=sqlite
# 必须指向机场独立数据库，不能指向桃花数据库
DATABASE_URL=postgresql://user:password@host:5432/airport
```

连接从进程环境或项目 `.env` 读取，进程环境优先。不把 URI 放在命令参数中。
机场模式必须明确指定源文件。机场与桃花使用相同变量名，但各自运行环境中的
`DATABASE_URL` 必须指向各自独立数据库；迁移前确认当前环境指向机场库。
默认目标 schema 是 `public`；如使用 `--schema`，运行时连接的 search_path
必须与迁移目标一致。通常独立数据库直接使用 `public` 即可。

## 预检查（只读）

确认源路径为机场生产 SQLite 文件，而不是桃花文件或旧备份：

```sh
.venv/bin/python scripts/migrate_sqlite_to_postgres.py --app airport --source /absolute/path/airport/user_expire.sqlite3
```

预检查不建表、不写数据、不创建备份。输出每表的源/目标行数、缺失、相同、
冲突和多余记录数，以及冲突字段名。目标缺表会列出；存在冲突或多余记录时
退出码为 1，但仍输出差异统计。没有目标数据时可以预检查通过。

检查源 SQLite 完整性、字段类型、主键及频道位置唯一性。已有 PostgreSQL
表须具备 BIGINT/TEXT 字段及必要唯一约束。机场标准表没有自定义外键或
触发器，若目标存在这些扩展，工具拒绝执行，避免级联删除或隐式写入。
工具全量读取所选表以逐字段比较，演练时应留意实际数据量和内存占用。

## 停写、导入和核对

先停止机场实例及其他源/目标写入者，并等待在途媒体任务结束；临时内存队列、
考试进度等不属于这六表，切换前需要让相关任务结束或接受重启后重新开始。

```sh
.venv/bin/python scripts/migrate_sqlite_to_postgres.py --app airport --source /absolute/path/airport/user_expire.sqlite3 --apply
.venv/bin/python scripts/migrate_sqlite_to_postgres.py --app airport --source /absolute/path/airport/user_expire.sqlite3 --verify
```

`--apply` 以 SQLite 为权威：插入缺失、覆盖不同、删除目标多余行，相同行跳过。
批次频道位置交换也能导入。SQLite 通过 `BEGIN IMMEDIATE` 暂时阻止新写入，
不修改源表；PostgreSQL 建表、六表排他锁、导入和核对处于同一事务。
锁等待超时或核对失败会整体回滚，不自动重试提交结果不明的事务。

写入前保存并核对一致性 SQLite 备份，以及目标六表的迁移前 JSON 快照。
默认在源文件旁的 `migration-backups/`，可用 `--backup-dir` 指定。
备份、原始目标快照和成功报告均为 0600 权限；请保留三者。
JSON 快照不是完整 PostgreSQL 数据库备份，不包含角色、权限或其他对象。
报告只包含统计、SHA-256 和路径，不包含原始记录或连接密码。

核对要求六表存在、所有行及字段相同；不仅比较行数或摘要。
提交时断线或提交后报告写入失败，先运行 `--verify` 判定数据库结果。
重复导入可以执行，但仍以当前 SQLite 覆盖目标；业务切换后不要再对旧 SQLite 执行导入。

## PostgreSQL 备份和恢复演练

机场运行环境必须安装 `pg_dump` 和 `pg_restore`，推荐与数据库使用同一主版本。
工具路径不必写进 `.env`：代码先查找项目 `.local/postgresql/<版本>/bin/` 中的
客户端（多个版本时选最高版本），再查找系统 PATH。也可用通用的 `PG_DUMP_BIN`、
`PG_RESTORE_BIN` 指定路径；配置项可省略，与操作系统无关。
本机已安装的 18.6 客户端可自动找到，不再需要本机绝对路径配置。
这不会安装软件：运行主机必须已提供兼容客户端；工具缺失或版本不兼容时备份失败，
本次自动清理取消。迁移到其他机器时，应在新机器安装相应的 PostgreSQL 客户端。
本机项目内的客户端仍依赖 Homebrew OpenSSL 3，已加入 gitignore，不随 Git 部署。
连接支持标准 PostgreSQL URI（含常见的 `sslmode`、证书路径、连接超时参数）；
不支持的查询参数会拒绝备份，不会静默忽略。密码通过子进程环境传递，
不会出现在命令参数、报告或工具错误输出中。

```sh
mkdir -p data/postgres-backups
.venv/bin/python scripts/backup_airport_postgres.py --output data/postgres-backups/airport-rehearsal.dump
```

该命令对机场**整个独立数据库**创建一致性 custom 格式归档，保留表结构及数据；
不包含集群角色或恢复时的原所有者/授权设置。不会覆盖已有文件。
归档以 0600 权限写入，检查目录和完整数据流可读取后才发布，失败不保留半成品。
成功报告包含大小和 SHA-256。归档检查不能替代真实恢复演练。

先由数据库管理工具创建一个空的、专用于恢复测试的数据库，再将其连接参数
设置为 libpq 环境变量（`PGHOST`、`PGPORT`、`PGUSER`、`PGDATABASE` 和密码/证书配置）。
确认 `PGDATABASE` 是恢复测试库，不能指向机场或桃花生产库，然后执行：

```sh
pg_restore --dbname="" --exit-on-error --no-owner --no-acl data/postgres-backups/airport-rehearsal.dump
```

空的 `--dbname` 从 libpq 环境变量取得连接。恢复后，用临时进程环境中的
`DATABASE_URL` 指向恢复测试库，对停写期间的 SQLite 源运行 `--verify`。
同时验证机器人所需表、浏览记录、邀请链接和媒体状态。
若原库已继续写入，旧 SQLite 与新备份不再代表同一时刻，应改用备份时的快照核对。

## 启动和每日维护

在导入、独立核对以及目标环境备份恢复演练通过后，将机场服务配置改为：

```dotenv
DATABASE_BACKEND=postgresql
POSTGRES_BACKUP_ENABLED=true
```

每日任务顺序是：pg_dump → 检查归档 → 本地保存 → 上传给指定账号 → 不活跃用户清理。
备份直接发送给现有共享配置中的 `KEY_MAN_ID`，不再配置额外收件人变量。
该账号须先向机场机器人发送 `/start`。KEY_MAN_ID 无效时取消本次备份和自动清理。
可选的通用 `BACKUP_DIR` 覆盖本地目录；不填时优先使用
`RAILWAY_VOLUME_MOUNT_PATH/postgres-backups/`，没有挂载卷时使用项目
`data/postgres-backups/`。这三个可选通用配置（目录及两个工具路径）均可省略。
工具缺失、归档检查失败、超时或上传失败时，本次自动清理取消。
`POSTGRES_BACKUP_ENABLED` 默认 false：未启用时备份和后续自动清理均暂停。
管理员主动用户清理不受每日任务开关控制。
本地完整备份以 0600 权限保留，上传成功或失败都不会删除；不自动清理历史文件，
请按需要管理本地保存周期。文件名带随机后缀，避免同秒重试覆盖备份。
上传大小限制导致发送失败也会取消清理，已落盘的备份仍保留。

手动 `/backup`、`/restore`、`/clear_media` 继续仅支持 SQLite；PostgreSQL 使用上述
脚本或数据库管理工具。启动机场后，运行时会清理超过 24 小时的 pending 媒体，
因此首次启动前必须完成与 SQLite 的完整核对。

PostgreSQL 产生新写入后，原 SQLite 已不包含增量，不能直接切回实现无损回滚。
切回前应停写并制定 PostgreSQL 增量回灌/恢复方案。

## 测试

```sh
.venv/bin/python -m unittest tests.test_airport_pg_migration tests.test_postgres_backup tests.test_airport_database -v
```

配置 `ASYNC_STORES_TEST_POSTGRES_DSN` 后运行 PostgreSQL 实库测试。
请使用独立测试服务器：迁移测试创建随机 schema，备份恢复测试创建两个随机数据库，
需要测试账号具备建库权限；结束后删除测试对象。所有源数据都是临时构造的数据。
不将机场或桃花生产 URL 配置为测试 DSN。
