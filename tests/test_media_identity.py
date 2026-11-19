import base64
import struct
import unittest

from utils.media_identity import media_identity


def _rle_encode(data: bytes) -> bytes:
    encoded = bytearray()
    zero_count = 0
    for value in data:
        if value == 0:
            zero_count += 1
            continue
        if zero_count:
            encoded.extend((0, zero_count))
            zero_count = 0
        encoded.append(value)
    if zero_count:
        encoded.extend((0, zero_count))
    return bytes(encoded)


def _tl_bytes(value: bytes) -> bytes:
    if len(value) >= 254:
        raise ValueError("test helper only supports short values")
    packed = bytes((len(value),)) + value
    return packed + b"\x00" * (-len(packed) % 4)


def _file_id(type_id: int, media_id: int, *, minor: int = 61) -> str:
    payload = (
        struct.pack("<II", type_id | (1 << 25), 5)
        + _tl_bytes(b"new-file-reference")
        + struct.pack("<qq", media_id, 987654321)
        + bytes((minor, 4))
    )
    return base64.urlsafe_b64encode(_rle_encode(payload)).decode().rstrip("=")


class MediaIdentityTests(unittest.TestCase):
    def test_decodes_document_with_new_file_id_subversion(self):
        self.assertEqual(
            media_identity(_file_id(4, 123456789), bot_id=42),
            {"media_type": "document", "media_id": 123456789, "bot_id": 42},
        )

    def test_decodes_photo_with_new_file_id_subversion(self):
        self.assertEqual(
            media_identity(_file_id(2, 987654321), bot_id=42),
            {"media_type": "photo", "media_id": 987654321, "bot_id": 42},
        )

    def test_invalid_file_id_returns_empty_identity(self):
        self.assertEqual(media_identity("not-a-file-id", bot_id=42), {})


if __name__ == "__main__":
    unittest.main()
