class EmojiUtils:
    _EMOJI_RANGES = (
        (0x2600, 0x27BF),
        (0x1F000, 0x1FAFF),
        (0x1F300, 0x1FAD6),
        (0x1F600, 0x1F64F),
        (0x1F680, 0x1F6FF),
        (0x1F900, 0x1F9FF),
        (0x1FA70, 0x1FAFF),
    )

    @classmethod
    def strip_consecutive_emojis(cls, text: str, count: int = 8) -> str:
        """去掉字符串中连续达到阈值的 emoji 段落。"""
        if not text:
            return text

        emoji_run = cls._extract_consecutive_emojis(text, count=count)
        if not emoji_run:
            return text

        return text.replace(emoji_run, "", 1).strip()

    @classmethod
    def _extract_consecutive_emojis(cls, text: str, count: int = 8) -> str | None:
        """提取第一段连续 Emoji 的前 ``count`` 个符号。"""
        if count <= 0:
            raise ValueError("count 必须大于 0")

        consecutive = 0
        sequence_start = 0
        index = 0
        while index < len(text):
            emoji_end = cls._consume_emoji(text, index)
            if emoji_end is None:
                consecutive = 0
                index += 1
                continue

            if consecutive == 0:
                sequence_start = index
            consecutive += 1
            if consecutive >= count:
                return text[sequence_start:emoji_end]
            index = emoji_end

        return None

    @classmethod
    def _consume_emoji(cls, text: str, index: int) -> int | None:
        """读取一个 Emoji（包括旗帜、肤色与 ZWJ 组合），返回结束位置。"""
        if index >= len(text):
            return None

        # 数字、#、* 加组合键帽符号组成一个 Emoji。
        if text[index] in "#*0123456789":
            end = index + 1
            if end < len(text) and ord(text[end]) == 0xFE0F:
                end += 1
            return end + 1 if end < len(text) and ord(text[end]) == 0x20E3 else None

        codepoint = ord(text[index])
        if 0x1F1E6 <= codepoint <= 0x1F1FF:
            next_index = index + 1
            if next_index < len(text) and 0x1F1E6 <= ord(text[next_index]) <= 0x1F1FF:
                return next_index + 1
            return None

        if not cls._is_emoji_base(codepoint):
            return None

        end = cls._consume_emoji_suffix(text, index + 1)
        while end < len(text) and ord(text[end]) == 0x200D:
            next_base = end + 1
            if next_base >= len(text) or not cls._is_emoji_base(ord(text[next_base])):
                break
            end = cls._consume_emoji_suffix(text, next_base + 1)
        return end

    @classmethod
    def _is_emoji_base(cls, codepoint: int) -> bool:
        if 0x1F3FB <= codepoint <= 0x1F3FF:
            return False
        return any(start <= codepoint <= end for start, end in cls._EMOJI_RANGES)

    @staticmethod
    def _consume_emoji_suffix(text: str, index: int) -> int:
        if index < len(text) and ord(text[index]) == 0xFE0F:
            index += 1
        if index < len(text) and 0x1F3FB <= ord(text[index]) <= 0x1F3FF:
            index += 1
        return index