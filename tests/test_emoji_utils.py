import unittest

from utils.emoji_utils import EmojiUtils


class EmojiUtilsTests(unittest.TestCase):
    def test_strip_leading_emoji_brackets_handles_combined_emojis(self):
        for text, expected in (
            ("[👒🎤🚋📢🌀🕞👁️🤼‍♂️][番外篇]网调", "[番外篇]网调"),
            ("[👩🏽‍💻🇨🇳1️⃣][番外篇]网调", "[番外篇]网调"),
            ("[🧟‍♀️🤪↖️🙍💊🌾🐡🤔]弟弟去叔叔家玩", "弟弟去叔叔家玩"),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    EmojiUtils.strip_leading_emoji_brackets(text),
                    expected,
                )

    def test_strip_leading_emoji_brackets_preserves_text_brackets(self):
        for text in ("[番外篇]网调", "[👁️番外篇]网调", "[]网调", "[👁️网调", "正文[👁️]", ""):
            with self.subTest(text=text):
                self.assertEqual(EmojiUtils.strip_leading_emoji_brackets(text), text)

    def test_strip_consecutive_emojis_removes_leading_run(self) -> None:
        text = "😀😀😀😀😀😀😀😀说明文字"
        self.assertEqual(EmojiUtils.strip_consecutive_emojis(text), "说明文字")

    def test_strip_consecutive_emojis_keeps_non_consecutive_text(self) -> None:
        text = "正常说明😀"
        self.assertEqual(EmojiUtils.strip_consecutive_emojis(text), "正常说明😀")


if __name__ == "__main__":
    unittest.main()
