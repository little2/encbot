import ast
import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, call


SOURCE = Path(__file__).resolve().parents[1] / "main_airport.py"


def load_function(name, namespace):
    tree = ast.parse(SOURCE.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"),
        namespace,
    )
    return namespace[name]


class AirportMediaForwardTests(unittest.TestCase):
    def test_takeoff_batch_id_accepts_supported_bot_names(self):
        extract = load_function(
            "_extract_takeoff_batch_id", {"re": __import__("re")}
        )
        batch_id = "Ab3_kL9-xY2pQr7Z"

        self.assertEqual(extract(f"zttower12bot_{batch_id}"), batch_id)
        self.assertEqual(extract(f"afu12bot_{batch_id}"), batch_id)
        self.assertEqual(extract(f"AFU12BOT_{batch_id}"), batch_id)

    def test_takeoff_batch_id_rejects_unsupported_bot_names(self):
        extract = load_function(
            "_extract_takeoff_batch_id", {"re": __import__("re")}
        )

        self.assertIsNone(extract("other12bot_Ab3_kL9-xY2pQr7Z"))

    def test_backup_bot_username_is_sent_as_telegram_chat_username(self):
        send_to_destination = AsyncMock(return_value=True)
        namespace = {
            "asyncio": asyncio,
            "MediaForwardTask": object,
            "APRON_CHANNEL_IDS": [],
            "X_MAN_BOT_ID": 123,
            "BACKUP_BOT_NAME": "zttowerpoolbot",
            "MEDIA_FORWARD_INTERVAL_SECONDS": 0,
            "_send_media_forward_to_destination": send_to_destination,
        }
        forward = load_function("_forward_media_in_background", namespace)
        task = object()

        asyncio.run(forward(task))

        self.assertEqual(
            send_to_destination.await_args_list,
            [
                call(task, "X_MAN_BOT_ID", 123),
                call(task, "BACKUP_BOT_NAME", "@zttowerpoolbot"),
            ],
        )
