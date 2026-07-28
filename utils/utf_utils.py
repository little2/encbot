import base64


class UtfConverter:
    TG64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-;"

    CJK_START = 0x4E00
    CJK_END = 0x9FFF
    CJK_SIZE = CJK_END - CJK_START + 1

    @staticmethod
    def generate_nonce(length: int = 8) -> str:
        """
        生成指定長度的隨機數字字串（nonce）。
        """
        import random

        if length <= 0:
            raise ValueError("length must be positive")

        return "".join(str(random.randint(0, 9)) for _ in range(length))

    @classmethod
    def tg64_to_int(cls, s: str) -> int:
        table = {ch: i for i, ch in enumerate(cls.TG64_ALPHABET)}
        base = len(cls.TG64_ALPHABET)

        n = 0

        for ch in s:
            if ch not in table:
                raise ValueError(f"Invalid Telegram base64 character: {ch!r}")

            n = n * base + table[ch]

        return n

    @classmethod
    def int_to_tg64(cls, n: int, min_len: int = 1) -> str:
        if n < 0:
            raise ValueError("n cannot be negative")

        base = len(cls.TG64_ALPHABET)

        if n == 0:
            result = cls.TG64_ALPHABET[0]
        else:
            chars = []

            while n > 0:
                n, rem = divmod(n, base)
                chars.append(cls.TG64_ALPHABET[rem])

            result = "".join(reversed(chars))

        return result.rjust(min_len, cls.TG64_ALPHABET[0])

    @classmethod
    def int_to_unicode_cjk(cls, n: int) -> str:
        """
        大整数转中文。
        不用字表，直接使用 Unicode 编号顺序。
        每一位范围是 U+4E00 ~ U+9FFF。
        """
        if n < 0:
            raise ValueError("n cannot be negative")

        if n == 0:
            return chr(cls.CJK_START)

        chars = []

        while n > 0:
            n, rem = divmod(n, cls.CJK_SIZE)
            chars.append(chr(cls.CJK_START + rem))

        return "".join(reversed(chars))

    @classmethod
    def unicode_cjk_to_int(cls, text: str) -> int:
        """
        中文转回大整数。
        """
        n = 0

        for ch in text:
            code = ord(ch)

            if not (cls.CJK_START <= code <= cls.CJK_END):
                raise ValueError(f"Character out of CJK range: {ch!r}, U+{code:X}")

            digit = code - cls.CJK_START
            n = n * cls.CJK_SIZE + digit

        return n

    @classmethod
    def telegram_to_unicode_cjk(cls, file_id: str) -> str:
        """
        Telegram 64 字符串转 Unicode 中文。

        第一个字符记录原始 Telegram 字符串长度，
        避免前导 A 丢失。
        """
        n = cls.tg64_to_int(file_id)

        length_prefix = chr(cls.CJK_START + len(file_id))
        payload = cls.int_to_unicode_cjk(n)

        return length_prefix + payload

    @classmethod
    def unicode_cjk_to_telegram(cls, text: str) -> str:
        """
        Unicode 中文转回 Telegram 64 字符串。
        """
        if len(text) < 2:
            raise ValueError("encoded text is too short")

        length_char = text[0]
        payload = text[1:]

        original_len = ord(length_char) - cls.CJK_START

        if original_len < 0:
            raise ValueError("invalid length prefix")

        n = cls.unicode_cjk_to_int(payload)

        return cls.int_to_tg64(n, min_len=original_len)

    @classmethod
    def build_file_token(
        cls,
        user_id: int,
        file_id: str,
        file_type: str,
        no_forward: bool,
        flash_seconds: int,
        valid_until: str,
        nonce: str | None = None,
    ) -> str:
        """
        將檔案資訊拼接成一個以 ; 分隔的字符串。

        格式: nonce;user_id;file_id;file_type;no_forward;flash_seconds;valid_until

        valid_until 建議格式: YYYYMMDDHHMMSS
        """
        if nonce is None:
            nonce = cls.generate_nonce()

        if not valid_until or not str(valid_until).isdigit() or len(str(valid_until)) != 14:
            raise ValueError("valid_until must be a 14-digit string in YYYYMMDDHHMMSS format")

        parts = [
            str(nonce),
            str(user_id),
            file_id,
            file_type,
            "1" if no_forward else "0",
            str(flash_seconds),
            str(valid_until),
        ]

        return ";".join(parts)

    @classmethod
    def build_media_token(
        cls,
        user_id: int,
        items: list[dict],
        no_forward: bool,
        flash_seconds: int,
        valid_until: str,
        if_spoiler: bool = False,
        anonymous: bool = False,
        nonce: str | None = None,
    ) -> str:
        """建立最多包含 10 个媒体的 v5 token。"""
        if nonce is None:
            nonce = cls.generate_nonce()
        if not 1 <= len(items) <= 10:
            raise ValueError("items must contain between 1 and 10 media")
        if not valid_until or not str(valid_until).isdigit() or len(str(valid_until)) != 14:
            raise ValueError("valid_until must be a 14-digit string in YYYYMMDDHHMMSS format")

        parts = ["5", str(nonce), str(user_id), str(len(items))]
        for item in items:
            file_id = str(item.get("file_id", ""))
            file_type = str(item.get("file_type", ""))
            if not file_id or not file_type:
                raise ValueError("each media item must have file_id and file_type")
            file_size = max(0, int(item.get("file_size", 0) or 0))
            duration = max(0, int(item.get("duration", 0) or 0))
            file_name = str(item.get("file_name", "") or "")
            encoded_file_name = base64.urlsafe_b64encode(
                file_name.encode("utf-8")
            ).decode("ascii").rstrip("=")
            parts.extend([
                file_id,
                file_type,
                str(file_size),
                str(duration),
                encoded_file_name,
            ])

        parts.extend([
            "1" if no_forward else "0",
            str(flash_seconds),
            str(valid_until),
            "1" if if_spoiler else "0",
            "1" if anonymous else "0",
        ])
        return ";".join(parts)

    @classmethod
    def parse_file_token(cls, token: str) -> dict:
        """
        將 build_file_token 產生的字符串解析回各個欄位。

        格式: nonce;user_id;file_id;file_type;no_forward;flash_seconds;valid_until
        """
        parts = token.split(";")

        if len(parts) >= 9 and parts[0] in {"2", "3", "4", "5"}:
            version, nonce, user_id, count_text, *payload = parts
            if not count_text.isdigit():
                raise ValueError("Invalid media count")
            count = int(count_text)
            if not 1 <= count <= 10:
                raise ValueError("Invalid media count, expected 1 to 10")
            trailing_count = 5 if version == "5" else (4 if version in {"3", "4"} else 3)
            fields_per_item = 5 if version in {"4", "5"} else 2
            if len(parts) != 4 + count * fields_per_item + trailing_count:
                raise ValueError(f"Invalid v{version} token format")

            media_parts = payload[:count * fields_per_item]
            trailing_parts = payload[count * fields_per_item:]
            no_forward, flash_seconds, valid_until = trailing_parts[:3]
            if_spoiler = trailing_parts[3] == "1" if version in {"3", "4", "5"} else False
            anonymous = trailing_parts[4] == "1" if version == "5" else False
            if not valid_until.isdigit() or len(valid_until) != 14:
                raise ValueError("Invalid valid_until format, expected YYYYMMDDHHMMSS")

            if version in {"4", "5"}:
                items = []
                for index in range(0, len(media_parts), fields_per_item):
                    encoded_file_name = media_parts[index + 4]
                    padding = "=" * (-len(encoded_file_name) % 4)
                    file_name = base64.urlsafe_b64decode(
                        encoded_file_name + padding
                    ).decode("utf-8") if encoded_file_name else ""
                    items.append({
                        "file_id": media_parts[index],
                        "file_type": media_parts[index + 1],
                        "file_size": int(media_parts[index + 2] or 0),
                        "duration": int(media_parts[index + 3] or 0),
                        "file_name": file_name,
                    })
            else:
                items = [
                    {"file_id": media_parts[index], "file_type": media_parts[index + 1]}
                    for index in range(0, len(media_parts), fields_per_item)
                ]
            return {
                "user_id": int(user_id),
                "file_id": items[0]["file_id"],
                "file_type": items[0]["file_type"],
                "items": items,
                "no_forward": no_forward == "1",
                "flash_seconds": int(flash_seconds),
                "if_spoiler": if_spoiler,
                "anonymous": anonymous,
                "valid_until": valid_until,
                "nonce": nonce,
            }

        if len(parts) != 7:
            raise ValueError(f"Invalid token format, expected 7 parts, got {len(parts)}")

        nonce, user_id, file_id, file_type, no_forward, flash_seconds, valid_until = parts

        if not valid_until.isdigit() or len(valid_until) != 14:
            raise ValueError("Invalid valid_until format, expected YYYYMMDDHHMMSS")

        item = {"file_id": file_id, "file_type": file_type}
        return {
            "user_id": int(user_id),
            "file_id": file_id,
            "file_type": file_type,
            "items": [item],
            "no_forward": no_forward == "1",
            "flash_seconds": int(flash_seconds),
            "if_spoiler": False,
            "anonymous": False,
            "valid_until": valid_until,
            "nonce": nonce,
        }


