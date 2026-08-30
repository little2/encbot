import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error, request

SETTING_URL = os.getenv('SETTING_URL')

class SharedConfig:
    _lock = threading.Lock()
    _loaded = False
    _data: Dict[str, Any] = {}

    # 你的 API
    API_URL = SETTING_URL or "https://example.com/api/config"

    # 本地缓存文件
    CACHE_FILE = Path(__file__).resolve().parent / "cache" / "config_cache.json"

    @classmethod
    def _ensure_cache_dir(cls) -> None:
        cls.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _download_from_api(cls) -> Dict[str, Any]:
        """
        从远端 API 下载 JSON
        """
        try:
            with request.urlopen(cls.API_URL, timeout=10) as response:
                payload = response.read().decode("utf-8", errors="ignore")
        except error.URLError:
            raise

        data = json.loads(payload or "{}")

        if not isinstance(data, dict):
            raise ValueError("API 返回格式不是 dict")

        return data

    @classmethod
    def _save_to_local(cls, data: Dict[str, Any]) -> None:
        """
        保存到本地 JSON
        """
        cls._ensure_cache_dir()
        with open(cls.CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def _load_from_local(cls) -> Dict[str, Any]:
        """
        从本地 JSON 读取
        """
        if not cls.CACHE_FILE.exists():
            raise FileNotFoundError(f"本地缓存不存在: {cls.CACHE_FILE}")

        with open(cls.CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("本地 JSON 格式不是 dict")

        return data

    @classmethod
    def load(cls, force_refresh: bool = False) -> None:
        """
        初始化加载：
        - 默认只加载一次
        - force_refresh=True 时强制重新从 API 尝试拉取
        """
        with cls._lock:
            if cls._loaded and not force_refresh:
                return

            try:
                data = cls._download_from_api()
                cls._save_to_local(data)
                cls._data = data
                print("✅ 配置已从 API 下载并写入本地缓存")
            except Exception as e:
                print(f"⚠️ API 下载失败，改读本地缓存: {e}")
                cls._data = cls._load_from_local()
                print("⚠️ 配置已从本地缓存载入")

            cls._loaded = True

    @classmethod
    def get(cls, key: str, default: Optional[Any] = None) -> Any:
        """
        读取单个配置值
        """
        if not cls._loaded:
            cls.load()
        return cls._data.get(key, default)

    @classmethod
    def get_all(cls) -> Dict[str, Any]:
        """
        读取全部配置
        """
        if not cls._loaded:
            cls.load()
        return cls._data.copy()

    @classmethod
    def get_var1(cls) -> Any:
        return cls.get("var1")

    @classmethod
    def get_var2(cls) -> Any:
        return cls.get("var2")

    @classmethod
    def get_var3(cls) -> Any:
        return cls.get("var3")