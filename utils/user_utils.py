import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from config import MEDIA_UPLOAD_EXTEND_MINUTES, MEDIA_VIEW_COST_MINUTES, MESSAGE_EXTEND_MINUTES, MAX_VALID_DURATION_MINUTES


@dataclass(slots=True)
class UserExpire:
    expire_timestamp: int
    update_timestamp: int


class UserExpireCache:

    def __init__(self, db_path: str | Path):
        self.users: dict[int, UserExpire] = {}
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS user_expire (
                user_id INTEGER PRIMARY KEY,
                expire_timestamp INTEGER NOT NULL,
                update_timestamp INTEGER NOT NULL
            )
        """)
        self.connection.commit()
        self._load()

    def _load(self) -> None:
        rows = self.connection.execute("""
            SELECT user_id, expire_timestamp, update_timestamp
            FROM user_expire
        """).fetchall()
        self.users = {
            int(user_id): UserExpire(
                expire_timestamp=int(expire_timestamp),
                update_timestamp=int(update_timestamp),
            )
            for user_id, expire_timestamp, update_timestamp in rows
        }

    def _save(self, user_id: int) -> None:
        user = self.users[user_id]
        with self.connection:
            self.connection.execute("""
                INSERT INTO user_expire (
                    user_id,
                    expire_timestamp,
                    update_timestamp
                )
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    expire_timestamp = excluded.expire_timestamp,
                    update_timestamp = excluded.update_timestamp
            """, (
                user_id,
                user.expire_timestamp,
                user.update_timestamp,
            ))

    def get(self, user_id: int) -> UserExpire | None:
        return self.users.get(user_id)

    def update(self, user_id: int, expire_timestamp: int) -> UserExpire:
        now = int(time.time())
        user = self.users.get(user_id)
        previous_values = (
            (user.expire_timestamp, user.update_timestamp)
            if user
            else None
        )

        if user:
            user.expire_timestamp = expire_timestamp
            user.update_timestamp = now
        else:
            user = UserExpire(
                expire_timestamp=expire_timestamp,
                update_timestamp=now,
            )
            self.users[user_id] = user

        try:
            self._save(user_id)
        except Exception:
            if previous_values is None:
                self.users.pop(user_id, None)
            else:
                user.expire_timestamp, user.update_timestamp = previous_values
            raise

        return user

    def remove(self, user_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM user_expire WHERE user_id = ?",
                (user_id,),
            )
        self.users.pop(user_id, None)

    def is_valid(self, user_id: int) -> bool:
        user = self.users.get(user_id)

        if not user:
            return False

        return user.expire_timestamp > int(time.time())

    def count(self) -> int:
        return len(self.users)


    def extend_minutes(self, user_id: int, minutes: int) -> UserExpire:
        now = int(time.time())
        user = self.users.get(user_id)

        base_timestamp = max(
            now,
            user.expire_timestamp if user else 0,
        )
        wanted_expire_timestamp = base_timestamp + max(0, minutes) * 60
        max_expire_timestamp = now + MAX_VALID_DURATION_MINUTES * 60
        expire_timestamp = max(
            base_timestamp,
            min(wanted_expire_timestamp, max_expire_timestamp),
        )

        self.update(user_id, expire_timestamp)
        return self.users[user_id]

    def consume_minutes(self, user_id: int, minutes: int) -> UserExpire | None:
        now = int(time.time())
        user = self.users.get(user_id)
        consume_seconds = max(0, int(minutes)) * 60

        if not user or user.expire_timestamp - now < consume_seconds:
            return None

        previous_values = (user.expire_timestamp, user.update_timestamp)
        user.expire_timestamp -= consume_seconds
        user.update_timestamp = now

        try:
            self._save(user_id)
        except Exception:
            user.expire_timestamp, user.update_timestamp = previous_values
            raise

        return user

    def close(self) -> None:
        self.connection.close()
