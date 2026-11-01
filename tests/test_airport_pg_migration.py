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

from scripts import migrate_sqlite_to_postgres as migration
from tests.test_sqlite_pg_migration import make_source, TEST_DSN
from utils.batch_utils import BatchStore
from utils.batch_view_utils import BatchViewStore
from utils.invite_link_utils import SharedInviteLinkStore
from utils.received_media_utils import ReceivedMediaStore


def make_airport_source(path):
    make_source(path)
    # Use the established synchronous application's schema as migration source.
    for factory in (BatchStore, BatchViewStore, ReceivedMediaStore, SharedInviteLinkStore):
        store = factory(path)
        store.close()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO batch VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("a", -1009000000001, 1, -1009000000002, 2, "机场 ✈️ %_", None,
                      9000000001, 1, 0, 0, 30, "20270101000000", 100, 101))
        conn.execute("INSERT INTO batch VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("b", -1009000000001, 2, None, None, None, "", 0, 0, 1, 1, 0,
                      "99991231235959", 102, 103))
        conn.executemany("INSERT INTO batch_view VALUES (?, ?, ?)",
                         [("a", 1, 100), ("a", 2, 101), ("b", 1, 102)])
        cols = migration.AIRPORT_TABLES["received_media"]
        conn.executemany("INSERT INTO received_media (" + ','.join(cols) + ") VALUES (" + ','.join('?' for _ in cols) + ")", [
            ("accepted", "file-a", "video", 65, 9000000001, -1009000000001, 1, "accepted", "a",
             "thumb", "thumb-unique", "abcd", 100, 200),
            ("pending", "", "photo", 0, 9000000001, -1009000000001, 2, "pending", None,
             None, None, None, 1, None),
        ])
        conn.execute("INSERT INTO shared_invite_link VALUES (?, ?, ?, ?, ?, ?)",
                     ("airport", -1009000000001, "https://t.me/+test", "机场 ✈️", 100, 101))


class AirportMigrationSourceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / 'airport.sqlite3'
        make_airport_source(self.source)

    def snapshot(self):
        with migration.open_source(self.source) as conn:
            return migration.read_sqlite(conn, app="airport")

    def test_all_fields_nulls_empty_strings_and_pending_are_preserved(self):
        snapshot = self.snapshot()
        self.assertEqual(set(snapshot.tables), set(migration.AIRPORT_TABLES))
        self.assertEqual(len(snapshot.tables["batch_view"]), 3)
        self.assertIsNone(snapshot.tables["batch"][0][6])
        self.assertIsNone(snapshot.tables["received_media"][1][-1])
        self.assertEqual(snapshot.tables["received_media"][1][1], "")
        self.assertEqual(snapshot.tables["received_media"][1][7], "pending")
        with migration.open_source(self.source, freeze=True):
            backup = migration.backup_source(self.source, self.source.parent / "backups", snapshot)
        self.assertTrue(backup.name.startswith("airport-before-pg-"))
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        with migration.open_source(backup) as conn:
            self.assertEqual(migration.read_sqlite(conn, app="airport").tables, snapshot.tables)

    def test_composite_primary_key_comparison(self):
        source = self.snapshot()
        target = self.snapshot()
        target.tables["batch_view"] = (("a", 1, 999), ("a", 3, 100), ("b", 1, 102))
        diff = migration.compare(source, target)["batch_view"]
        self.assertEqual((diff["missing"], diff["extra"], diff["conflicting"], diff["identical"]), (1, 1, 1, 1))
        self.assertEqual(diff["conflict_fields"], {"timestamp": 1})

    def test_missing_required_airport_table_is_rejected(self):
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("DROP TABLE batch_view")
        with self.assertRaisesRegex(migration.MigrationError, "batch_view"):
            self.snapshot()

    def test_legacy_optional_columns_receive_runtime_defaults(self):
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute("DROP INDEX idx_received_media_batch_id")
            for table, defaults in migration.LEGACY_DEFAULTS.items():
                for column in defaults:
                    conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        snapshot = self.snapshot()
        self.assertEqual(snapshot.tables["batch"][0][10:13], (1, 0, "99991231235959"))
        self.assertEqual(snapshot.tables["received_media"][0][1], "")
        self.assertIsNone(snapshot.tables["received_media"][0][-1])

    def test_invalid_status_duplicate_composite_key_and_bad_types_fail(self):
        snapshot = self.snapshot()
        row = list(snapshot.tables['received_media'][0])
        row[7] = 'unknown'
        with self.assertRaisesRegex(migration.MigrationError, 'status'):
            migration.validate_rows('received_media', [row])
        with self.assertRaisesRegex(migration.MigrationError, 'batch_id, user_id'):
            migration.validate_rows('batch_view', [('a', 1, 1), ('a', 1, 2)])
        for value in (None, 1, 'nul\x00value'):
            row = list(snapshot.tables['shared_invite_link'][0])
            row[2] = value
            with self.assertRaises(migration.MigrationError):
                migration.validate_rows('shared_invite_link', [row])
        with self.assertRaisesRegex(migration.MigrationError, 'channel_chat_id, channel_message_id'):
            row = list(snapshot.tables['batch'][0])
            row[0] = 'different-id'
            migration.validate_rows('batch', [snapshot.tables['batch'][0], row])

    def test_cli_requires_explicit_airport_source(self):
        with patch.object(migration, 'load_dotenv'), patch('sys.argv', ["migration", "--app", "airport"]), patch('sys.stderr'):
            with self.assertRaises(SystemExit) as raised:
                migration.main()
            self.assertEqual(raised.exception.code, 2)

    def test_cli_uses_generic_database_url(self):
        from unittest.mock import AsyncMock
        migrate = AsyncMock(return_value={'status': 'ok'})
        with patch.object(migration, 'load_dotenv'), patch.object(migration, 'migrate', migrate), \
             patch.dict(os.environ, {'DATABASE_URL': 'postgresql://airport.invalid/airport'}, clear=True), \
             patch('sys.argv', ['migration', '--app', 'airport', '--source', str(self.source)]), patch('builtins.print'):
            self.assertEqual(migration.main(), 0)
        self.assertEqual(migrate.await_args.args[1], 'postgresql://airport.invalid/airport')
        self.assertEqual(migrate.await_args.kwargs['app'], 'airport')


