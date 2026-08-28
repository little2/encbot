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
    expires_at: int = 0


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
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL DEFAULT 0
            )
        """)
        columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(user_blacklist)")
        }
        if "expires_at" not in columns:
            self.connection.execute(
                "ALTER TABLE user_blacklist "
                "ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.commit()
        self.user_ids: set[int] = set()
        self.expires_at_by_user: dict[int, int] = {}
        self._load()

    def _load(self) -> None:
        columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(user_blacklist)")
        }
        if "expires_at" not in columns:
            with self.connection:
                self.connection.execute(
                    "ALTER TABLE user_blacklist "
                    "ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0"
                )
        now = int(time.time())
        with self.connection:
            self.connection.execute(
                "DELETE FROM user_blacklist WHERE expires_at > 0 AND expires_at <= ?",
                (now,),
            )
        rows = self.connection.execute(
            "SELECT user_id, expires_at FROM user_blacklist"
        ).fetchall()
        self.user_ids = {int(row[0]) for row in rows}
        self.expires_at_by_user = {
            int(row[0]): int(row[1] or 0)
            for row in rows
        }

    def is_blocked(self, user_id: int) -> bool:
        user_id = int(user_id)
        if user_id not in self.user_ids:
            return False
        expires_at = self.expires_at_by_user.get(user_id, 0)
        if expires_at > 0 and expires_at <= int(time.time()):
            self.unban(user_id)
            return False
        return True

    def ban(
        self,
        user_id: int,
        reason: str,
        created_by: int,
        expires_at: int = 0,
    ) -> BlacklistEntry:
        entry = BlacklistEntry(
            user_id=int(user_id),
            reason=str(reason).strip(),
            created_by=int(created_by),
            created_at=int(time.time()),
            expires_at=max(0, int(expires_at)),
        )
        with self.connection:
            self.connection.execute("""
                INSERT INTO user_blacklist (
                    user_id,
                    reason,
                    created_by,
                    created_at,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    reason = excluded.reason,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
            """, (
                entry.user_id,
                entry.reason,
                entry.created_by,
                entry.created_at,
                entry.expires_at,
            ))
        self.user_ids.add(entry.user_id)
        self.expires_at_by_user[entry.user_id] = entry.expires_at
        return entry

    def unban(self, user_id: int) -> bool:
        user_id = int(user_id)
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM user_blacklist WHERE user_id = ?",
                (user_id,),
            )
        self.user_ids.discard(user_id)
        self.expires_at_by_user.pop(user_id, None)
        return cursor.rowcount > 0

    def get(self, user_id: int) -> BlacklistEntry | None:
        if not self.is_blocked(user_id):
            return None
        row = self.connection.execute("""
            SELECT user_id, reason, created_by, created_at, expires_at
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
            expires_at=int(row[4] or 0),
        )

    def list_page(
        self,
        page: int = 1,
        page_size: int = 10,
    ) -> tuple[list[BlacklistEntry], int]:
        page = max(1, int(page))
        page_size = max(1, int(page_size))
        self._load()
        total = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM user_blacklist"
            ).fetchone()[0]
        )
        rows = self.connection.execute("""
            SELECT user_id, reason, created_by, created_at, expires_at
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
                expires_at=int(row[4] or 0),
            )
            for row in rows
        ]
        return entries, total

    def list_by_reason_prefix(self, reason_prefix: str) -> list[BlacklistEntry]:
        self._load()
        prefix = str(reason_prefix).strip()
        if not prefix:
            return []
        rows = self.connection.execute("""
            SELECT user_id, reason, created_by, created_at, expires_at
            FROM user_blacklist
            WHERE reason LIKE ?
            ORDER BY created_at ASC, user_id ASC
        """, (f"{prefix}%",)).fetchall()
        return [
            BlacklistEntry(
                user_id=int(row[0]),
                reason=str(row[1]),
                created_by=int(row[2]),
                created_at=int(row[3]),
                expires_at=int(row[4] or 0),
            )
            for row in rows
        ]

    def close(self) -> None:
        self.connection.close()
