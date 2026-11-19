"""应用配置。密钥只从环境变量或进程环境读取，不写死在代码里。"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_host: str = ""
    postgres_port: int = 5432
    postgres_db: str = "policy_qa"
    postgres_user: str = "box"
    postgres_password: str = ""

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0

    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    # 官方日本区域；LANGFUSE_HOST 仅作兼容别名，优先使用 LANGFUSE_BASE_URL。
    langfuse_base_url: str = "https://jp.cloud.langfuse.com"
    langfuse_host: str = ""

    embedding_model: str = ""
    embedding_dim: int | None = None
    rerank_model: str = "gte-rerank"
    qwen_fast_model: str = "qwen3.8-flash"
    qwen_strong_model: str = "qwen-plus"

    cache_ttl_seconds: int = 86400
    cache_namespace: str = "policyqa:v1"

    app_host: str = "127.0.0.1"
    app_port: int = 8000

    def dsn(self) -> str:
        # 本机 peer 认证：POSTGRES_HOST 留空则走 Unix socket，无需密码。
        if not self.postgres_host:
            return (
                f"postgresql://{self.postgres_user}@/{self.postgres_db}"
                f"?host=/var/run/postgresql"
            )
        password = self.postgres_password or ""
        auth = f"{self.postgres_user}:{password}" if password else self.postgres_user
        return (
            f"postgresql://{auth}@{self.postgres_host}:{self.postgres_port}"
            f"/{self.postgres_db}"
        )

    def langfuse_url(self) -> str:
        url = (self.langfuse_base_url or self.langfuse_host or "https://jp.cloud.langfuse.com").strip()
        return url.rstrip("/")


def get_settings() -> Settings:
    return Settings()
