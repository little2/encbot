"""Private PostgreSQL custom archives; credentials never appear in argv or errors."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import parse_qsl, unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def get_backup_directory():
    configured = os.getenv("BACKUP_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    return (Path(volume) if volume else PROJECT_ROOT / "data") / "postgres-backups"


def resolve_postgres_tool(name, explicit=None):
    """Use an optional override, project client installation, then system PATH."""
    configured = explicit if explicit is not None else os.getenv(name.upper() + "_BIN", "")
    if configured and str(configured).strip():
        return str(Path(configured).expanduser())
    # Prefer a project installation to an older client elsewhere on PATH.
    candidates = []
    for candidate in (PROJECT_ROOT / ".local" / "postgresql").glob(f"*/bin/{name}"):
        try:
            version = tuple(int(part) for part in candidate.parent.parent.name.split("."))
        except ValueError:
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            candidates.append((version, candidate))
    if candidates:
        return str(max(candidates)[1])
    return shutil.which(name) or name


class PostgresBackupError(RuntimeError):
    pass


def postgres_connection_env(dsn):
    """Translate a standard PostgreSQL URI to libpq environment parameters."""
    names = {
        "host": "PGHOST", "hostaddr": "PGHOSTADDR", "port": "PGPORT", "user": "PGUSER",
        "password": "PGPASSWORD", "dbname": "PGDATABASE", "connect_timeout": "PGCONNECT_TIMEOUT",
        "sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY", "sslcrl": "PGSSLCRL", "application_name": "PGAPPNAME",
        "options": "PGOPTIONS", "target_session_attrs": "PGTARGETSESSIONATTRS",
    }
    try:
        url = urlsplit(dsn)
        if url.scheme not in {"postgres", "postgresql"}:
            raise ValueError()
        parameters = {"connect_timeout": "15"}
        for key, value in (("host", url.hostname), ("port", url.port),
                           ("user", url.username), ("password", url.password),
                           ("dbname", url.path[1:] if url.path.startswith("/") else url.path)):
            if value is not None:
                parameters[key] = unquote(str(value))
        for key, value in parse_qsl(url.query, keep_blank_values=True):
            if key not in names:
                raise ValueError()
            parameters[key] = value
        if not parameters.get("dbname") or any("\x00" in value for value in parameters.values()):
            raise ValueError()
    except (TypeError, ValueError):
        raise PostgresBackupError("备份连接须为标准 PostgreSQL URI，连接参数缺失或不受支持") from None
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env.update({names[key]: value for key, value in parameters.items()})
    return env


async def _run(*arguments, env, timeout):
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise PostgresBackupError("PostgreSQL 备份工具不可用，请检查 pg_dump/pg_restore 安装及路径") from None
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        raise
    if process.returncode:
        # Tool stderr may contain credentials, SQL or row data; do not surface it.
        raise PostgresBackupError(f"PostgreSQL 备份或归档检查失败（退出码 {process.returncode}）")


def _digest(path):
    with path.open("rb") as stream:
        if stream.read(5) != b"PGDMP":
            raise PostgresBackupError("备份不是 PostgreSQL custom 格式归档")
        stream.seek(0)
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def create_postgres_backup(dsn, destination, *, pg_dump_bin=None,
                                 pg_restore_bin=None, timeout=300):
    """Dump the entire dedicated database and check its archive directory.

    A readable archive is not proof of recoverability; rehearse pg_restore into
    a separate database before production cutover. The caller owns retention.
    """
    if not dsn or not dsn.strip():
        raise PostgresBackupError("DATABASE_URL 未配置")
    pg_dump_bin = resolve_postgres_tool("pg_dump", pg_dump_bin)
    pg_restore_bin = resolve_postgres_tool("pg_restore", pg_restore_bin)
    destination = Path(destination)
    if destination.exists():
        raise PostgresBackupError("备份目标已存在，拒绝覆盖")
    # Do not pass credentials in argv; inherited PG* settings must not redirect
    # or alter the requested database. PGDATABASE itself is a database name.
    env = postgres_connection_env(dsn)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".airport-pg-", suffix=".partial",
                                                 dir=destination.parent)
    os.close(descriptor)  # mkstemp permissions are 0600; pg_dump preserves them.
    temporary = Path(temporary_name)
    try:
        await _run(pg_dump_bin, "--format=custom", "--no-owner", "--no-acl", "--no-password",
                   "--file", str(temporary), env=env, timeout=timeout)
        digest = await asyncio.to_thread(_digest, temporary)
        await _run(pg_restore_bin, "--list", str(temporary), env=env, timeout=timeout)
        # Read/decompress all archive members, rather than only its directory.
        await _run(pg_restore_bin, "--file", os.devnull, "--no-owner", "--no-acl",
                   str(temporary), env=env, timeout=timeout)
        # Persist before publishing; link is atomic and refuses existing targets.
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(temporary, destination)
        return {"file": str(destination), "size_bytes": destination.stat().st_size,
                "sha256": digest, "format": "postgresql-custom", "archive_checked": True}
    except TimeoutError:
        raise PostgresBackupError("PostgreSQL 备份或归档检查超时") from None
    finally:
        temporary.unlink(missing_ok=True)
