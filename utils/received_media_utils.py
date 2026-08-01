import sqlite3
import time
from pathlib import Path


class ReceivedMediaStore:

    PENDING_RETENTION_SECONDS = 24 * 60 * 60

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS received_media (
                file_unique_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL DEFAULT '',
                file_type TEXT NOT NULL,
                first_user_id INTEGER NOT NULL,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'accepted')),
                created_at INTEGER NOT NULL,
                accepted_at INTEGER
            )
        """)
        columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(received_media)"
            ).fetchall()
        }
        if "file_id" not in columns:
            self.connection.execute(
                "ALTER TABLE received_media "
                "ADD COLUMN file_id TEXT NOT NULL DEFAULT ''"
            )
        self.connection.commit()
        self.cleanup_stale_pending()

    def cleanup_stale_pending(self) -> int:
        cutoff = int(time.time()) - self.PENDING_RETENTION_SECONDS
        with self.connection:
            cursor = self.connection.execute("""
                DELETE FROM received_media
                WHERE status = 'pending' AND created_at < ?
            """, (cutoff,))
        return max(0, int(cursor.rowcount))

    def claim(
        self,
        file_unique_id: str,
        file_id: str,
        file_type: str,
        user_id: int,
        source_chat_id: int,
        source_message_id: int,
    ) -> bool:
        unique_id = str(file_unique_id or "").strip()
        if not unique_id:
            raise ValueError("file_unique_id is required")
        reusable_file_id = str(file_id or "").strip()
        if not reusable_file_id:
            raise ValueError("file_id is required")

        with self.connection:
            cursor = self.connection.execute("""
                INSERT OR IGNORE INTO received_media (
                    file_unique_id,
                    file_id,
                    file_type,
                    first_user_id,
                    source_chat_id,
                    source_message_id,
                    status,
                    created_at,
                    accepted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
            """, (
                unique_id,
                str(file_id),
                str(file_type),
                int(user_id),
                int(source_chat_id),
                int(source_message_id),
                int(time.time()),
            ))
            if cursor.rowcount == 0:
                self.connection.execute("""
                    UPDATE received_media
                    SET file_id = ?
                    WHERE file_unique_id = ? AND file_id = ''
                """, (reusable_file_id, unique_id))
        return cursor.rowcount > 0

    def release_pending(
        self,
        file_unique_id: str,
        source_chat_id: int,
        source_message_id: int,
    ) -> bool:
        with self.connection:
            cursor = self.connection.execute("""
                DELETE FROM received_media
                WHERE file_unique_id = ?
                  AND source_chat_id = ?
                  AND source_message_id = ?
                  AND status = 'pending'
            """, (
                str(file_unique_id),
                int(source_chat_id),
                int(source_message_id),
            ))
        return cursor.rowcount > 0

    def mark_accepted_many(self, file_unique_ids: list[str]) -> int:
        unique_ids = list(dict.fromkeys(
            str(value).strip()
            for value in file_unique_ids
            if str(value).strip()
        ))
        if not unique_ids:
            return 0

        accepted_at = int(time.time())
        placeholders = ",".join("?" for _ in unique_ids)
        with self.connection:
            cursor = self.connection.execute(
                f"""
                    UPDATE received_media
                    SET status = 'accepted', accepted_at = ?
                    WHERE status = 'pending'
                      AND file_unique_id IN ({placeholders})
                """,
                (accepted_at, *unique_ids),
            )
        return max(0, int(cursor.rowcount))

    def close(self) -> None:
        self.connection.close()
