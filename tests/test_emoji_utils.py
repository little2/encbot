import unittest

from utils.emoji_utils import EmojiUtils


class EmojiUtilsTests(unittest.TestCase):
    def test_strip_consecutive_emojis_removes_leading_run(self) -> None:
        text = "😀😀😀😀😀😀😀😀说明文字"
        self.assertEqual(EmojiUtils.strip_consecutive_emojis(text), "说明文字")

    def test_strip_consecutive_emojis_keeps_non_consecutive_text(self) -> None:
        text = "正常说明😀"
        self.assertEqual(EmojiUtils.strip_consecutive_emojis(text), "正常说明😀")


if __name__ == "__main__":
    unittest.main()
