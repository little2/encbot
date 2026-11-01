import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg

from scripts import migrate_sqlite_to_postgres as migration
from tests.test_airport_pg_migration import make_airport_source
from tests.test_sqlite_pg_migration import TEST_DSN
from utils.postgres_backup import create_postgres_backup, PostgresBackupError, postgres_connection_env, _run
from utils.postgres_backup import get_backup_directory, resolve_postgres_tool


class BackupConfigurationTests(unittest.TestCase):
    def test_backup_directory_defaults_and_volume(self):
        with patch.dict(os.environ, {}, clear=True), patch('utils.postgres_backup.PROJECT_ROOT', Path('/project')):
            self.assertEqual(get_backup_directory(), Path('/project/data/postgres-backups'))
            with patch.dict(os.environ, {'RAILWAY_VOLUME_MOUNT_PATH': '/volume'}):
                self.assertEqual(get_backup_directory(), Path('/volume/postgres-backups'))
            with patch.dict(os.environ, {'BACKUP_DIR': '/custom/backups'}):
                self.assertEqual(get_backup_directory(), Path('/custom/backups'))

    def test_tools_can_be_overridden_using_generic_configuration(self):
        with patch.dict(os.environ, {'PG_DUMP_BIN': '/custom/pg_dump'}, clear=True):
            self.assertEqual(resolve_postgres_tool('pg_dump'), '/custom/pg_dump')
            self.assertEqual(resolve_postgres_tool('pg_dump', '/explicit/pg_dump'), '/explicit/pg_dump')

    def test_project_client_uses_newest_numeric_version_before_system_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for version in ('18.9', '18.10', 'invalid'):
                binary = root / '.local/postgresql' / version / 'bin/pg_dump'
                binary.parent.mkdir(parents=True)
                binary.write_text('stub')
                binary.chmod(0o755)
            with patch.dict(os.environ, {}, clear=True), patch('utils.postgres_backup.PROJECT_ROOT', root), \
                 patch('utils.postgres_backup.shutil.which', return_value='/system/pg_dump'):
                self.assertEqual(resolve_postgres_tool('pg_dump'), str(root / '.local/postgresql/18.10/bin/pg_dump'))

    def test_tools_fall_back_to_system_path(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), \
             patch('utils.postgres_backup.PROJECT_ROOT', Path(directory)), \
             patch('utils.postgres_backup.shutil.which', return_value='/system/pg_restore'):
            self.assertEqual(resolve_postgres_tool('pg_restore'), '/system/pg_restore')

    def test_missing_tool_is_reported_without_creating_backup(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), \
                 patch('utils.postgres_backup.PROJECT_ROOT', Path(directory)), \
                 patch('utils.postgres_backup.shutil.which', return_value=None):
                destination = Path(directory) / 'missing.dump'
                with self.assertRaises(PostgresBackupError):
                    await create_postgres_backup('postgresql://test/test', destination,
                                                 pg_dump_bin=str(Path(directory) / 'missing-tool'))
                self.assertFalse(destination.exists())
                self.assertEqual(list(Path(directory).iterdir()), [])
        asyncio.run(run())


class PostgresBackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.destination = self.directory / 'airport.dump'

    async def test_password_is_only_in_environment_and_archive_is_private(self):
        calls = []
        dsn = 'postgresql://airport:secret-password@example.invalid/airport'

        async def run(*args, env, timeout):
            self.assertNotIn('secret-password', ' '.join(args))
            self.assertEqual(env['PGDATABASE'], 'airport')
            self.assertEqual(env['PGPASSWORD'], 'secret-password')
            self.assertEqual(env['PGHOST'], 'example.invalid')
            calls.append(args)
            if args[0] == 'dump-test':
                Path(args[args.index('--file') + 1]).write_bytes(b'PGDMP-test-archive')

        with patch('utils.postgres_backup._run', side_effect=run), patch.dict(os.environ, {'PGHOST': 'wrong-host'}):
            report = await create_postgres_backup(dsn, self.destination,
                                                  pg_dump_bin='dump-test', pg_restore_bin='restore-test')
        self.assertEqual(report['sha256'], hashlib.sha256(self.destination.read_bytes()).hexdigest())
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(calls), 3)
        self.assertTrue(report['archive_checked'])
        self.assertEqual(list(self.directory.glob('*.partial')), [])

    async def test_failed_or_corrupt_backup_never_publishes_partial_archive(self):
        async def corrupt(*args, **kwargs):
            if args[0] == 'dump-test':
                Path(args[args.index('--file') + 1]).write_bytes(b'corrupt')

        for effect in (PostgresBackupError('tool failed'), corrupt, TimeoutError()):
            with self.subTest(effect=type(effect).__name__), patch('utils.postgres_backup._run', side_effect=effect):
                with self.assertRaises(PostgresBackupError):
                    await create_postgres_backup('postgresql://test/test', self.destination, pg_dump_bin='dump-test')
                self.assertFalse(self.destination.exists())
                self.assertEqual(list(self.directory.iterdir()), [])

    async def test_archive_decompression_failure_is_not_reported_as_success(self):
        async def run(*args, **kwargs):
            if args[0] == 'dump-test':
                Path(args[args.index('--file') + 1]).write_bytes(b'PGDMP-invalid-stream')
            elif '--file' in args:
                raise PostgresBackupError('cannot decompress')

        with patch('utils.postgres_backup._run', side_effect=run):
            with self.assertRaises(PostgresBackupError):
                await create_postgres_backup('postgresql://test/test', self.destination, pg_dump_bin='dump-test')
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_existing_backup_is_not_overwritten(self):
        self.destination.write_bytes(b'keep')
        with self.assertRaises(PostgresBackupError):
            await create_postgres_backup('postgresql://test/test', self.destination)
        self.assertEqual(self.destination.read_bytes(), b'keep')

    async def test_cancellation_removes_partial_archive(self):
        async def run(*args, **kwargs):
            raise asyncio.CancelledError()

        with patch('utils.postgres_backup._run', side_effect=run):
            with self.assertRaises(asyncio.CancelledError):
                await create_postgres_backup('postgresql://test/test', self.destination)
        self.assertEqual(list(self.directory.iterdir()), [])


@unittest.skipUnless(TEST_DSN, 'Set ASYNC_STORES_TEST_POSTGRES_DSN for backup/restore rehearsal')
class PostgresBackupRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_archive_restores_all_six_tables_into_separate_database(self):
        admin = await asyncpg.connect(TEST_DSN)
        self.addAsyncCleanup(admin.close)
        source_database = 'airport_dump_source_' + uuid4().hex
        target_database = 'airport_dump_target_' + uuid4().hex
        for name in (source_database, target_database):
            await admin.execute(f'CREATE DATABASE "{name}"')

            async def cleanup(database=name):
                await admin.execute(f'DROP DATABASE "{database}" WITH (FORCE)')

            self.addAsyncCleanup(cleanup)
        url = urlsplit(TEST_DSN)
        source_dsn = urlunsplit(url._replace(path='/' + source_database))
        target_dsn = urlunsplit(url._replace(path='/' + target_database))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        source = path / 'source.sqlite3'
        make_airport_source(source)
        await migration.migrate(source, source_dsn, mode='apply', app='airport')
        report = await create_postgres_backup(source_dsn, path / 'airport.dump')
        self.assertTrue(report['archive_checked'])
        env = postgres_connection_env(target_dsn)
        # Empty --dbname asks libpq to resolve the target from PGDATABASE.
        await _run('pg_restore', '--dbname=', '--exit-on-error', '--no-owner', '--no-acl',
                   report['file'], env=env, timeout=60)
        self.assertTrue((await migration.migrate(source, target_dsn, mode='verify', app='airport'))['verified'])
        restored = await asyncpg.connect(target_dsn)
        try:
            self.assertEqual(await restored.fetchval("SELECT count(*) FROM batch_view"), 3)
            self.assertEqual(await restored.fetchval("SELECT status FROM received_media WHERE file_unique_id='pending'"), 'pending')
        finally:
            await restored.close()


if __name__ == '__main__':
    unittest.main()
