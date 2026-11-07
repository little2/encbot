import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import asyncpg
from dotenv import load_dotenv

from scripts import migrate_sqlite_to_postgres as migration


load_dotenv(migration.ROOT / ".env", override=True)
TEST_DSN = os.getenv("ASYNC_STORES_TEST_POSTGRES_DSN", "")


def make_source(path, *, legacy=False):
    with closing(sqlite3.connect(path)) as conn, conn:
        extra_user = "" if legacy else ", group_message_timestamp INTEGER NOT NULL DEFAULT 0, subscription_status INTEGER NOT NULL DEFAULT 0"
        extra_ban = "" if legacy else ", expires_at INTEGER NOT NULL DEFAULT 0"
        conn.execute("CREATE TABLE user_expire (user_id INTEGER PRIMARY KEY, expire_timestamp INTEGER NOT NULL, update_timestamp INTEGER NOT NULL" + extra_user + ")")
        conn.execute("CREATE TABLE user_blacklist (user_id INTEGER PRIMARY KEY, reason TEXT NOT NULL, created_by INTEGER NOT NULL, created_at INTEGER NOT NULL" + extra_ban + ")")
        conn.execute("INSERT INTO user_expire (user_id, expire_timestamp, update_timestamp) VALUES (?, ?, ?)",
                     (9_000_000_001, 5_000_000_000, 1_700_000_000))
        conn.execute("INSERT INTO user_blacklist (user_id, reason, created_by, created_at) VALUES (?, ?, ?, ?)",
                     (9_000_000_002, "测试 Unicode 🍑 %_", 9_000_000_003, 1_700_000_001))
        if not legacy:
            conn.execute("UPDATE user_expire SET group_message_timestamp = 123, subscription_status = 1")
            conn.execute("UPDATE user_blacklist SET expires_at = 1700000002")


class MigrationSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "source.sqlite3"

    def test_legacy_defaults_and_private_snapshot(self):
        make_source(self.source, legacy=True)
        with migration.open_source(self.source, freeze=True) as conn:
            snapshot = migration.read_sqlite(conn)
            self.assertEqual(snapshot.tables["user_expire"][0][-2:], (0, 0))
            self.assertEqual(snapshot.tables["user_blacklist"][0][-1], 0)
            backup = migration.backup_source(self.source, Path(self.directory.name) / "backups", snapshot)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        with migration.open_source(backup) as conn:
            self.assertEqual(migration.read_sqlite(conn).tables, snapshot.tables)

    def test_subscription_status_is_included_in_snapshot(self):
        make_source(self.source)
        with migration.open_source(self.source) as conn:
            snapshot = migration.read_sqlite(conn)
        self.assertEqual(snapshot.tables["user_expire"][0][-2:], (123, 1))

    def test_apply_freezes_writers_and_releases_lock(self):
        make_source(self.source)
        with migration.open_source(self.source, freeze=True):
            with closing(sqlite3.connect(self.source, timeout=0.01)) as writer:
                with self.assertRaises(sqlite3.OperationalError):
                    writer.execute("UPDATE user_expire SET update_timestamp = 1")
        with closing(sqlite3.connect(self.source, timeout=0.01)) as writer, writer:
            writer.execute("UPDATE user_expire SET update_timestamp = 2")

    def test_missing_source_is_not_created(self):
        with self.assertRaises(migration.MigrationError):
            with migration.open_source(self.source):
                pass
        self.assertFalse(self.source.exists())

    def test_missing_required_table_is_not_silently_ignored(self):
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("CREATE TABLE user_expire (user_id INTEGER)")
        with migration.open_source(self.source) as conn:
            with self.assertRaises(migration.MigrationError):
                migration.read_sqlite(conn)

    def test_invalid_bigint_and_null_values_are_rejected(self):
        for value in (None, "not-an-integer", 2**63):
            with self.subTest(value=value):
                with self.assertRaises(migration.MigrationError):
                    migration.validate_rows("user_expire", [(1, value, 0, 0, 0)])

    def test_conflict_extra_and_missing_are_distinguished(self):
        source = migration.Snapshot({"user_expire": ((1, 2, 3, 4, 0), (5, 6, 7, 8, 1)), "user_blacklist": ()})
        target = migration.Snapshot({"user_expire": ((1, 2, 3, 9, 0), (9, 9, 9, 9, 0)), "user_blacklist": ()})
        diff = migration.compare(source, target)
        self.assertEqual(diff["user_expire"]["missing"], 1)
        self.assertEqual(diff["user_expire"]["conflicting"], 1)
        self.assertEqual(diff["user_expire"]["extra"], 1)
        self.assertEqual(diff["user_expire"]["conflict_fields"], {"group_message_timestamp": 1})
        with self.assertRaises(migration.MigrationError):
            migration.assert_compatible(diff)
        target.tables["user_expire"] = ((1, 2, 3, 9, 0),)
        diff = migration.compare(source, target)
        with self.assertRaises(migration.MigrationError):
            migration.assert_compatible(diff)
        with self.assertRaises(migration.MigrationError):
            migration.assert_compatible(diff, exact=True)


