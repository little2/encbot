"""Decode the underlying Telegram media identity without changing file IDs."""
import logging


def media_identity(file_id: str, bot_id: int | None = None) -> dict:
    from tg_file_id.file_id import FileId

    try:
        decoded = FileId.from_file_id(file_id)
        if decoded.type_generic not in {"document", "photo"}:
            raise ValueError("Unsupported media type")
        return {"media_type": decoded.type_generic, "media_id": int(decoded.id), "bot_id": bot_id}
    except Exception:
        logging.getLogger(__name__).exception("Cannot decode Telegram media identity")
        return {}
