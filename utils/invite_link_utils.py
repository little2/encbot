import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SharedInviteLink:
    link_key: str
    chat_id: int
    invite_link: str
    name: str
    created_at: int
    validated_at: int


class SharedInviteLinkStore:

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS shared_invite_link (
                link_key TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                invite_link TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                validated_at INTEGER NOT NULL
            )
        """)
        self.connection.commit()

    def get(self, link_key: str) -> SharedInviteLink | None:
        row = self.connection.execute("""
            SELECT
                link_key,
                chat_id,
                invite_link,
                name,
                created_at,
                validated_at
            FROM shared_invite_link
            WHERE link_key = ?
        """, (str(link_key),)).fetchone()
        if not row:
            return None
        return SharedInviteLink(
            link_key=str(row[0]),
            chat_id=int(row[1]),
            invite_link=str(row[2]),
            name=str(row[3]),
            created_at=int(row[4]),
            validated_at=int(row[5]),
        )

    def save(
        self,
        link_key: str,
        chat_id: int,
        invite_link: str,
        name: str,
        created_at: int | None = None,
    ) -> SharedInviteLink:
        now = int(time.time())
        created_at = int(created_at or now)
        with self.connection:
            self.connection.execute("""
                INSERT INTO shared_invite_link (
                    link_key,
                    chat_id,
                    invite_link,
                    name,
                    created_at,
                    validated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(link_key) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    invite_link = excluded.invite_link,
                    name = excluded.name,
                    created_at = excluded.created_at,
                    validated_at = excluded.validated_at
            """, (
                str(link_key),
                int(chat_id),
                str(invite_link),
                str(name),
                created_at,
                now,
            ))
        return self.get(link_key)  # type: ignore[return-value]

    def mark_validated(self, link_key: str) -> None:
        with self.connection:
            self.connection.execute("""
                UPDATE shared_invite_link
                SET validated_at = ?
                WHERE link_key = ?
            """, (int(time.time()), str(link_key)))

    def delete(self, link_key: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM shared_invite_link WHERE link_key = ?",
                (str(link_key),),
            )
        return cursor.rowcount > 0

    def close(self) -> None:
        self.connection.close()
