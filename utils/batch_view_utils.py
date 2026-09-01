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

    def close(self) -> None:
        self.connection.close()
