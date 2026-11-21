"""应用配置。密钥只从环境变量或本地 .env 读取，不写死在代码里，也不打印。"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_policy_qa_home() -> Path:
    """安装与数据根目录。Windows 默认 D:/policy-qa，其他系统默认 ~/policy-qa。可用环境变量覆盖。"""
    raw = os.environ.get("POLICY_QA_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    if sys.platform.startswith("win"):
        return Path("D:/policy-qa")
    return Path.home() / "policy-qa"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    policy_qa_home: Path = Field(default_factory=default_policy_qa_home)
    # 可选，覆盖默认的 <POLICY_QA_HOME>/data/policy_qa.sqlite
    policy_qa_db_path: Path | None = None

    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"

    # 模型名来自配置；维度也从配置读取，建表时不写死 1024。
    embedding_model: str = "text-embedding-v4"
    embedding_dim: int = 1024
    rerank_model: str = "gte-rerank"
    # qwen-turbo 已于 2026-10-10 官方下线，默认改为 qwen3.8-flash。
    qwen_fast_model: str = "qwen3.8-flash"
    qwen_strong_model: str = "qwen-plus"

    app_host: str = "127.0.0.1"
    app_port: int = 8000
    streamlit_host: str = "127.0.0.1"
    streamlit_port: int = 8501

    def home_dir(self) -> Path:
        return Path(self.policy_qa_home).expanduser()

    def data_dir(self) -> Path:
        return self.home_dir() / "data"

    def db_path(self) -> Path:
        if self.policy_qa_db_path is not None:
            return Path(self.policy_qa_db_path).expanduser()
        return self.data_dir() / "policy_qa.sqlite"

    def log_dir(self) -> Path:
        return self.home_dir() / "logs"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
