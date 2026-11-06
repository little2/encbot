import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from utils.batch_utils import BatchStore as LegacyBatchStore
from utils.batch_view_utils import BatchViewStore as LegacyBatchViewStore
from utils.invite_link_utils import SharedInviteLinkStore as LegacyInviteLinkStore
from utils.received_media_utils import ReceivedMediaStore as LegacyReceivedMediaStore
from utils.airport_async_stores import (
    BatchStore,
    BatchViewStore,
    ReceivedMediaStore,
    SharedInviteLinkStore,
    create_airport_stores,
)


NOW = 1_700_000_000
TEST_POSTGRES_DSN = os.getenv("ASYNC_STORES_TEST_POSTGRES_DSN", "")


def media(unique_id, message_id, *, file_id=None):
    return {
        "file_unique_id": unique_id,
        "file_id": file_id or f"file-{unique_id}",
        "file_type": "video",
        "duration": 12,
        "source_chat_id": -1001,
        "source_message_id": message_id,
        "thumb_file_id": f"thumb-{unique_id}",
        "thumb_file_unique_id": f"thumb-unique-{unique_id}",
        "thumb_phash": "abcd",
    }


class AirportStoreContract:
    async def test_exposes_all_six_store_contracts(self):
        self.assertIsInstance(self.stores.batches, BatchStore)
        self.assertIsInstance(self.stores.batch_views, BatchViewStore)
        self.assertIsInstance(self.stores.received_media, ReceivedMediaStore)
        self.assertIsInstance(self.stores.shared_invite_links, SharedInviteLinkStore)
        self.assertIsNone(await self.stores.users.get(1))
        self.assertFalse(await self.stores.blacklist.is_blocked(1))

    async def test_batch_upsert_and_discussion_location(self):
        batches = self.stores.batches
        await batches.upsert_channel_location(
            "batch-a", -100, 10, "first", "tag", 99,
            no_forward=True, if_spoiler=True, anonymous=False,
            flash_seconds=30, valid_until="20270101000000",
        )
        self.assertTrue(await batches.update_discussion_location(-100, 10, -200, 20))
        first = await batches.get("batch-a")
        self.assertEqual(first["discussion_chat_id"], -200)
        self.assertTrue(first["no_forward"])
        self.assertFalse(first["anonymous"])

        await batches.upsert_channel_location("batch-a", -100, 10, "updated")
        self.assertEqual(await batches.get_discussion_location(-100, 10), (-200, 20))
        await batches.upsert_channel_location("batch-a", -101, 11, "moved")
        self.assertEqual(await batches.get_discussion_location(-101, 11), (None, None))
        self.assertFalse(await batches.update_discussion_location(-999, 1, -200, 1))

    async def test_batch_count_by_uploader(self):
        batches = self.stores.batches
        await batches.upsert_channel_location("batch-a", -100, 1, uploader_user_id=42)
        await batches.upsert_channel_location("batch-b", -100, 2, uploader_user_id=42)
        await batches.upsert_channel_location("batch-c", -100, 3, uploader_user_id=7)

        self.assertEqual(await batches.count_by_uploader(42), 2)
        self.assertEqual(await batches.count_by_uploader(7), 1)
        self.assertEqual(await batches.count_by_uploader(99), 0)

    async def test_batch_views_are_unique_and_hot_query_joins_batch(self):
        await self.stores.batches.upsert_channel_location("popular", -100, 1, "Popular")
        await self.stores.batches.upsert_channel_location("other", -100, 2, "Other")
        self.assertTrue(await self.stores.batch_views.record("popular", 1))
        self.assertFalse(await self.stores.batch_views.record("popular", 1))
        self.assertTrue(await self.stores.batch_views.record("popular", 2))
        self.assertTrue(await self.stores.batch_views.record("other", 1))
        self.assertEqual(
            await self.stores.batch_views.get_hot_batches(days=7, limit=10),
            [("popular", "Popular", 2), ("other", "Other", 1)],
        )

    async def test_single_media_claim_updates_empty_reusable_id(self):
        store = self.stores.received_media
        self.assertTrue(await store.claim("one", "file-one", "video", 1, -100, 10))
        self.assertFalse(await store.claim("one", "new-file", "video", 2, -100, 11))
        self.assertFalse(await store.is_accepted("one"))
        self.assertTrue(await store.release_pending("one", -100, 10))
        self.assertFalse(await store.release_pending("one", -100, 10))

    async def test_batch_claim_conflict_rolls_back_all_new_rows(self):
        store = self.stores.received_media
        self.assertEqual(await store.claim_batch([media("taken", 1)], 1, "old"), [])
        conflicts = await store.claim_batch(
            [media("fresh", 2), media("taken", 3)], 2, "new"
        )
        self.assertEqual(conflicts, ["taken"])
        self.assertEqual(await store.get_batch_ids(["fresh", "taken"]), {"taken": "old"})

    async def test_concurrent_batch_claim_has_one_winner(self):
        store = self.stores.received_media
        results = await asyncio.gather(
            store.claim_batch([media("race", 1)], 1, "batch-a"),
            store.claim_batch([media("race", 1)], 2, "batch-b"),
        )
        self.assertEqual(sorted(results), [[], ["race"]])
        self.assertIn((await store.get_batch_ids(["race"]))["race"], {"batch-a", "batch-b"})

    async def test_accept_lookup_and_release_batch(self):
        store = self.stores.received_media
        self.assertEqual(
            await store.claim_batch([media("a", 2), media("b", 1)], 1, "batch"), []
        )
        self.assertEqual(await store.accept_batch(["a"], "batch"), 1)
        self.assertTrue(await store.is_accepted("a"))
        items = await store.get_media_by_batch_id("batch")
        self.assertEqual([item["file_unique_id"] for item in items], ["b", "a"])
        self.assertEqual(await store.release_pending_batch("batch"), 1)
        self.assertEqual(await store.get_batch_ids(["a", "b"]), {"a": "batch"})

    async def test_release_pending_many_and_stale_cleanup(self):
        store = self.stores.received_media
        await store.claim_batch([media("a", 1), media("b", 2)], 1, "batch")
        self.assertEqual(await store.release_pending_many(["a", "a", "missing"]), 1)
        async with self.stores._database.transaction(write=True) as conn:
            await conn.execute(
                "UPDATE received_media SET created_at = $1 WHERE file_unique_id = $2",
                NOW - store.PENDING_RETENTION_SECONDS - 1, "b",
            )
        self.assertEqual(await store.cleanup_stale_pending(), 1)

    async def test_shared_invite_link_lifecycle(self):
        links = self.stores.shared_invite_links
        self.assertIsNone(await links.get("airport"))
        saved = await links.save("airport", -100, "https://t.me/+one", "Airport", 123)
        self.assertEqual(saved.created_at, 123)
        self.assertEqual(saved.validated_at, NOW)
        updated = await links.set_chat_id("airport", -200)
        self.assertEqual(updated.chat_id, -200)
        self.assertEqual(updated.invite_link, "https://t.me/+one")
        await links.mark_validated("airport")
        self.assertTrue(await links.delete("airport"))
        self.assertFalse(await links.delete("airport"))

    async def test_closed_bundle_rejects_every_store(self):
        await self.stores.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await self.stores.batches.get("x")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await self.stores.received_media.is_accepted("x")


