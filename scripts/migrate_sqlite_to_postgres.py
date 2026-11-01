"""Preview, import, and verify Taohua's two tables.

Run from the repository root with .venv/bin/python scripts/migrate_sqlite_to_postgres.py.
Default mode is read-only preview. --apply treats SQLite as the authoritative
source and makes PostgreSQL exactly match it in one transaction, after backing
up the current PostgreSQL rows.
--verify requires exact equality. Credentials are read only from environment/.env.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from uuid import uuid4

import asyncpg
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
TABLES = {
    "user_expire": ("user_id", "expire_timestamp", "update_timestamp", "group_message_timestamp"),
    "user_blacklist": ("user_id", "reason", "created_by", "created_at", "expires_at"),
}
OPTIONAL_COLUMNS = {"group_message_timestamp", "expires_at"}
DDL = {
    "user_expire": """CREATE TABLE user_expire (
        user_id BIGINT PRIMARY KEY, expire_timestamp BIGINT NOT NULL,
        update_timestamp BIGINT NOT NULL, group_message_timestamp BIGINT NOT NULL DEFAULT 0
    )""",
    "user_blacklist": """CREATE TABLE user_blacklist (
        user_id BIGINT PRIMARY KEY, reason TEXT NOT NULL DEFAULT '',
        created_by BIGINT NOT NULL, created_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL DEFAULT 0
    )""",
}


class MigrationError(ValueError):
    def __init__(self, message, *, report=None):
        super().__init__(message)
        self.report = report


@dataclass
class Snapshot:
    tables: dict[str, tuple[tuple, ...]]

    def summary(self):
        result = {}
        for table, rows in self.tables.items():
            payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            result[table] = {"count": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}
        return result


def validate_rows(table, rows):
    columns = TABLES[table]
    seen = set()
    validated = []
    for row in rows:
        values = []
        for column, value in zip(columns, row):
            if column == "reason":
                valid = isinstance(value, str)
            else:
                valid = isinstance(value, int) and -(2**63) <= value < 2**63
            if not valid:
                raise MigrationError(f"{table}.{column} 包含空值、错误类型或超出 BIGINT 范围的数据")
            values.append(value)
        if values[0] in seen:
            raise MigrationError(f"{table} 包含重复 user_id")
        seen.add(values[0])
        validated.append(tuple(values))
    return tuple(sorted(validated, key=lambda row: row[0]))


def read_sqlite(connection):
    integrity = connection.execute("PRAGMA quick_check").fetchall()
    if integrity != [("ok",)]:
        raise MigrationError("SQLite 完整性检查失败")
    existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    tables = {}
    for table, columns in TABLES.items():
        if table not in existing:
            raise MigrationError(f"SQLite 缺少必要表 {table}，拒绝将缺表当作空表")
        actual = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        missing = set(columns) - actual
        if missing - OPTIONAL_COLUMNS:
            raise MigrationError(f"SQLite {table} 缺少必要字段：{', '.join(sorted(missing))}")
        projection = ", ".join(f'"{name}"' if name in actual else f'0 AS "{name}"' for name in columns)
        rows = connection.execute(f'SELECT {projection} FROM "{table}"').fetchall()
        tables[table] = validate_rows(table, rows)
    return Snapshot(tables)


@contextmanager
def open_source(path: Path, *, freeze=False):
    path = path.resolve()
    if not path.is_file():
        raise MigrationError("SQLite 源文件不存在")
    mode = "rw" if freeze else "ro"
    with closing(sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=10,
                                 isolation_level=None)) as connection:
        try:
            # APPLY temporarily blocks SQLite writers; no source rows are modified.
            connection.execute("BEGIN IMMEDIATE" if freeze else "BEGIN")
            yield connection
        finally:
            connection.rollback()


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def backup_source(source: Path, directory: Path, expected: Snapshot):
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"taohua-before-pg-{stamp}-{uuid4().hex[:12]}.sqlite3"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    # Read via a separate connection: backup on a connection holding BEGIN
    # IMMEDIATE can wait on its own write transaction. Caller keeps writers frozen.
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(path)) as destination:
            reader.backup(destination)
            if read_sqlite(destination).tables != expected.tables:
                raise MigrationError("SQLite 备份逐字段核对失败")
    return path


async def read_postgres(conn, schema):
    tables, absent = {}, []
    for table, columns in TABLES.items():
        metadata = await conn.fetch("""
            SELECT column_name, udt_name FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = $2
        """, schema, table)
        if not metadata:
            absent.append(table)
            tables[table] = ()
            continue
        types = {row["column_name"]: row["udt_name"] for row in metadata}
        if any(types.get(name) != ("text" if name == "reason" else "int8") for name in columns):
            raise MigrationError(f"PostgreSQL {table} 的字段缺失或类型不符合 BIGINT/TEXT 要求")
        projection = ", ".join(f'"{name}"' for name in columns)
        rows = await conn.fetch(f'SELECT {projection} FROM "{schema}"."{table}"')
        tables[table] = validate_rows(table, rows)
    return Snapshot(tables), absent


def compare(source: Snapshot, target: Snapshot):
    result = {}
    for table in TABLES:
        left = {row[0]: row for row in source.tables[table]}
        right = {row[0]: row for row in target.tables[table]}
        shared = left.keys() & right.keys()
        result[table] = {
            "source_count": len(left), "target_count": len(right),
            "missing": len(left.keys() - right.keys()),
            "identical": sum(left[key] == right[key] for key in shared),
            "conflicting": sum(left[key] != right[key] for key in shared),
            "extra": len(right.keys() - left.keys()),
            "conflict_fields": {
                column: sum(left[key][index] != right[key][index] for key in shared)
                for index, column in enumerate(TABLES[table])
                if any(left[key][index] != right[key][index] for key in shared)
            },
        }
    return result


def assert_compatible(differences, *, exact=False):
    for table, diff in differences.items():
        if diff["extra"] or diff["conflicting"]:
            raise MigrationError(f"{table}: 目标存在 {diff['conflicting']} 条不同记录、{diff['extra']} 条多余记录；拒绝覆盖或删除")
        if exact and diff["missing"]:
            raise MigrationError(f"{table}: 目标缺少 {diff['missing']} 条记录")


async def migrate(source: Path, dsn: str, *, schema="public", mode="preview",
                  backup_dir: Path | None = None):
    if mode not in {"preview", "apply", "verify"}:
        raise MigrationError("mode 必须为 preview、apply 或 verify")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema):
        raise MigrationError("schema 名称必须为不超过 63 字符的字母、数字、下划线标识符")
    if not dsn.strip():
        raise MigrationError("目标数据库连接配置为空")
    source = Path(source).resolve()
    backup_dir = Path(backup_dir) if backup_dir is not None else source.parent / "migration-backups"
    report = {"mode": mode, "schema": schema, "source_file": str(source),
              "conflict_policy": "sqlite-authoritative" if mode == "apply" else "error",
              "created_at_utc": datetime.now(timezone.utc).isoformat()}
    with open_source(source, freeze=mode == "apply") as sqlite_conn:
        snapshot = read_sqlite(sqlite_conn)
        report["source"] = snapshot.summary()
        conn = await asyncpg.connect(dsn, timeout=15, command_timeout=60,
                                     server_settings={"search_path": f'"{schema}"'})
        try:
            isolation = "read_committed" if mode == "apply" else "repeatable_read"
            async with conn.transaction(isolation=isolation, readonly=mode != "apply"):
                await conn.execute("SET LOCAL lock_timeout = '10s'")
                await conn.execute("SET LOCAL row_security = off")
                if not await conn.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", schema):
                    raise MigrationError("目标 schema 不存在，请先创建专用 schema 或明确选择 public")
                if mode == "apply":
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtext(current_database()), hashtext($1::text))",
                                       "encbot-sqlite-migration:" + schema)
                    # Table creation, inserts and verification share one transaction.
                    for table, ddl in DDL.items():
                        exists = await conn.fetchval("SELECT to_regclass($1)", f'"{schema}"."{table}"')
                        if exists is None:
                            await conn.execute(ddl)
                    names = ", ".join(f'"{schema}"."{table}"' for table in TABLES)
                    await conn.execute(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE")
                target, absent = await read_postgres(conn, schema)
                differences = compare(snapshot, target)
                report["before"] = differences
                report["absent_tables"] = absent
                report["target_before"] = target.summary()
                if mode != "apply":
                    assert_compatible(differences, exact=mode == "verify")
                if mode == "verify" and absent:
                    raise MigrationError("目标缺表，不能判定为完全一致")
                if mode == "apply":
                    backup = backup_source(source, backup_dir, snapshot)
                    report["backup_file"] = str(backup)
                    # Persist the full pre-image before any destructive write while
                    # both target tables remain locked. This is a table snapshot,
                    # not a complete pg_dump of the database.
                    pg_backup = backup.with_suffix(".postgres-before.json")
                    private_json(pg_backup, {
                        "format": "encbot-postgres-table-snapshot-v1",
                        "schema": schema, "created_at_utc": report["created_at_utc"],
                        "columns": TABLES, "tables": target.tables, "summary": target.summary(),
                    })
                    report["postgres_backup_file"] = str(pg_backup)
                    report["inserted"] = {}
                    report["overwritten"] = {}
                    report["deleted"] = {}
                    for table, columns in TABLES.items():
                        projection = ", ".join(f'"{name}"' for name in columns)
                        placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
                        existing = {row[0]: row for row in target.tables[table]}
                        pending = [row for row in snapshot.tables[table]
                                   if row[0] not in existing or row != existing[row[0]]]
                        report["inserted"][table] = differences[table]["missing"]
                        report["overwritten"][table] = differences[table]["conflicting"]
                        report["deleted"][table] = differences[table]["extra"]
                        source_ids = [row[0] for row in snapshot.tables[table]]
                        if source_ids:
                            await conn.execute(
                                f'DELETE FROM "{schema}"."{table}" WHERE NOT (user_id = ANY($1::bigint[]))',
                                source_ids,
                            )
                        else:
                            await conn.execute(f'DELETE FROM "{schema}"."{table}"')
                        assignments = ", ".join(f'"{name}" = EXCLUDED."{name}"' for name in columns[1:])
                        conflict_action = "DO UPDATE SET " + assignments
                        for offset in range(0, len(pending), 1000):
                            await conn.executemany(
                                f'INSERT INTO "{schema}"."{table}" ({projection}) VALUES ({placeholders}) '
                                "ON CONFLICT(user_id) " + conflict_action, pending[offset:offset + 1000])
                    after, absent = await read_postgres(conn, schema)
                    assert_compatible(compare(snapshot, after), exact=True)
                    if absent or after.tables != snapshot.tables:
                        raise MigrationError("导入后逐字段核对失败，事务将回滚")
                    report["target_after"] = after.summary()
                    report["verified"] = True
                elif mode == "verify":
                    report["verified"] = True
            # Only report success once COMMIT returns; never retry unknown outcomes.
            report["status"] = "committed" if mode == "apply" else "ok"
        except MigrationError as exc:
            exc.report = report
            raise
        finally:
            await conn.close(timeout=10)
    if mode == "apply":
        report_path = Path(report["backup_file"]).with_suffix(".json")
        report["report_file"] = str(report_path)
        private_json(report_path, report)
    return report


def default_source():
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    default = Path(volume) / "user_expire.sqlite3" if volume else ROOT / "data" / "user_expire.sqlite3"
    return Path(os.getenv("USER_EXPIRE_DB_PATH", str(default)))


def main():
    load_dotenv(ROOT / ".env", override=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_source())
    parser.add_argument("--schema", default="public")
    parser.add_argument("--dsn-env", default="DATABASE_URL", help="环境变量名称，不是连接字符串")
    parser.add_argument("--backup-dir", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--apply", action="store_true",
        help="锁定 SQLite 写入，以 SQLite 为准覆盖并删除 PostgreSQL 差异后核对",
    )
    group.add_argument("--verify", action="store_true", help="只读检查所有字段完全一致")
    args = parser.parse_args()
    mode = "apply" if args.apply else "verify" if args.verify else "preview"
    try:
        report = asyncio.run(migrate(args.source, os.getenv(args.dsn_env, ""), schema=args.schema,
                                     mode=mode, backup_dir=args.backup_dir))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except MigrationError as exc:
        if exc.report is not None:
            print(json.dumps(exc.report, ensure_ascii=False, indent=2))
        print(f"迁移检查未通过：{exc}", file=sys.stderr)
    except Exception as exc:
        # Do not print driver errors/DSNs: they can contain credentials or row data.
        print(f"迁移未完成：{type(exc).__name__}。请检查连接或权限；如提交时断线，请先运行 --verify 核对。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