@unittest.skipUnless(TEST_DSN, "Set ASYNC_STORES_TEST_POSTGRES_DSN for airport migration rehearsal")
class AirportMigrationPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / "airport.sqlite3"
        self.backups = Path(directory.name) / "backups"
        make_airport_source(self.source)
        self.schema = 'airport_migration_' + uuid4().hex
        self.admin = await asyncpg.connect(TEST_DSN, timeout=15, command_timeout=60)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')

        async def cleanup():
            await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')

        self.addAsyncCleanup(cleanup)

    async def run_migration(self, mode):
        return await migration.migrate(self.source, TEST_DSN, schema=self.schema, mode=mode,
                                       backup_dir=self.backups, app="airport")

    async def read_target(self):
        return (await migration.read_postgres(self.admin, self.schema, app="airport"))[0]

    async def test_preview_apply_verify_and_repeated_import(self):
        preview = await self.run_migration('preview')
        self.assertEqual(set(preview['absent_tables']), set(migration.AIRPORT_TABLES))
        self.assertFalse(self.backups.exists())
        first = await self.run_migration('apply')
        self.assertEqual(first['source'], first['target_after'])
        self.assertEqual(first['inserted']['batch_view'], 3)
        self.assertEqual((await self.read_target()).tables['received_media'][1][7], 'pending')
        second = await self.run_migration('apply')
        self.assertTrue(all(value == 0 for value in second['inserted'].values()))
        self.assertTrue(all(value == 0 for value in second['overwritten'].values()))
        self.assertTrue((await self.run_migration('verify'))['verified'])
        report_text = Path(first['report_file']).read_text()
        self.assertNotIn('https://t.me/+test', report_text)
        self.assertNotIn('file-a', report_text)
        self.assertNotIn(TEST_DSN, report_text)

    async def test_overwrites_composite_key_conflicts_and_deletes_only_extra_rows(self):
        await self.run_migration('apply')
        await self.admin.execute(f'UPDATE "{self.schema}".batch_view SET timestamp=999 WHERE batch_id=\'a\' AND user_id=1')
        await self.admin.execute(f'INSERT INTO "{self.schema}".batch_view VALUES (\'a\', 3, 100)')
        with self.assertRaises(migration.MigrationError):
            await self.run_migration('preview')
        report = await self.run_migration('apply')
        self.assertEqual(report['overwritten']['batch_view'], 1)
        self.assertEqual(report['deleted']['batch_view'], 1)
        self.assertEqual((await self.read_target()).tables['batch_view'], (('a', 1, 100), ('a', 2, 101), ('b', 1, 102)))
        saved = json.loads(Path(report['postgres_backup_file']).read_text())
        self.assertIn(['a', 1, 999], saved['tables']['batch_view'])

    async def test_channel_locations_can_swap_between_batches(self):
        await self.run_migration('apply')
        with closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute('UPDATE batch SET channel_message_id=channel_message_id+10')
            conn.execute('UPDATE batch SET channel_message_id=CASE batch_id WHEN \'a\' THEN 2 ELSE 1 END')
        self.assertEqual((await self.run_migration('apply'))['overwritten']['batch'], 2)
        await self.run_migration('verify')

    async def test_verification_failure_rolls_back_all_six_tables(self):
        await self.run_migration('apply')
        await self.admin.execute(f'UPDATE "{self.schema}".user_expire SET expire_timestamp=999')
        before = await self.read_target()
        original = migration.read_postgres
        calls = 0

        async def corrupted_read(conn, schema, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                await conn.execute(f'UPDATE "{schema}".shared_invite_link SET name=\'injected mismatch\'')
            return await original(conn, schema, **kwargs)

        with patch.object(migration, 'read_postgres', side_effect=corrupted_read):
            with self.assertRaises(migration.MigrationError):
                await self.run_migration('apply')
        self.assertEqual((await self.read_target()).tables, before.tables)

    async def test_backup_failure_prevents_overwrites(self):
        await self.run_migration('apply')
        await self.admin.execute(f'UPDATE "{self.schema}".batch_view SET timestamp=999')
        before = await self.read_target()
        with patch.object(migration, 'private_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                await self.run_migration('apply')
        self.assertEqual((await self.read_target()).tables, before.tables)

    async def test_empty_source_tables_clear_all_six_targets(self):
        await self.run_migration('apply')
        with closing(sqlite3.connect(self.source)) as conn, conn:
            for table in migration.AIRPORT_TABLES:
                conn.execute(f'DELETE FROM "{table}"')
        report = await self.run_migration('apply')
        self.assertTrue(all(row['count'] == 0 for row in report['target_after'].values()))
        await self.run_migration('verify')

    async def test_missing_composite_unique_constraint_is_rejected(self):
        await self.run_migration('apply')
        await self.admin.execute(f'ALTER TABLE "{self.schema}".batch_view DROP CONSTRAINT batch_view_pkey')
        with self.assertRaisesRegex(migration.MigrationError, '唯一约束'):
            await self.run_migration('apply')

    async def test_custom_cascading_foreign_key_is_rejected_before_writes(self):
        await self.run_migration('apply')
        await self.admin.execute(f'CREATE TABLE "{self.schema}".custom_child '
                                 f'(batch_id TEXT REFERENCES "{self.schema}".batch(batch_id) ON DELETE CASCADE)')
        await self.admin.execute(f'INSERT INTO "{self.schema}".custom_child VALUES (\'a\')')
        with self.assertRaisesRegex(migration.MigrationError, '自定义外键'):
            await self.run_migration('apply')
        self.assertEqual(await self.admin.fetchval(f'SELECT COUNT(*) FROM "{self.schema}".custom_child'), 1)


if __name__ == '__main__':
    unittest.main()
