# syntax=docker/dockerfile:1.6
# -----------------------------------------------------------------------------
# ClipQuery 生产镜像（CPU 版本）
#
# 单进程 uvicorn + 多 worker 架构：
#   - uvicorn --workers N  启动 N 个 FastAPI worker（CLIPQUERY_UVICORN_WORKERS 控制）
#   - 每个 worker 内部 WorkerRuntime 自动做：
#       · 在 clipquery_workers 索引注册 + 10s 心跳
#       · 启动 APScheduler，调度 clipquery.task.* 下的所有定时任务
#       · 按 OpenSearch 上的 hash 环（一致性 hash）判定任务归属
#   - 任务状态全部放 OpenSearch clipquery_tasks，多 worker / 多节点一致
#   - worker 挂掉时心跳过期 30s 被剔除；in_progress 任务超时 5min 被别的 worker 抢占
#
# 构建:
#   docker build -t clipquery:latest .
#
# 运行:
#   docker run -d --name clipquery \
#     -p 8000:8000 \
#     -e CLIPQUERY_UVICORN_WORKERS=3 \
#     -v $PWD/.env:/app/.env:ro \
#     -v clipquery-news:/app/news \
#     -v clipquery-data:/app/data \
#     clipquery:latest
#
# 只跑一次爬虫（不走 supervisord / hash 环）：
#   docker run --rm \
#     -v $PWD/.env:/app/.env:ro \
#     -v clipquery-news:/app/news \
#     -v clipquery-data:/app/data \
#     clipquery:latest \
#     python scripts/pipeline_one.py "https://news.cgtn.com/news/.../index.html"
# -----------------------------------------------------------------------------

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 模型下载走镜像站；运行时可被 .env 的 HF_ENDPOINT 覆盖
    HF_ENDPOINT=https://hf-mirror.com \
    # 让 encoder.py 命中本地模型（和项目配置一致）
    HF_HOME=/app/data/hf_cache \
    # 默认 3 个 API worker。可在 docker run 时通过 -e 覆盖。
    CLIPQUERY_UVICORN_WORKERS=3

# ---------- 系统依赖 ----------
# ffmpeg      : m3u8 下载、音频抽取、视频切片
# supervisor  : 容器内进程守护（PID 1）
# curl        : 健康检查 + 调试
# ca-certs    : HTTPS
# libgl1      : opencv-python 依赖（libGL.so.1）
# libglib2.0-0: opencv 依赖
# 装完清理 apt 缓存控制镜像体积
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        supervisor \
        curl \
        ca-certificates \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---------- Python 依赖 ----------
# 单独 COPY requirements.txt 是为了利用层缓存：改代码时不会触发重装依赖
COPY requirements.txt .

# CPU 版 torch 用官方 CPU wheel 索引，省掉 ~2GB CUDA 运行时
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.2.0" \
    && pip install --no-cache-dir -r requirements.txt

# ---------- 项目代码 ----------
COPY clipquery ./clipquery
COPY scripts   ./scripts

# ---------- supervisord 配置 ----------
COPY docker/supervisord.conf /etc/supervisor/conf.d/clipquery.conf

# ---------- 预拉 CLIP 模型到镜像 ----------
# 和 encoder.py 的 _looks_complete() 对齐，运行时直接命中，无需联网
RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download( \
    repo_id='sentence-transformers/clip-ViT-B-32', \
    local_dir='/app/data/clip-ViT-B-32', \
    local_dir_use_symlinks=False, \
)"

# ---------- 运行时目录 ----------
# news/ 和 data/ 建议在 docker run 时挂 volume 做持久化
RUN mkdir -p /app/news /app/data/thumbnails /app/data/clips

EXPOSE 8000

# 健康检查：/health 返回 OpenSearch 索引里的帧数 + 当前 worker + 环成员
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# 用 supervisord 作为 PID 1，拉起 uvicorn 多 worker。
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