if __name__ == "__main__":
    from datetime import datetime, timedelta

    file_unique_id = "BAACAgUAAx0Cd0bnWgACBKRoE-XMV3TiQ14Wsn9pXO3BcOPDCQACfgEAAkg4OFXzlA7pZcM7qjYE"
    valid_until = (datetime.now() + timedelta(minutes=24)).strftime("%Y%m%d%H%M%S")

    token = UtfConverter.build_file_token(
        user_id=123456789,
        file_id=file_unique_id,
        file_type="photo",
        no_forward=True,
        flash_seconds=5,
        valid_until=valid_until,
    )
    encoded = UtfConverter.telegram_to_unicode_cjk(token)
    decoded = UtfConverter.unicode_cjk_to_telegram(encoded)
    json_data = UtfConverter.parse_file_token(decoded)

    print("原始 Telegram 字符串:")
    print(token)

    print("\n转成 Unicode 中文:")
    print(encoded)

    print("\n转回 Telegram 字符串:")
    print(decoded)

    print("\n是否一致:")
    print(decoded == token)

    print("\n解析回各個欄位:")
    print(json_data)

    '''
    class 寫一個 function, 可以輸入 user_id, file_id, file_type, 是否禁止轉發, 閃照秒數, 可用時數
    最後拼接成一個字符串, 並用 ; 來分隔各個部分。
    
    '''
