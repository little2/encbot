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
                batch_id TEXT,
                thumb_file_id TEXT,
                thumb_file_unique_id TEXT,
                thumb_phash TEXT,
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
        if "batch_id" not in columns:
            self.connection.execute(
                "ALTER TABLE received_media ADD COLUMN batch_id TEXT"
            )
        if "accepted_at" not in columns:
            self.connection.execute(
                "ALTER TABLE received_media ADD COLUMN accepted_at INTEGER"
            )
        if "thumb_file_id" not in columns:
            self.connection.execute(
                "ALTER TABLE received_media ADD COLUMN thumb_file_id TEXT"
            )
        if "thumb_file_unique_id" not in columns:
            self.connection.execute(
                "ALTER TABLE received_media ADD COLUMN thumb_file_unique_id TEXT"
            )
        if "thumb_phash" not in columns:
            self.connection.execute(
                "ALTER TABLE received_media ADD COLUMN thumb_phash TEXT"
            )
        self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_received_media_batch_id
            ON received_media(batch_id)
        """)
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

    def is_accepted(self, file_unique_id: str) -> bool:
        row = self.connection.execute("""
            SELECT 1
            FROM received_media
            WHERE file_unique_id = ? AND status = 'accepted'
            LIMIT 1
        """, (str(file_unique_id),)).fetchone()
        return row is not None

    def claim_batch(
        self,
        items: list[dict],
        user_id: int,
        batch_id: str,
    ) -> list[str]:
        normalized_batch_id = str(batch_id or "").strip()
        if not normalized_batch_id:
            raise ValueError("batch_id is required")
        if len(normalized_batch_id) > 64:
            raise ValueError("batch_id cannot exceed 64 characters")

        rows: list[tuple[
            str, str, str, int, int, int, str,
            str | None, str | None, str | None, int,
        ]] = []
        seen: set[str] = set()
        for item in items:
            file_unique_id = str(item.get("file_unique_id", "")).strip()
            if not file_unique_id or file_unique_id in seen:
                continue
            seen.add(file_unique_id)
            rows.append((
                file_unique_id,
                str(item.get("file_id", "")),
                str(item.get("file_type", "")),
                int(user_id),
                int(item.get("source_chat_id", 0)),
                int(item.get("source_message_id", 0)),
                normalized_batch_id,
                str(item.get("thumb_file_id", "") or "") or None,
                str(item.get("thumb_file_unique_id", "") or "") or None,
                str(item.get("thumb_phash", "") or "") or None,
                int(time.time()),
            ))
        if not rows:
            raise ValueError("media items are required")

        file_unique_ids = [row[0] for row in rows]
        placeholders = ",".join("?" for _ in file_unique_ids)
        with self.connection:
            # 旧版会在上传阶段留下 batch_id 为空的 pending 记录；这些记录
            # 不代表用户已经确认送出，可以由本次确认安全取代。
            self.connection.execute(
                f"""
                    DELETE FROM received_media
                    WHERE status = 'pending'
                      AND batch_id IS NULL
                      AND file_unique_id IN ({placeholders})
                """,
                tuple(file_unique_ids),
            )
            existing_rows = self.connection.execute(
                f"""
                    SELECT file_unique_id
                    FROM received_media
                    WHERE file_unique_id IN ({placeholders})
                """,
                tuple(file_unique_ids),
            ).fetchall()
            conflicts = [str(row[0]) for row in existing_rows]
            if conflicts:
                return conflicts

            self.connection.executemany("""
                INSERT INTO received_media (
                    file_unique_id,
                    file_id,
                    file_type,
                    first_user_id,
                    source_chat_id,
                    source_message_id,
                    status,
                    batch_id,
                    thumb_file_id,
                    thumb_file_unique_id,
                    thumb_phash,
                    created_at,
                    accepted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, NULL)
            """, rows)
        return []

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

    def release_pending_many(self, file_unique_ids: list[str]) -> int:
        unique_ids = list(dict.fromkeys(
            str(value).strip()
            for value in file_unique_ids
            if str(value).strip()
        ))
        if not unique_ids:
            return 0

        placeholders = ",".join("?" for _ in unique_ids)
        with self.connection:
            cursor = self.connection.execute(
                f"""
                    DELETE FROM received_media
                    WHERE status = 'pending'
                      AND file_unique_id IN ({placeholders})
                """,
                tuple(unique_ids),
            )
        return max(0, int(cursor.rowcount))

    def release_pending_batch(self, batch_id: str) -> int:
        normalized_batch_id = str(batch_id or "").strip()
        if not normalized_batch_id:
            return 0
        with self.connection:
            cursor = self.connection.execute("""
                DELETE FROM received_media
                WHERE status = 'pending' AND batch_id = ?
            """, (normalized_batch_id,))
        return max(0, int(cursor.rowcount))

    def accept_batch(
        self,
        file_unique_ids: list[str],
        batch_id: str,
    ) -> int:
        unique_ids = list(dict.fromkeys(
            str(value).strip()
            for value in file_unique_ids
            if str(value).strip()
        ))
        if not unique_ids:
            return 0
        normalized_batch_id = str(batch_id or "").strip()
        if not normalized_batch_id:
            raise ValueError("batch_id is required")
        if len(normalized_batch_id) > 64:
            raise ValueError("batch_id cannot exceed 64 characters")

        accepted_at = int(time.time())
        placeholders = ",".join("?" for _ in unique_ids)
        with self.connection:
            cursor = self.connection.execute(
                f"""
                    UPDATE received_media
                    SET
                        status = 'accepted',
                        accepted_at = ?,
                        batch_id = ?
                    WHERE status = 'pending'
                      AND batch_id = ?
                      AND file_unique_id IN ({placeholders})
                """,
                (accepted_at, normalized_batch_id, normalized_batch_id, *unique_ids),
            )
        return max(0, int(cursor.rowcount))

    def get_batch_ids(
        self,
        file_unique_ids: list[str],
    ) -> dict[str, str | None]:
        unique_ids = list(dict.fromkeys(
            str(value).strip()
            for value in file_unique_ids
            if str(value).strip()
        ))
        if not unique_ids:
            return {}

        placeholders = ",".join("?" for _ in unique_ids)
        rows = self.connection.execute(
            f"""
                SELECT file_unique_id, batch_id
                FROM received_media
                WHERE file_unique_id IN ({placeholders})
            """,
            tuple(unique_ids),
        ).fetchall()
        return {
            str(file_unique_id): (
                str(batch_id) if batch_id is not None else None
            )
            for file_unique_id, batch_id in rows
        }

    def get_media_by_batch_id(self, batch_id: str) -> list[dict[str, str]]:
        normalized_batch_id = str(batch_id or "").strip()
        if not normalized_batch_id:
            return []

        rows = self.connection.execute(
            """
                SELECT file_id, file_type
                FROM received_media
                WHERE batch_id = ?
                  AND status = 'accepted'
                  AND file_id <> ''
                ORDER BY source_message_id, file_unique_id
            """,
            (normalized_batch_id,),
        ).fetchall()
        return [
            {
                "file_id": str(file_id),
                "file_type": str(file_type),
            }
            for file_id, file_type in rows
        ]

    def close(self) -> None:
        self.connection.close()
