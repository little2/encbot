import sqlite3
import time
from pathlib import Path


class BatchStore:

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS batch (
                batch_id TEXT PRIMARY KEY,
                channel_chat_id INTEGER NOT NULL,
                channel_message_id INTEGER NOT NULL,
                discussion_chat_id INTEGER,
                discussion_message_id INTEGER,
                batch_content TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(channel_chat_id, channel_message_id)
            )
        """)
        columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(batch)")
        }
        if "batch_content" not in columns:
            self.connection.execute(
                "ALTER TABLE batch ADD COLUMN batch_content TEXT"
            )
        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_batch_channel_message
            ON batch(channel_chat_id, channel_message_id)
        """)
        self.connection.commit()

    def upsert_channel_location(
        self,
        batch_id: str,
        channel_chat_id: int,
        channel_message_id: int,
        batch_content: str = "",
    ) -> None:
        normalized_batch_id = str(batch_id or "").strip()
        if not normalized_batch_id:
            raise ValueError("batch_id is required")

        now = int(time.time())
        with self.connection:
            self.connection.execute(
                """
                    INSERT INTO batch (
                        batch_id,
                        channel_chat_id,
                        channel_message_id,
                        discussion_chat_id,
                        discussion_message_id,
                        batch_content,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, NULL, NULL, ?, ?, ?)
                    ON CONFLICT(batch_id) DO UPDATE SET
                        discussion_chat_id = CASE
                            WHEN batch.channel_chat_id = excluded.channel_chat_id
                             AND batch.channel_message_id = excluded.channel_message_id
                            THEN batch.discussion_chat_id
                            ELSE NULL
                        END,
                        discussion_message_id = CASE
                            WHEN batch.channel_chat_id = excluded.channel_chat_id
                             AND batch.channel_message_id = excluded.channel_message_id
                            THEN batch.discussion_message_id
                            ELSE NULL
                        END,
                        channel_chat_id = excluded.channel_chat_id,
                        channel_message_id = excluded.channel_message_id,
                        batch_content = excluded.batch_content,
                        updated_at = excluded.updated_at
                """,
                (
                    normalized_batch_id,
                    int(channel_chat_id),
                    int(channel_message_id),
                    str(batch_content or "").strip(),
                    now,
                    now,
                ),
            )

    def update_discussion_location(
        self,
        channel_chat_id: int,
        channel_message_id: int,
        discussion_chat_id: int,
        discussion_message_id: int,
    ) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """
                    UPDATE batch
                    SET
                        discussion_chat_id = ?,
                        discussion_message_id = ?,
                        updated_at = ?
                    WHERE channel_chat_id = ?
                      AND channel_message_id = ?
                """,
                (
                    int(discussion_chat_id),
                    int(discussion_message_id),
                    int(time.time()),
                    int(channel_chat_id),
                    int(channel_message_id),
                ),
            )
        return cursor.rowcount > 0

    def get(self, batch_id: str) -> dict[str, int | str | None] | None:
        row = self.connection.execute(
            """
                SELECT
                    batch_id,
                    channel_chat_id,
                    channel_message_id,
                    discussion_chat_id,
                    discussion_message_id,
                    batch_content,
                    created_at,
                    updated_at
                FROM batch
                WHERE batch_id = ?
            """,
            (str(batch_id or "").strip(),),
        ).fetchone()
        if row is None:
            return None
        return {
            "batch_id": str(row[0]),
            "channel_chat_id": int(row[1]),
            "channel_message_id": int(row[2]),
            "discussion_chat_id": int(row[3]) if row[3] is not None else None,
            "discussion_message_id": int(row[4]) if row[4] is not None else None,
            "batch_content": str(row[5] or ""),
            "created_at": int(row[6]),
            "updated_at": int(row[7]),
        }

    def get_discussion_location(
        self,
        channel_chat_id: int,
        channel_message_id: int,
    ) -> tuple[int | None, int | None] | None:
        row = self.connection.execute(
            """
                SELECT discussion_chat_id, discussion_message_id
                FROM batch
                WHERE channel_chat_id = ?
                  AND channel_message_id = ?
            """,
            (int(channel_chat_id), int(channel_message_id)),
        ).fetchone()
        if row is None:
            return None
        return (
            int(row[0]) if row[0] is not None else None,
            int(row[1]) if row[1] is not None else None,
        )

    def close(self) -> None:
        self.connection.close()
