import sqlite3
import tempfile
from pathlib import Path
import unittest

from utils.received_media_utils import ReceivedMediaStore


class ReceivedMediaStoreDurationTests(unittest.TestCase):
    def test_claim_batch_preserves_video_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ReceivedMediaStore(Path(directory) / "received.sqlite3")
            try:
                conflicts = store.claim_batch(
                    [{
                        "file_unique_id": "unique-video",
                        "file_id": "video-file",
                        "file_type": "video",
                        "duration": 3723,
                        "source_chat_id": 100,
                        "source_message_id": 200,
                        "thumb_file_id": "thumb-file",
                        "thumb_file_unique_id": "unique-thumb",
                    }],
                    user_id=300,
                    batch_id="batch-duration",
                )

                self.assertEqual(conflicts, [])
                items = store.get_media_by_batch_id("batch-duration")
                self.assertEqual(items[0]["duration"], 3723)
            finally:
                store.close()

    def test_existing_database_gets_duration_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(db_path)
            connection.execute("""
                CREATE TABLE received_media (
                    file_unique_id TEXT PRIMARY KEY,
                    file_id TEXT NOT NULL DEFAULT '',
                    file_type TEXT NOT NULL,
                    first_user_id INTEGER NOT NULL,
                    source_chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    batch_id TEXT,
                    thumb_file_id TEXT,
                    thumb_file_unique_id TEXT,
                    thumb_phash TEXT,
                    created_at INTEGER NOT NULL,
                    accepted_at INTEGER
                )
            """)
            connection.commit()
            connection.close()

            store = ReceivedMediaStore(db_path)
            try:
                columns = {
                    row[1]
                    for row in store.connection.execute(
                        "PRAGMA table_info(received_media)"
                    ).fetchall()
                }
                self.assertIn("duration", columns)
            finally:
                store.close()
