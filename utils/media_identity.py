"""Decode the stable media identity embedded in a Telegram Bot API file ID."""
from __future__ import annotations

import base64
import logging
import struct


_WEB_LOCATION_FLAG = 1 << 24
_FILE_REFERENCE_FLAG = 1 << 25
_PHOTO_TYPES = {0, 1, 2}
_DOCUMENT_TYPES = {3, 4, 5, 8, 9, 10, 13, 17}


def _rle_decode(data: bytes) -> bytes:
    decoded = bytearray()
    index = 0
    while index < len(data):
        value = data[index]
        index += 1
        if value:
            decoded.append(value)
            continue
        if index >= len(data):
            raise ValueError("Invalid Telegram file_id RLE data")
        decoded.extend(b"\x00" * data[index])
        index += 1
    return bytes(decoded)


def _skip_tl_bytes(data: bytes, offset: int) -> int:
    if offset >= len(data):
        raise ValueError("Missing Telegram file reference")
    first = data[offset]
    if first < 254:
        length = first
        header_length = 1
    elif first == 254 and offset + 4 <= len(data):
        length = int.from_bytes(data[offset + 1:offset + 4], "little")
        header_length = 4
    else:
        raise ValueError("Invalid Telegram file reference length")

    end = offset + header_length + length
    padded_end = end + (-end % 4)
    if padded_end > len(data):
        raise ValueError("Truncated Telegram file reference")
    return padded_end


def _decode_media_identity(file_id: str) -> tuple[str, int, int, int]:
    encoded = str(file_id or "").strip()
    if not encoded:
        raise ValueError("file_id is empty")
    compressed = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    decoded = _rle_decode(compressed)
    if len(decoded) < 10:
        raise ValueError("Telegram file_id is too short")

    major = decoded[-1]
    if major >= 4:
        minor = decoded[-2]
        payload = decoded[:-2]
    else:
        minor = 0
        payload = decoded[:-1]
    if major not in {2, 4}:
        raise ValueError(f"Unsupported Telegram file_id major version: {major}")

    raw_type, _dc_id = struct.unpack_from("<II", payload, 0)
    has_web_location = bool(raw_type & _WEB_LOCATION_FLAG)
    has_file_reference = bool(raw_type & _FILE_REFERENCE_FLAG)
    type_id = raw_type & ~(_WEB_LOCATION_FLAG | _FILE_REFERENCE_FLAG)
    if has_web_location:
        raise ValueError("Web-location file IDs do not contain a media identity")

    offset = 8
    if has_file_reference:
        offset = _skip_tl_bytes(payload, offset)
    if offset + 8 > len(payload):
        raise ValueError("Telegram file_id has no media ID")
    media_id = struct.unpack_from("<q", payload, offset)[0]

    if type_id in _PHOTO_TYPES:
        media_type = "photo"
    elif type_id in _DOCUMENT_TYPES:
        media_type = "document"
    else:
        raise ValueError(f"Unsupported Telegram media type: {type_id}")
    return media_type, int(media_id), int(major), int(minor)


def media_identity(file_id: str, bot_id: int | None = None) -> dict:
    try:
        media_type, media_id, _major, _minor = _decode_media_identity(file_id)
        return {"media_type": media_type, "media_id": media_id, "bot_id": bot_id}
    except Exception:
        logging.getLogger(__name__).exception("Cannot decode Telegram media identity")
        return {}
