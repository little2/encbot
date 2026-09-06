import base64
import struct
from io import BytesIO


class TelegramFileId:
    FILE_REFERENCE_FLAG = 1 << 25
    WEB_LOCATION_FLAG = 1 << 24

    TYPE_NAMES = {
        0: "thumbnail",
        1: "profile_photo",
        2: "photo",
        3: "voice",
        4: "video",
        5: "document",
        8: "sticker",
        9: "audio",
        10: "animation",
        13: "video_note",
        16: "background",
    }

    DOCUMENT_TYPES = {
        3,   # voice
        4,   # video
        5,   # document
        8,   # sticker
        9,   # audio
        10,  # animation
        13,  # video_note
        16,  # background
    }

    UNIQUE_TYPE_DOCUMENT = 2

    @staticmethod
    def _base64url_decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)

    @staticmethod
    def _base64url_encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    @staticmethod
    def _rle_decode(data: bytes) -> bytes:
        result = bytearray()
        last = None

        for current in data:
            if last == 0:
                result.extend(b"\x00" * current)
                last = None
            else:
                if last is not None:
                    result.append(last)
                last = current

        if last is not None:
            result.append(last)

        return bytes(result)

    @staticmethod
    def _rle_encode(data: bytes) -> bytes:
        result = bytearray()
        zero_count = 0

        for current in data:
            if current == 0:
                zero_count += 1

                if zero_count == 255:
                    result.extend((0, 255))
                    zero_count = 0
            else:
                if zero_count:
                    result.extend((0, zero_count))
                    zero_count = 0

                result.append(current)

        if zero_count:
            result.extend((0, zero_count))

        return bytes(result)

    @staticmethod
    def _unpack_tl_string(buffer: BytesIO) -> bytes:
        first = buffer.read(1)

        if not first:
            raise ValueError("Invalid TL string")

        first_length = first[0]

        if first_length == 254:
            length_bytes = buffer.read(3)

            if len(length_bytes) != 3:
                raise ValueError("Invalid TL string length")

            length = struct.unpack(
                "<I",
                length_bytes + b"\x00"
            )[0]

            header_length = 4
        else:
            length = first_length
            header_length = 1

        value = buffer.read(length)

        if len(value) != length:
            raise ValueError("Incomplete TL string")

        padding = (-(header_length + length)) % 4

        if padding:
            buffer.read(padding)

        return value

    @classmethod
    def parse(cls, file_id: str) -> dict:
        """
        解析 Telegram Bot API file_id。

        当前主要支持 document-family：
        voice / video / document / sticker /
        audio / animation / video_note / background
        """

        raw = cls._base64url_decode(file_id)
        decoded = cls._rle_decode(raw)

        if len(decoded) < 2:
            raise ValueError("Invalid file_id")

        version = decoded[-1]

        if version == 4:
            sub_version = decoded[-2]
            payload = decoded[:-2]
        else:
            sub_version = 0
            payload = decoded[:-1]

        buffer = BytesIO(payload)

        raw_type_data = buffer.read(4)

        if len(raw_type_data) != 4:
            raise ValueError("Invalid file_id type")

        raw_type_id = struct.unpack("<I", raw_type_data)[0]

        has_file_reference = bool(
            raw_type_id & cls.FILE_REFERENCE_FLAG
        )

        is_web_location = bool(
            raw_type_id & cls.WEB_LOCATION_FLAG
        )

        type_id = raw_type_id
        type_id &= ~cls.FILE_REFERENCE_FLAG
        type_id &= ~cls.WEB_LOCATION_FLAG

        dc_data = buffer.read(4)

        if len(dc_data) != 4:
            raise ValueError("Invalid dc_id")

        dc_id = struct.unpack("<I", dc_data)[0]

        file_reference = None

        if has_file_reference:
            file_reference = cls._unpack_tl_string(buffer)

        result = {
            "type_id": type_id,
            "type": cls.TYPE_NAMES.get(
                type_id,
                f"unknown_{type_id}"
            ),
            "dc_id": dc_id,
            "version": version,
            "sub_version": sub_version,
            "has_file_reference": has_file_reference,
            "is_web_location": is_web_location,
            "file_reference": (
                file_reference.hex()
                if file_reference
                else None
            ),
        }

        if type_id in cls.DOCUMENT_TYPES:
            media_data = buffer.read(8)

            if len(media_data) != 8:
                raise ValueError("Invalid media_id")

            media_id = struct.unpack("<q", media_data)[0]

            result["media_id"] = media_id
            result["file_unique_id"] = (
                cls._document_unique_id(media_id)
            )

        else:
            result["file_unique_id"] = None

        return result

    @classmethod
    def _document_unique_id(
        cls,
        media_id: int
    ) -> str:
        unique_binary = struct.pack(
            "<lQ",
            cls.UNIQUE_TYPE_DOCUMENT,
            media_id,
        )

        encoded = cls._rle_encode(unique_binary)

        return cls._base64url_encode(encoded)

    @classmethod
    def file_id_to_unique_id(
        cls,
        file_id: str
    ) -> str:
        info = cls.parse(file_id)

        unique_id = info.get("file_unique_id")

        if not unique_id:
            raise ValueError(
                f"Unsupported file type: "
                f"{info['type']} "
                f"(type_id={info['type_id']})"
            )

        return unique_id