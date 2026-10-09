import re
from collections import deque

PEACH_EXCHANGE_RECORDS: deque[tuple[int, int]] = deque(maxlen=1000)


def remember_peach_exchange_record(message_id: int, user_id: int) -> bool:
    """保存 message_id 和 user_id 的配对，保证内容唯一且最多保留 1000 笔。"""
    record = (int(message_id), int(user_id))
    if record in PEACH_EXCHANGE_RECORDS:
        return False

    if len(PEACH_EXCHANGE_RECORDS) >= PEACH_EXCHANGE_RECORDS.maxlen:
        PEACH_EXCHANGE_RECORDS.popleft()

    PEACH_EXCHANGE_RECORDS.append(record)
    return True


def has_peach_exchange_for_user(user_id: int, message_id: int | None = None) -> bool:
    target_user_id = int(user_id)
    if message_id is None:
        return any(stored_user_id == target_user_id for _, stored_user_id in PEACH_EXCHANGE_RECORDS)
    return any(
        stored_user_id == target_user_id and stored_message_id == int(message_id)
        for stored_message_id, stored_user_id in PEACH_EXCHANGE_RECORDS
    )


def remove_peach_exchange_for_user(user_id: int, message_id: int | None = None) -> bool:
    target_user_id = int(user_id)
    kept_records: list[tuple[int, int]] = []
    removed = False

    for stored_message_id, stored_user_id in PEACH_EXCHANGE_RECORDS:
        if message_id is None:
            match = stored_user_id == target_user_id
        else:
            match = stored_user_id == target_user_id and stored_message_id == int(message_id)
        if match:
            removed = True
            continue
        kept_records.append((stored_message_id, stored_user_id))

    if removed:
        PEACH_EXCHANGE_RECORDS.clear()
        PEACH_EXCHANGE_RECORDS.extend(kept_records)

    return removed


def bump_alert_button_text(button_text: str, callback_data: str) -> str:
    if callback_data == "alert:like":
        emoji = "👍"
    elif callback_data == "alert:dislike":
        emoji = "👎"
    else:
        return button_text

    match = re.search(r"(\d+)$", str(button_text).strip())
    current_count = int(match.group(1)) if match else 0
    return f"{emoji} {current_count + 1}"
