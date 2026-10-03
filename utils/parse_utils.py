class ParseUtils:
    @staticmethod
    def parse_user_ids(value) -> set[int]:
        """将 SharedConfig 中的用户 ID 列表规范化为整数集合。"""
        if isinstance(value, (list, tuple, set)):
            values = value
        else:
            values = str(value or "").split(",")

        user_ids: set[int] = set()
        for item in values:
            try:
                user_id = int(str(item).strip())
            except (TypeError, ValueError):
                continue
            if user_id > 0:
                user_ids.add(user_id)
        return user_ids
    
    @staticmethod
    def _parse_positive_user_id(raw: str) -> int | None:
        text = str(raw or "").strip()
        if not text.isdigit():
            return None
        user_id = int(text)
        return user_id if user_id > 0 else None