# 断点分片上传协调 API

一个**只做协调、不碰文件内容**的断点续传服务：记录分片租约（lease）与各分片的完成摘要，
帮助多个上传客户端协调“谁传哪一片、是否传完”。服务使用 Python + FastAPI + SQLite，
数据在启动时自动建库。

> 本服务 **不接收、不保存任何文件字节**。客户端上报分片完成时只提交该分片的摘要（digest）。

## 单条命令启动

需要 Docker（含 Compose 插件 v2）。在本目录执行：

```bash
docker compose up --build -d
```

启动后：

- API 地址（本机）： `http://localhost:8080`
- 交互式文档（Swagger UI）： `http://localhost:8080/docs`
- 健康检查： `http://localhost:8080/healthz`
- 数据持久化在 Docker 卷 `upload-data`（容器内 `/data/sessions.db`）

查看日志 / 停止：

```bash
docker compose logs -f
docker compose down            # 停止并删除容器（保留数据卷）
docker compose down -v         # 同时删除数据卷
```

## 必要配置（均为环境变量，可选，带默认值）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/sessions.db` | SQLite 数据库文件路径，建议放在挂载卷中 |
| `LEASE_TTL_SECONDS` | `300` | 分片租约有效期（秒）。过期后同一分片可被重新领取 |
| `MAX_TOTAL_CHUNKS` | `100000` | 单个会话允许的最大分片数 |
| `PORT` | `8080` | 服务监听端口（容器内），Compose 映射到宿主机同端口 |

依赖安装与建库均在容器启动时完成：镜像构建时 `pip install -r requirements.txt`，
应用 `startup` 阶段自动执行幂等建表（`app/db.py: init_db`）。

### 不用 Docker 时（本地开发）

```bash
pip install -r requirements.txt
DB_PATH=./sessions.db uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## 工作流程

```text
客户端                         协调服务
  │  1. 创建会话(总分片数, 期望文件摘要)
  │ ────────────────────────────────►  sessions
  │  2. 为分片 i 领取限时租约
  │ ────────────────────────────────►  leases（同一片同时仅一个有效租约）
  │  3. 自行把分片内容上传到真正的存储（本服务之外）
  │     并计算分片摘要
  │  4. 凭租约令牌 + 分片摘要上报完成（可重放、幂等）
  │ ────────────────────────────────►  chunks（仅存摘要）
  │  5. 全部分片完成后提交
  │ ────────────────────────────────►  返回按序汇总的分片清单
```

- 租约过期后不会自动续期，其他客户端可重新领取该分片；旧租约重新上报会被拒绝（`lease_superseded` / `lease_expired`）。
- 重复上报同一片且摘要一致 → 幂等成功（`duplicate: true`）；摘要不一致 → `409 digest_mismatch`。
- 有缺失分片时提交 → `409 chunks_missing`，错误体中给出 `missing_chunks` 列表。

## API

Base URL： `http://localhost:8080`

### 1. 创建上传会话

`POST /sessions`

```json
{ "total_chunks": 3, "expected_digest": "sha256:9f2a..." }
```

`201` 返回会话状态（含 `session_id`、`missing_chunks: [0,1,2]` 等）。

### 2. 领取分片租约

`POST /sessions/{session_id}/leases`

```json
{ "chunk_index": 0 }
```

成功 `201`：

```json
{
  "lease_token": "随机令牌，上报时必须原样带回",
  "chunk_index": 0,
  "expires_at": "2026-09-18T10:05:00Z",
  "expires_in": 300,
  "already_completed": false
}
```

- 该分片已有未过期租约： `409 lease_already_active`（附 `retry_after` 秒）。
- 分片已完成： `200` 且 `already_completed: true`，`lease_token` 为空，无需重传。

### 3. 上报分片完成（幂等）

`POST /sessions/{session_id}/chunks`

```json
{
  "chunk_index": 0,
  "lease_token": "上一步拿到的令牌",
  "digest": "sha256:该片内容的摘要",
  "size": 1048576
}
```

- 令牌无效 / 不属于该会话与分片： `403 invalid_lease`
- 租约已过期 / 已被新租约取代： `403 lease_expired` / `403 lease_superseded`
- 重复上报且摘要一致： `200 {"duplicate": true, ...}`
- 重复上报但摘要不一致： `409 digest_mismatch`

### 4. 提交会话

`POST /sessions/{session_id}/commit`

请求体可省略；也可附带完整文件摘要做最终核对：

```json
{ "digest": "sha256:9f2a..." }   // 可选，必须与 expected_digest 一致
```

- 全部完成： `200`，`status: "committed"`，`chunks` 为**按分片序号升序**的清单（index/digest/size/completed_at）。
- 尚有缺失： `409 chunks_missing`，返回 `missing_chunks`。
- 重复提交已提交的会话： 幂等返回同一份清单（`duplicate: true`）。

### 5. 查询会话状态

`GET /sessions/{session_id}`

```json
{
  "session_id": "...",
  "total_chunks": 3,
  "expected_digest": "sha256:9f2a...",
  "status": "open",
  "created_at": "2026-09-18T10:00:00Z",
  "committed_at": null,
  "completed_count": 1,
  "missing_chunks": [1, 2],
  "chunks": [ { "chunk_index": 0, "digest": "...", "size": 1048576, "completed_at": "..." } ]
}
```

### 6. 健康检查

`GET /healthz` → `{"status":"ok","lease_ttl_seconds":300}`

## 端到端示例（curl）

```bash
BASE=http://localhost:8080

# 创建会话
SID=$(curl -s -X POST $BASE/sessions -H 'Content-Type: application/json' \
  -d '{"total_chunks":2,"expected_digest":"sha256:abc"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

# 领取分片 0 的租约
TOKEN=$(curl -s -X POST $BASE/sessions/$SID/leases -H 'Content-Type: application/json' \
  -d '{"chunk_index":0}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["lease_token"])')

# （实际文件字节由客户端上传到别处，这里只上报摘要）
curl -s -X POST $BASE/sessions/$SID/chunks -H 'Content-Type: application/json' \
  -d "{\"chunk_index\":0,\"lease_token\":\"$TOKEN\",\"digest\":\"sha256:c0\",\"size\":123}"

# 未全部完成时提交 -> 409，missing_chunks=[1]
curl -s -X POST $BASE/sessions/$SID/commit

# 补齐分片 1 后提交 -> 200，返回按序清单
T1=$(curl -s -X POST $BASE/sessions/$SID/leases -H 'Content-Type: application/json' -d '{"chunk_index":1}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["lease_token"])')
curl -s -X POST $BASE/sessions/$SID/chunks -H 'Content-Type: application/json' \
  -d "{\"chunk_index\":1,\"lease_token\":\"$T1\",\"digest\":\"sha256:c1\",\"size\":456}"
curl -s -X POST $BASE/sessions/$SID/commit
```

## 设计说明

- **存储**： 单文件 SQLite（WAL 模式）。表 `sessions / chunks / leases`；`chunks` 只保存摘要与可选大小，绝不保存文件内容。
- **租约互斥**： `leases` 上对 `(session_id, chunk_index) WHERE superseded=0` 的部分唯一索引 + 写锁，
  保证并发领取时同一片只有一个有效租约；新租约行会使旧行失效，过期判断在使用令牌时进行。
- **时间**： 服务端时钟，Unix 时间戳存储，接口以 ISO-8601 UTC（`Z` 后缀）返回。
- **分片序号**： 从 `0` 开始，范围 `[0, total_chunks)`。
