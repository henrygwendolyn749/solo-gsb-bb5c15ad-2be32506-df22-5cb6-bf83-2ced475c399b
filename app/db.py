"""SQLite 连接、建库与初始化。

表结构：
  sessions  上传会话（总分片数、期望文件摘要、状态）
  chunks    已完成的分片（会话 + 分片序号唯一，保存分片摘要，不保存文件内容）
  leases    分片租约（active 唯一令牌，过期后可被重新领取）

本服务全程不接收、不保存任何文件字节，chunks 表仅记录摘要。
"""
from __future__ import annotations

import os
import sqlite3
import threading

from .config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    total_chunks    INTEGER NOT NULL,
    expected_digest TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',   -- open | committed
    created_at      REAL NOT NULL,
    committed_at    REAL
);

CREATE TABLE IF NOT EXISTS chunks (
    session_id  TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    digest      TEXT NOT NULL,
    size        INTEGER,
    completed_at REAL NOT NULL,
    PRIMARY KEY (session_id, chunk_index),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS leases (
    token       TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    expires_at  REAL NOT NULL,
    superseded  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- 只对“仍有效”的租约建唯一索引（superseded=0）。
-- 过期租约保留在表中供审计，但不阻塞同一分片被重新领取。
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_lease
    ON leases(session_id, chunk_index)
    WHERE superseded = 0;
"""

# 写操作串行化，配合 unique 索引保证“同一分片同时仅一个有效租约”
_write_lock = threading.Lock()


def get_db_path() -> str:
    return settings.db_path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path,
        timeout=30.0,
        isolation_level=None,  # autocommit；事务由显式 BEGIN/COMMIT 控制
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    """建库：确保数据目录存在并执行建表语句（幂等）。"""
    db_path = settings.db_path
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


write_lock = _write_lock
