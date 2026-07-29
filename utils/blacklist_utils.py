import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BlacklistEntry:
    user_id: int
    reason: str
    created_by: int
    created_at: int


class BlacklistStore:

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS user_blacklist (
                user_id INTEGER PRIMARY KEY,
                reason TEXT NOT NULL DEFAULT '',
                created_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        self.connection.commit()
        self.user_ids: set[int] = set()
        self._load()

    def _load(self) -> None:
        rows = self.connection.execute(
            "SELECT user_id FROM user_blacklist"
        ).fetchall()
        self.user_ids = {int(row[0]) for row in rows}

    def is_blocked(self, user_id: int) -> bool:
        return int(user_id) in self.user_ids

    def ban(self, user_id: int, reason: str, created_by: int) -> BlacklistEntry:
        entry = BlacklistEntry(
            user_id=int(user_id),
            reason=str(reason).strip(),
            created_by=int(created_by),
            created_at=int(time.time()),
        )
        with self.connection:
            self.connection.execute("""
                INSERT INTO user_blacklist (
                    user_id,
                    reason,
                    created_by,
                    created_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    reason = excluded.reason,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at
            """, (
                entry.user_id,
                entry.reason,
                entry.created_by,
                entry.created_at,
            ))
        self.user_ids.add(entry.user_id)
        return entry

    def unban(self, user_id: int) -> bool:
        user_id = int(user_id)
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM user_blacklist WHERE user_id = ?",
                (user_id,),
            )
        self.user_ids.discard(user_id)
        return cursor.rowcount > 0

    def get(self, user_id: int) -> BlacklistEntry | None:
        row = self.connection.execute("""
            SELECT user_id, reason, created_by, created_at
            FROM user_blacklist
            WHERE user_id = ?
        """, (int(user_id),)).fetchone()
        if not row:
            return None
        return BlacklistEntry(
            user_id=int(row[0]),
            reason=str(row[1]),
            created_by=int(row[2]),
            created_at=int(row[3]),
        )

    def list_page(
        self,
        page: int = 1,
        page_size: int = 10,
    ) -> tuple[list[BlacklistEntry], int]:
        page = max(1, int(page))
        page_size = max(1, int(page_size))
        total = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM user_blacklist"
            ).fetchone()[0]
        )
        rows = self.connection.execute("""
            SELECT user_id, reason, created_by, created_at
            FROM user_blacklist
            ORDER BY created_at DESC, user_id ASC
            LIMIT ? OFFSET ?
        """, (page_size, (page - 1) * page_size)).fetchall()
        entries = [
            BlacklistEntry(
                user_id=int(row[0]),
                reason=str(row[1]),
                created_by=int(row[2]),
                created_at=int(row[3]),
            )
            for row in rows
        ]
        return entries, total

    def close(self) -> None:
        self.connection.close()
