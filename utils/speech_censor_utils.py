"""
言论审查 Class
1. 输入为 telegram message
2. 检查 message.text 或 message.caption 中的内容是否包含 KEYWORDS 定义的关键词
3. 返回匹配到的关键词键名；若都不符合，返回 None
4. 关键词不区分大小写
"""

from collections.abc import Iterable, Mapping
from typing import Any


class SpeechCensor:
    """检查 Telegram message 的 text/caption 是否包含定义的关键词。"""

    KEYWORDS: Mapping[str, Iterable[str]] = {
        "ban": (),
        "delete": (),
        "ignored": (),
    }

    def __init__(
        self,
        keywords: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        self.keywords = self._normalize_keyword_groups(
            self.KEYWORDS if keywords is None else keywords
        )

    def check_message(self, message: Any) -> str | None:
        """
        返回审查结果：
        - 匹配到关键词时，返回对应的关键词键名
        - 未匹配时，返回 None
        """
        content = self.get_message_content(message)
        return self.check_content(content)

    def check_content(self, content: Any) -> str | None:
        """检查文字内容是否包含关键词，匹配到即返回关键词键名。"""
        if not content:
            return None

        normalized_content = str(content).casefold()
        for keyword_name, keywords in self.keywords.items():
            if self._contains_keyword(normalized_content, keywords):
                return keyword_name
        return None

    @classmethod
    def get_message_content(cls, message: Any) -> str:
        """读取 message.text 或 message.caption；都没有时返回空字符串。"""
        text = getattr(message, "text", None)
        if text:
            return str(text)

        caption = getattr(message, "caption", None)
        if caption:
            return str(caption)

        return ""

    @classmethod
    def _normalize_keyword_groups(
        cls,
        keyword_groups: Mapping[str, Iterable[str]],
    ) -> dict[str, tuple[str, ...]]:
        return {
            str(keyword_name): cls._normalize_keywords(keywords)
            for keyword_name, keywords in keyword_groups.items()
        }

    @classmethod
    def _normalize_keywords(cls, keywords: Iterable[str] | None) -> tuple[str, ...]:
        if keywords is None:
            return ()

        normalized_keywords = []
        for keyword in keywords:
            normalized_keyword = str(keyword).strip().casefold()
            if normalized_keyword:
                normalized_keywords.append(normalized_keyword)
        return tuple(normalized_keywords)

    @classmethod
    def _contains_keyword(cls, content: str, keywords: Iterable[str]) -> bool:
        return any(keyword in content for keyword in keywords)