@unittest.skipUnless(TEST_DSN, "Set ASYNC_STORES_TEST_POSTGRES_DSN for migration integration tests")
class MigrationPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "source.sqlite3"
        self.backups = Path(self.directory.name) / "backups"
        make_source(self.source)
        self.schema = "encbot_migration_test_" + uuid4().hex
        self.admin = await asyncpg.connect(TEST_DSN, timeout=15, command_timeout=60)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')

        async def cleanup():
            await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')

        self.addAsyncCleanup(cleanup)

    async def run_migration(self, mode, **kwargs):
        return await migration.migrate(self.source, TEST_DSN, schema=self.schema,
                                       mode=mode, backup_dir=self.backups, **kwargs)

    async def test_preview_import_verify_and_repeated_import(self):
        preview = await self.run_migration("preview")
        self.assertEqual(set(preview["absent_tables"]), set(migration.TABLES))
        self.assertFalse(self.backups.exists())
        self.assertIsNone(await self.admin.fetchval("SELECT to_regclass($1)", f'"{self.schema}".user_expire'))
        first = await self.run_migration("apply")
        self.assertTrue(first["verified"])
        self.assertEqual(first["source"], first["target_after"])
        self.assertTrue(Path(first["backup_file"]).is_file())
        self.assertTrue(Path(first["report_file"]).is_file())
        second = await self.run_migration("apply")
        self.assertEqual(second["before"]["user_expire"]["missing"], 0)
        self.assertEqual(second["target_after"], first["target_after"])
        verified = await self.run_migration("verify")
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["source"], verified["target_before"])

    async def test_apply_overwrites_conflicts_and_deletes_target_extras(self):
        await self.run_migration("apply")
        await self.admin.execute(f'UPDATE "{self.schema}".user_expire SET expire_timestamp = 999')
        await self.admin.execute(f'INSERT INTO "{self.schema}".user_expire (user_id, expire_timestamp, update_timestamp, group_message_timestamp) VALUES (123, 1, 1, 0)')
        report = await self.run_migration("apply")
        self.assertEqual(report["before"]["user_expire"]["conflict_fields"],
                         {"expire_timestamp": 1})
        self.assertEqual(report["overwritten"]["user_expire"], 1)
        self.assertEqual(report["deleted"]["user_expire"], 1)
        self.assertEqual(await self.admin.fetchval(
            f'SELECT expire_timestamp FROM "{self.schema}".user_expire'), 5_000_000_000)
        self.assertEqual(await self.admin.fetchval(
            f'SELECT COUNT(*) FROM "{self.schema}".user_expire'), 1)

    async def test_empty_sqlite_tables_force_empty_postgres_tables(self):
        await self.run_migration("apply")
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("DELETE FROM user_expire")
            conn.execute("DELETE FROM user_blacklist")
        report = await self.run_migration("apply")
        self.assertEqual(report["deleted"], {"user_expire": 1, "user_blacklist": 1})
        self.assertEqual(await self.admin.fetchval(
            f'SELECT COUNT(*) FROM "{self.schema}".user_expire'), 0)
        self.assertEqual(await self.admin.fetchval(
            f'SELECT COUNT(*) FROM "{self.schema}".user_blacklist'), 0)

    async def test_sqlite_overwrites_conflicts_only_after_pg_preimage_is_saved(self):
        await self.run_migration("apply")
        await self.admin.execute(f'UPDATE "{self.schema}".user_expire SET expire_timestamp = 999')
        await self.admin.execute(f'UPDATE "{self.schema}".user_blacklist SET reason = \'old pg reason\'')
        before, _ = await migration.read_postgres(self.admin, self.schema)
        report = await self.run_migration("apply")
        self.assertEqual(report["overwritten"], {"user_expire": 1, "user_blacklist": 1})
        self.assertTrue(report["verified"])
        backup = Path(report["postgres_backup_file"])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        saved = json.loads(backup.read_text())
        self.assertEqual(saved["summary"], before.summary())
        self.assertEqual(saved["tables"]["user_expire"][0][1], 999)
        self.assertEqual(saved["tables"]["user_blacklist"][0][1], "old pg reason")
        await self.run_migration("verify")
        repeated = await self.run_migration("apply")
        self.assertEqual(repeated["overwritten"], {"user_expire": 0, "user_blacklist": 0})

    async def test_overwrite_verification_failure_restores_original_pg_rows(self):
        await self.run_migration("apply")
        await self.admin.execute(f'UPDATE "{self.schema}".user_expire SET expire_timestamp = 999')
        before, _ = await migration.read_postgres(self.admin, self.schema)
        original = migration.read_postgres
        calls = 0

        async def corrupted_read(conn, schema):
            nonlocal calls
            calls += 1
            if calls == 2:
                await conn.execute(f'UPDATE "{schema}".user_blacklist SET reason = \'injected mismatch\'')
            return await original(conn, schema)

        with patch.object(migration, "read_postgres", side_effect=corrupted_read):
            with self.assertRaises(migration.MigrationError):
                await self.run_migration("apply")
        after, _ = await migration.read_postgres(self.admin, self.schema)
        self.assertEqual(after.tables, before.tables)
        backups = list(self.backups.glob("*.postgres-before.json"))
        self.assertGreaterEqual(len(backups), 2)
        self.assertIn(before.summary(), [json.loads(path.read_text())["summary"] for path in backups])

    async def test_pg_backup_failure_prevents_overwrite(self):
        await self.run_migration("apply")
        await self.admin.execute(f'UPDATE "{self.schema}".user_expire SET expire_timestamp = 999')
        with patch.object(migration, "private_json", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                await self.run_migration("apply")
        self.assertEqual(await self.admin.fetchval(f'SELECT expire_timestamp FROM "{self.schema}".user_expire'), 999)

    async def test_verification_error_rolls_back_inserts_and_schema_creation(self):
        original = migration.read_postgres
        calls = 0

        async def corrupted_read(conn, schema):
            nonlocal calls
            calls += 1
            if calls == 2:
                await conn.execute(f'UPDATE "{schema}".user_blacklist SET reason = \'injected mismatch\'')
            return await original(conn, schema)

        with patch.object(migration, "read_postgres", side_effect=corrupted_read):
            with self.assertRaises(migration.MigrationError):
                await self.run_migration("apply")
        for table in migration.TABLES:
            self.assertIsNone(await self.admin.fetchval("SELECT to_regclass($1)", f'"{self.schema}"."{table}"'))
        self.assertEqual(len(list(self.backups.glob("*.sqlite3"))), 1)
        self.assertEqual(len(list(self.backups.glob("*.postgres-before.json"))), 1)
        self.assertEqual(len([
            path for path in self.backups.glob("*.json")
            if not path.name.endswith(".postgres-before.json")
        ]), 0)

    async def test_verify_is_read_only_and_requires_tables(self):
        with self.assertRaises(migration.MigrationError):
            await self.run_migration("verify")
        self.assertIsNone(await self.admin.fetchval("SELECT to_regclass($1)", f'"{self.schema}".user_expire'))


if __name__ == "__main__":
    unittest.main()
