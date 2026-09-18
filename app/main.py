"""断点分片上传协调服务（不接收/保存文件内容）。

流程：
  1. POST   /sessions                 创建会话（总分片数 + 期望文件摘要）
  2. POST   /sessions/{id}/leases     领取某分片的限时租约（同片仅一个有效租约）
  3. POST   /sessions/{id}/chunks     凭租约令牌 + 分片摘要上报完成（幂等）
  4. POST   /sessions/{id}/commit     全片完成才允许提交，返回按序分片清单
  5. GET    /sessions/{id}            查询会话状态
  6. GET    /healthz                  健康检查
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .config import settings
from .db import connect, init_db, write_lock


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 依赖安装后，建库纳入启动流程（幂等）
    init_db()
    yield


app = FastAPI(
    title="Resumable Chunk Upload Coordinator",
    version="1.0.0",
    description="协调断点分片上传的租约与完成状态；不接收、不保存文件内容。",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return secrets.token_hex(16)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def get_conn(request: Request) -> sqlite3.Connection:
    # 每个请求一个连接，由 db_session 中间件创建并关闭
    return request.state.db


@app.middleware("http")
async def db_session(request: Request, call_next):
    conn = connect()
    request.state.db = conn
    try:
        return await call_next(request)
    finally:
        conn.close()


def fetch_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "session_not_found"})
    return row


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------

class CreateSessionIn(BaseModel):
    total_chunks: int = Field(..., ge=1, description="总分片数")
    expected_digest: str = Field(..., min_length=1, description="期望的完整文件摘要（算法由客户端约定，如 hex）")


class LeaseIn(BaseModel):
    chunk_index: int = Field(..., ge=0, description="分片序号，从 0 开始")


class ChunkCompleteIn(BaseModel):
    chunk_index: int = Field(..., ge=0)
    lease_token: str = Field(..., min_length=1, description="领取租约时获得的令牌")
    digest: str = Field(..., min_length=1, description="该分片内容的摘要")
    size: Optional[int] = Field(None, ge=0, description="分片大小（字节，可选，仅用于展示）")


class CommitIn(BaseModel):
    # 可选：提交时再次给出文件摘要，必须与创建会话时的期望摘要一致
    digest: Optional[str] = None


class LeaseOut(BaseModel):
    lease_token: str
    chunk_index: int
    expires_at: str
    expires_in: int
    already_completed: bool


class ChunkOut(BaseModel):
    chunk_index: int
    digest: str
    size: Optional[int]
    completed_at: str


class SessionOut(BaseModel):
    session_id: str
    total_chunks: int
    expected_digest: str
    status: str
    created_at: str
    committed_at: Optional[str]
    completed_count: int
    missing_chunks: List[int]
    chunks: List[ChunkOut]


class CommitOut(SessionOut):
    duplicate: bool = False


class ChunkCompleteOut(BaseModel):
    chunk_index: int
    digest: str
    size: Optional[int] = None
    completed_at: str
    duplicate: bool = False
    # 首次完成时附带进度；幂等重放时不带
    completed_count: Optional[int] = None
    missing_chunks: Optional[List[int]] = None


# ---------------------------------------------------------------------------
# 状态组装
# ---------------------------------------------------------------------------

def build_session_payload(conn: sqlite3.Connection, s: sqlite3.Row) -> dict:
    rows = conn.execute(
        "SELECT chunk_index, digest, size, completed_at FROM chunks "
        "WHERE session_id = ? ORDER BY chunk_index",
        (s["id"],),
    ).fetchall()
    done = {r["chunk_index"] for r in rows}
    total = s["total_chunks"]
    missing = [i for i in range(total) if i not in done]
    return {
        "session_id": s["id"],
        "total_chunks": total,
        "expected_digest": s["expected_digest"],
        "status": s["status"],
        "created_at": iso(s["created_at"]),
        "committed_at": iso(s["committed_at"]) if s["committed_at"] else None,
        "completed_count": len(rows),
        "missing_chunks": missing,
        "chunks": [
            {
                "chunk_index": r["chunk_index"],
                "digest": r["digest"],
                "size": r["size"],
                "completed_at": iso(r["completed_at"]),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.post("/sessions", status_code=201, response_model=SessionOut)
def create_session(body: CreateSessionIn, conn: sqlite3.Connection = Depends(get_conn)):
    if body.total_chunks > settings.max_total_chunks:
        raise HTTPException(
            status_code=400,
            detail={"code": "total_chunks_too_large", "max": settings.max_total_chunks},
        )
    sid = new_id()
    now = time.time()
    with write_lock:
        conn.execute(
            "INSERT INTO sessions(id, total_chunks, expected_digest, status, created_at) "
            "VALUES (?, ?, ?, 'open', ?)",
            (sid, body.total_chunks, body.expected_digest, now),
        )
    s = fetch_session(conn, sid)
    return build_session_payload(conn, s)


@app.get("/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    s = fetch_session(conn, session_id)
    return build_session_payload(conn, s)


@app.post(
    "/sessions/{session_id}/leases",
    status_code=201,
    response_model=LeaseOut,
    responses={200: {"model": LeaseOut, "description": "分片已完成，无需租约"}},
)
def claim_lease(
    session_id: str,
    body: LeaseIn,
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn),
):
    s = fetch_session(conn, session_id)
    if body.chunk_index >= s["total_chunks"]:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "chunk_index_out_of_range",
                "chunk_index": body.chunk_index,
                "total_chunks": s["total_chunks"],
            },
        )
    if s["status"] == "committed":
        raise HTTPException(status_code=409, detail={"code": "session_committed"})

    now = time.time()

    # 已完成的分片无需再领取租约；明确告知客户端，保持可重放
    already = conn.execute(
        "SELECT 1 FROM chunks WHERE session_id = ? AND chunk_index = ?",
        (session_id, body.chunk_index),
    ).fetchone()
    if already is not None:
        response.status_code = 200
        return LeaseOut(
            lease_token="",
            chunk_index=body.chunk_index,
            expires_at=iso(now),
            expires_in=0,
            already_completed=True,
        )

    ttl = settings.lease_ttl_seconds
    token = new_token()
    expires = now + ttl

    with write_lock:
        active = conn.execute(
            "SELECT token, expires_at FROM leases "
            "WHERE session_id = ? AND chunk_index = ? AND superseded = 0",
            (session_id, body.chunk_index),
        ).fetchone()
        if active is not None:
            if active["expires_at"] > now:
                # 同一分片已有有效租约：拒绝重复领取
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "lease_already_active",
                        "chunk_index": body.chunk_index,
                        "retry_after": max(1, int(active["expires_at"] - now + 0.999)),
                    },
                )
            # 旧租约已过期：失效后允许重新领取
            conn.execute(
                "UPDATE leases SET superseded = 1 WHERE token = ?",
                (active["token"],),
            )

        try:
            conn.execute(
                "INSERT INTO leases(token, session_id, chunk_index, expires_at, superseded, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (token, session_id, body.chunk_index, expires, now),
            )
        except sqlite3.IntegrityError:
            # 并发下另一请求抢先建立了有效租约
            raise HTTPException(status_code=409, detail={"code": "lease_already_active"})

    return LeaseOut(
        lease_token=token,
        chunk_index=body.chunk_index,
        expires_at=iso(expires),
        expires_in=ttl,
        already_completed=False,
    )


@app.post("/sessions/{session_id}/chunks", response_model=ChunkCompleteOut)
def complete_chunk(
    session_id: str,
    body: ChunkCompleteIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    s = fetch_session(conn, session_id)
    if body.chunk_index >= s["total_chunks"]:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "chunk_index_out_of_range",
                "chunk_index": body.chunk_index,
                "total_chunks": s["total_chunks"],
            },
        )
    if s["status"] == "committed":
        raise HTTPException(status_code=409, detail={"code": "session_committed"})

    now = time.time()
    with write_lock:
        existing = conn.execute(
            "SELECT digest, size, completed_at FROM chunks "
            "WHERE session_id = ? AND chunk_index = ?",
            (session_id, body.chunk_index),
        ).fetchone()

        lease = conn.execute(
            "SELECT token, expires_at, superseded FROM leases "
            "WHERE token = ? AND session_id = ? AND chunk_index = ?",
            (body.lease_token, session_id, body.chunk_index),
        ).fetchone()
        if lease is None:
            # 令牌不存在或不属于该会话/分片
            raise HTTPException(status_code=403, detail={"code": "invalid_lease"})

        if existing is not None:
            # 分片已完成：幂等优先（支持重试，允许租约已消费/过期）。
            # 摘要一致视为成功，摘要不同则冲突。
            if existing["digest"] != body.digest:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "digest_mismatch",
                        "chunk_index": body.chunk_index,
                        "existing_digest": existing["digest"],
                    },
                )
            return {
                "chunk_index": body.chunk_index,
                "digest": existing["digest"],
                "size": existing["size"],
                "completed_at": iso(existing["completed_at"]),
                "duplicate": True,
            }

        # 分片尚未完成：租约必须有效（未被取代、未过期）
        if lease["superseded"]:
            raise HTTPException(status_code=403, detail={"code": "lease_superseded"})
        if lease["expires_at"] <= now:
            raise HTTPException(status_code=403, detail={"code": "lease_expired"})

        try:
            conn.execute(
                "INSERT INTO chunks(session_id, chunk_index, digest, size, completed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, body.chunk_index, body.digest, body.size, now),
            )
        except sqlite3.IntegrityError:
            # 并发下另一请求刚刚写入：重读，按幂等/冲突处理
            row = conn.execute(
                "SELECT digest, size, completed_at FROM chunks "
                "WHERE session_id = ? AND chunk_index = ?",
                (session_id, body.chunk_index),
            ).fetchone()
            if row is not None and row["digest"] == body.digest:
                return {
                    "chunk_index": body.chunk_index,
                    "digest": row["digest"],
                    "size": row["size"],
                    "completed_at": iso(row["completed_at"]),
                    "duplicate": True,
                }
            raise HTTPException(
                status_code=409,
                detail={"code": "digest_mismatch", "chunk_index": body.chunk_index},
            )

        # 租约一次性消费：令牌立即失效，不可用于其他分片
        conn.execute(
            "UPDATE leases SET superseded = 1 WHERE token = ?", (body.lease_token,)
        )

    s = fetch_session(conn, session_id)
    payload = build_session_payload(conn, s)
    return {
        "chunk_index": body.chunk_index,
        "digest": body.digest,
        "size": body.size,
        "completed_at": iso(now),
        "duplicate": False,
        "completed_count": payload["completed_count"],
        "missing_chunks": payload["missing_chunks"],
    }


@app.post("/sessions/{session_id}/commit", response_model=CommitOut)
def commit_session(
    session_id: str,
    body: Optional[CommitIn] = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    s = fetch_session(conn, session_id)

    if body is not None and body.digest is not None and body.digest != s["expected_digest"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "file_digest_mismatch",
                "expected_digest": s["expected_digest"],
                "provided_digest": body.digest,
            },
        )

    payload = build_session_payload(conn, s)

    if s["status"] == "committed":
        # 重复提交幂等：返回同一份按序清单
        payload["duplicate"] = True
        return payload

    if payload["missing_chunks"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "chunks_missing",
                "completed_count": payload["completed_count"],
                "total_chunks": payload["total_chunks"],
                "missing_chunks": payload["missing_chunks"],
            },
        )

    now = time.time()
    with write_lock:
        cur = conn.execute(
            "UPDATE sessions SET status = 'committed', committed_at = ? "
            "WHERE id = ? AND status = 'open'",
            (now, session_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=409, detail={"code": "session_committed"})

    s = fetch_session(conn, session_id)
    return build_session_payload(conn, s)


@app.get("/healthz")
def healthz(conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "lease_ttl_seconds": settings.lease_ttl_seconds}
