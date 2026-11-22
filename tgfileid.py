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

        支持普通照片及 document-family 媒体的 file_unique_id。
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

        elif type_id == 2 and not is_web_location:
            result["file_unique_id"] = cls._photo_unique_id(buffer.read(), sub_version)
        else:
            result["file_unique_id"] = None

        return result

    @classmethod
    def _photo_unique_id(cls, data: bytes, sub_version: int) -> str:
        """Rebuild TDLib's size-specific unique ID for ordinary Telegram photos."""
        try:
            photo_id, _ = struct.unpack_from("<qq", data)
            offset = 16
            if sub_version < 32:  # Earlier locations also store volume_id and local_id.
                volume_id, = struct.unpack_from("<q", data, offset)
                offset += 8
            source = struct.unpack_from("<i", data, offset)[0] if sub_version >= 22 else 0
            if source == 1:  # Thumbnail source, including the individual sizes of a photo.
                _, size = struct.unpack_from("<ii", data, offset + 4)
                if not 0 <= size <= 127:
                    raise ValueError("Invalid Telegram photo size")
                size_key = {ord("a"): 0, ord("c"): 1}.get(size, size + 5)
                key = struct.pack("<qB", photo_id, size_key)
            elif source == 5:  # FullLegacy source carries its own volume and local IDs.
                volume_id, _, local_id = struct.unpack_from("<qqi", data, offset + 4)
                key = struct.pack("<qi", volume_id, local_id)
            elif source == 0 and sub_version < 32:
                secret_offset = offset + 4 if sub_version >= 22 else offset
                _, local_id = struct.unpack_from("<qi", data, secret_offset)
                key = struct.pack("<qi", volume_id, local_id)
            else:
                raise ValueError("Unsupported Telegram photo location")
        except struct.error as exc:
            raise ValueError("Invalid Telegram photo location") from exc
        return cls._base64url_encode(cls._rle_encode(struct.pack("<i", 1) + key))

    @classmethod
    def _document_unique_id(
        cls,
        media_id: int
    ) -> str:
        unique_binary = struct.pack(
            "<iq",
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
