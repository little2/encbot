import time
from dataclasses import dataclass


@dataclass(slots=True)
class UserExpire:
    expire_timestamp: int
    update_timestamp: int


class UserExpireCache:

    def __init__(self):
        self.users: dict[int, UserExpire] = {}

    def get(self, user_id: int) -> UserExpire | None:
        return self.users.get(user_id)

    def update(self, user_id: int, expire_timestamp: int):
        now = int(time.time())

        if user_id in self.users:
            user = self.users[user_id]
            user.expire_timestamp = expire_timestamp
            user.update_timestamp = now
        else:
            self.users[user_id] = UserExpire(
                expire_timestamp=expire_timestamp,
                update_timestamp=now
            )

    def remove(self, user_id: int):
        self.users.pop(user_id, None)

    def is_valid(self, user_id: int) -> bool:
        user = self.users.get(user_id)

        if not user:
            return False

        return user.expire_timestamp > int(time.time())

    def count(self):
        return len(self.users)