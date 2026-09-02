import sqlite3
import time
from pathlib import Path


class BatchViewStore:

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS batch_view (
                batch_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                PRIMARY KEY (batch_id, user_id)
            )
        """)
        self.connection.commit()

    def record(self, batch_id: str, user_id: int) -> bool:
        normalized_batch_id = str(batch_id or "").strip()
        if not normalized_batch_id:
            raise ValueError("batch_id is required")

        normalized_user_id = int(user_id)
        if normalized_user_id <= 0:
            raise ValueError("user_id must be positive")

        with self.connection:
            cursor = self.connection.execute(
                """
                    INSERT OR IGNORE INTO batch_view (
                        batch_id,
                        user_id,
                        timestamp
                    )
                    VALUES (?, ?, ?)
                """,
                (normalized_batch_id, normalized_user_id, int(time.time())),
            )
        return cursor.rowcount > 0

    def get_hot_batches(
        self,
        days: int = 7,
        limit: int = 10,
    ) -> list[tuple[str, str, int]]:
        normalized_days = max(1, int(days))
        normalized_limit = min(100, max(1, int(limit)))
        cutoff_timestamp = int(time.time()) - normalized_days * 24 * 60 * 60
        rows = self.connection.execute(
            """
                SELECT
                    batch_view.batch_id,
                    COALESCE(batch.batch_content, ''),
                    COUNT(*) AS view_count
                FROM batch_view
                INNER JOIN batch
                    ON batch.batch_id = batch_view.batch_id
                WHERE batch_view.timestamp >= ?
                GROUP BY batch_view.batch_id, batch.batch_content
                ORDER BY view_count DESC, batch_view.batch_id ASC
                LIMIT ?
            """,
            (cutoff_timestamp, normalized_limit),
        ).fetchall()
        return [
            (str(batch_id), str(batch_content), int(view_count))
            for batch_id, batch_content, view_count in rows
        ]

    def close(self) -> None:
        self.connection.close()
