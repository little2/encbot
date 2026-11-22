import base64
import struct
import unittest

from utils.bk_media_identity import media_identity
from tgfileid import TelegramFileId


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
    photo_fields = b""
    if type_id in {0, 1, 2}:
        # Pyrogram's photo file-ID layout includes a volume and thumbnail source.
        photo_fields = (
            struct.pack("<q", 123456789)
            + struct.pack("<i", 0)  # ThumbnailSource.LEGACY
            + struct.pack("<qi", 987654321, 123)
        )
    payload = (
        struct.pack("<II", type_id | (1 << 25), 5)
        + _tl_bytes(b"new-file-reference")
        + struct.pack("<qq", media_id, 987654321)
        + photo_fields
        + bytes((minor, 4))
    )
    return base64.urlsafe_b64encode(_rle_encode(payload)).decode().rstrip("=")


class MediaIdentityTests(unittest.TestCase):
    def test_photo_recovery_key_preserves_size_and_legacy_location(self):
        legacy_key = struct.pack("<iqi", 1, 60, 7)
        cases = [
            (0, struct.pack("<qqi", 60, 654, 7), legacy_key),
            (23, struct.pack("<qiqi", 60, 0, 654, 7), legacy_key),
            (61, struct.pack("<iqqi", 5, 60, 654, 7), legacy_key),
        ]
        for size, suffix in (("a", 0), ("c", 1), ("x", ord("x") + 5)):
            key = struct.pack("<iqB", 1, 42, suffix)
            thumb = struct.pack("<iii", 1, 2, ord(size))
            cases.append((61, thumb, key))
            cases.append((23, struct.pack("<q", 60) + thumb + struct.pack("<i", 7), key))
        for version, location, expected_key in cases:
            with self.subTest(version=version, location=location):
                header = struct.pack("<II", 2 | (1 << 25), 2) + _tl_bytes(b"reference")
                trailer = bytes((version, 4)) if version else bytes((3,))
                raw = header + struct.pack("<qq", 42, 123) + location + trailer
                file_id = base64.urlsafe_b64encode(_rle_encode(raw)).decode().rstrip("=")
                expected = base64.urlsafe_b64encode(_rle_encode(expected_key)).decode().rstrip("=")
                self.assertEqual(TelegramFileId.file_id_to_unique_id(file_id), expected)

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
