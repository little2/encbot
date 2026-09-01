import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any


class VideoStore:

    ID_GENERATION_ATTEMPTS = 5

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS video_record (
                id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                file_type TEXT NOT NULL,
                uploader_id INTEGER NOT NULL,
                createtimestamp INTEGER NOT NULL,
                view_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_video_record_uploader
            ON video_record(uploader_id)
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS video_viewer (
                id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                createtimestamp INTEGER NOT NULL,
                PRIMARY KEY (id, user_id),
                FOREIGN KEY (id)
                    REFERENCES video_record(id)
                    ON DELETE CASCADE
            )
        """)
        self.connection.commit()

    def add_video(
        self,
        file_id: str,
        file_type: str,
        uploader_id: int,
    ) -> str:
        normalized_file_id = str(file_id or "").strip()
        if not normalized_file_id:
            raise ValueError("file_id is required")

        normalized_file_type = str(file_type or "").strip()
        if not normalized_file_type:
            raise ValueError("file_type is required")

        normalized_uploader_id = int(uploader_id)
        if normalized_uploader_id <= 0:
            raise ValueError("uploader_id must be positive")

        for _ in range(self.ID_GENERATION_ATTEMPTS):
            record_id = secrets.token_hex(8)
            try:
                with self.connection:
                    self.connection.execute(
                        """
                            INSERT INTO video_record (
                                id,
                                file_id,
                                file_type,
                                uploader_id,
                                createtimestamp,
                                view_count
                            )
                            VALUES (?, ?, ?, ?, ?, 0)
                        """,
                        (
                            record_id,
                            normalized_file_id,
                            normalized_file_type,
                            normalized_uploader_id,
                            int(time.time()),
                        ),
                    )
                return record_id
            except sqlite3.IntegrityError:
                continue

        raise RuntimeError("failed to generate a unique video id")

    def get_video(self, record_id: str) -> dict[str, int | str] | None:
        row = self.connection.execute(
            """
                SELECT
                    id,
                    file_id,
                    file_type,
                    uploader_id,
                    createtimestamp,
                    view_count
                FROM video_record
                WHERE id = ?
            """,
            (str(record_id or "").strip().lower(),),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "file_id": str(row[1]),
            "file_type": str(row[2]),
            "uploader_id": int(row[3]),
            "createtimestamp": int(row[4]),
            "view_count": int(row[5]),
        }

    def record_unique_view(self, record_id: str, user_id: int) -> int:
        normalized_record_id = str(record_id or "").strip().lower()
        normalized_user_id = int(user_id)
        if not normalized_record_id:
            raise ValueError("id is required")
        if normalized_user_id <= 0:
            raise ValueError("user_id must be positive")

        with self.connection:
            self.connection.execute(
                """
                    INSERT OR IGNORE INTO video_viewer (
                        id,
                        user_id,
                        createtimestamp
                    )
                    VALUES (?, ?, ?)
                """,
                (normalized_record_id, normalized_user_id, int(time.time())),
            )
            self.connection.execute(
                """
                    UPDATE video_record
                    SET view_count = (
                        SELECT COUNT(*)
                        FROM video_viewer
                        WHERE video_viewer.id = video_record.id
                    )
                    WHERE id = ?
                """,
                (normalized_record_id,),
            )
            row = self.connection.execute(
                "SELECT view_count FROM video_record WHERE id = ?",
                (normalized_record_id,),
            ).fetchone()

        if row is None:
            raise ValueError("video does not exist")
        return int(row[0])

    def get_top_videos(
        self,
        uploader_id: int,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        normalized_limit = min(100, max(1, int(limit)))
        rows = self.connection.execute(
            """
                SELECT id, view_count, createtimestamp
                FROM video_record
                WHERE uploader_id = ?
                ORDER BY view_count DESC, createtimestamp DESC
                LIMIT ?
            """,
            (int(uploader_id), normalized_limit),
        ).fetchall()
        return [
            {
                "id": str(record_id),
                "view_count": int(view_count),
                "createtimestamp": int(created_at),
            }
            for record_id, view_count, created_at in rows
        ]

    def get_total_view_count(self, uploader_id: int) -> int:
        row = self.connection.execute(
            """
                SELECT COALESCE(SUM(view_count), 0)
                FROM video_record
                WHERE uploader_id = ?
            """,
            (int(uploader_id),),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def get_recent_view_count(
        self,
        uploader_id: int,
        days: int = 7,
    ) -> int:
        normalized_days = max(1, int(days))
        cutoff_timestamp = int(time.time()) - normalized_days * 24 * 60 * 60
        row = self.connection.execute(
            """
                SELECT COUNT(*)
                FROM video_viewer
                INNER JOIN video_record
                    ON video_record.id = video_viewer.id
                WHERE video_record.uploader_id = ?
                  AND video_viewer.createtimestamp >= ?
            """,
            (int(uploader_id), cutoff_timestamp),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def close(self) -> None:
        self.connection.close()