class SQLiteAirportStoreTests(AirportStoreContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = patch("utils.airport_async_stores.time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.path = Path(self.directory.name) / "airport.sqlite3"
        self.stores = await create_airport_stores(backend="sqlite", sqlite_path=self.path)
        self.addAsyncCleanup(self.stores.close)

    async def test_reopens_legacy_airport_schema_and_adds_optional_columns(self):
        await self.stores.close()
        legacy = Path(self.directory.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as conn, conn:
            conn.execute("CREATE TABLE batch (batch_id TEXT PRIMARY KEY, channel_chat_id INTEGER NOT NULL, channel_message_id INTEGER NOT NULL, discussion_chat_id INTEGER, discussion_message_id INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, UNIQUE(channel_chat_id, channel_message_id))")
            conn.execute("CREATE TABLE received_media (file_unique_id TEXT PRIMARY KEY, file_type TEXT NOT NULL, first_user_id INTEGER NOT NULL, source_chat_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL)")
        async with await create_airport_stores(backend="sqlite", sqlite_path=legacy) as stores:
            await stores.batches.upsert_channel_location("legacy", -100, 1, "works")
            self.assertEqual((await stores.batches.get("legacy"))["batch_content"], "works")
            self.assertTrue(await stores.received_media.claim("file", "id", "video", 1, -100, 1))

    async def test_restore_repairs_all_six_table_schemas(self):
        snapshot = Path(self.directory.name) / "snapshot.sqlite3"
        with closing(sqlite3.connect(snapshot)) as conn, conn:
            conn.execute("CREATE TABLE user_expire (user_id INTEGER PRIMARY KEY, expire_timestamp INTEGER NOT NULL, update_timestamp INTEGER NOT NULL)")
            conn.execute("INSERT INTO user_expire VALUES (1, ?, ?)", (NOW + 60, NOW))
            conn.execute("CREATE TABLE batch (batch_id TEXT PRIMARY KEY, channel_chat_id INTEGER NOT NULL, channel_message_id INTEGER NOT NULL, discussion_chat_id INTEGER, discussion_message_id INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, UNIQUE(channel_chat_id, channel_message_id))")
            conn.execute("CREATE TABLE received_media (file_unique_id TEXT PRIMARY KEY, file_type TEXT NOT NULL, first_user_id INTEGER NOT NULL, source_chat_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL)")
        async with self.stores.sqlite_restore_session() as restore:
            await restore(snapshot)
        self.assertEqual((await self.stores.users.get(1)).group_message_timestamp, 0)
        await self.stores.batches.upsert_channel_location("restored", -100, 1, "works")
        self.assertEqual((await self.stores.batches.get("restored"))["batch_content"], "works")
        self.assertTrue(await self.stores.received_media.claim("file", "id", "video", 1, -100, 1))

    async def test_async_sqlite_schema_remains_readable_by_legacy_stores(self):
        await self.stores.batches.upsert_channel_location("batch", -100, 1, "content")
        await self.stores.batch_views.record("batch", 1)
        await self.stores.received_media.claim_batch([media("file", 1)], 1, "batch")
        await self.stores.shared_invite_links.save("airport", -100, "link", "Airport")
        await self.stores.close()

        batches = LegacyBatchStore(self.path)
        views = LegacyBatchViewStore(self.path)
        received = LegacyReceivedMediaStore(self.path)
        links = LegacyInviteLinkStore(self.path)
        try:
            self.assertEqual(batches.get("batch")["batch_content"], "content")
            self.assertEqual(views.get_hot_batches(), [("batch", "content", 1)])
            self.assertEqual(received.get_batch_ids(["file"]), {"file": "batch"})
            self.assertEqual(links.get("airport").chat_id, -100)
        finally:
            batches.close()
            views.close()
            received.close()
            links.close()


@unittest.skipUnless(
    TEST_POSTGRES_DSN,
    "Set ASYNC_STORES_TEST_POSTGRES_DSN to run PostgreSQL airport contract tests",
)
class PostgresAirportStoreTests(AirportStoreContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg

        self.clock = patch("utils.airport_async_stores.time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        schema = "airport_stores_test_" + uuid4().hex
        admin = await asyncpg.connect(TEST_POSTGRES_DSN)
        self.addAsyncCleanup(admin.close)

        async def drop_schema():
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')

        await admin.execute(f'CREATE SCHEMA "{schema}"')
        self.addAsyncCleanup(drop_schema)
        url = urlsplit(TEST_POSTGRES_DSN)
        query = [(key, value) for key, value in parse_qsl(url.query) if key != "search_path"]
        query.append(("search_path", schema))
        dsn = urlunsplit(url._replace(query=urlencode(query)))
        self.stores = await create_airport_stores(
            backend="postgresql", database_url=dsn
        )
        self.addAsyncCleanup(self.stores.close)


if __name__ == "__main__":
    unittest.main()
