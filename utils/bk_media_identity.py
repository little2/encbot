"""Decode Telegram Bot API file IDs through Pyrogram when their format is supported."""
from __future__ import annotations

import logging

from pyrogram.file_id import DOCUMENT_TYPES, PHOTO_TYPES, FileId


def media_identity(file_id: str, bot_id: int | None = None) -> dict:
	"""Return a media identity, or an empty mapping when Pyrogram cannot decode it."""
	try:
		decoded = FileId.decode(str(file_id or "").strip())
		if decoded.file_type in PHOTO_TYPES:
			media_type = "photo"
		elif decoded.file_type in DOCUMENT_TYPES:
			media_type = "document"
		else:
			raise ValueError(f"Unsupported Pyrogram file type: {decoded.file_type}")
		if decoded.media_id is None:
			raise ValueError("Pyrogram file ID has no media_id")
		return {
			"media_type": media_type,
			"media_id": int(decoded.media_id),
			"bot_id": bot_id,
		}
	except Exception as exc:
		logging.getLogger(__name__).warning(
			"Cannot decode Telegram media identity with Pyrogram: %s", exc,
		)
		return {}
