"""运行期配置。所有项均可通过环境变量覆盖。"""
from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Settings:
    # SQLite 数据库文件路径（容器内默认 /data/sessions.db）
    db_path: str = os.getenv("DB_PATH", "/data/sessions.db")
    # 分片租约有效期（秒）
    lease_ttl_seconds: int = _env_int("LEASE_TTL_SECONDS", 300)
    # 单次会话允许的最大分片数，防止误填
    max_total_chunks: int = _env_int("MAX_TOTAL_CHUNKS", 100_000)
    # 分片索引取值范围（不含上界），与分片数一起校验
    # 服务监听
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _env_int("PORT", 8080)


settings = Settings()
