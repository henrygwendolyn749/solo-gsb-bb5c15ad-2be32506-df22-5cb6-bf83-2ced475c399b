FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据文件目录（可通过 volume 持久化）
RUN mkdir -p /data
VOLUME ["/data"]

ENV DB_PATH=/data/sessions.db \
    HOST=0.0.0.0 \
    PORT=8080 \
    LEASE_TTL_SECONDS=300 \
    MAX_TOTAL_CHUNKS=100000

EXPOSE 8080

# 启动即建库（app 启动时 init_db 幂等执行），随后对外服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
