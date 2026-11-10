# syntax=docker/dockerfile:1.6

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_ENDPOINT=https://hf-mirror.com \
    HF_HOME=/app/data/hf_cache

# ---------- 系统依赖 ----------
# ffmpeg      : m3u8 下载、音频抽取、视频切片
# curl        : 健康检查
# ca-certs    : HTTPS
# libgl1      : opencv-python 依赖（libGL.so.1）
# libglib2.0-0: opencv 依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---------- Python 依赖 ----------
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.2.0" \
    && pip install --no-cache-dir -r requirements.txt

# ---------- 项目代码 ----------
COPY clipquery ./clipquery
COPY scripts   ./scripts

# ---------- 预拉 CLIP 模型到镜像 ----------
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='sentence-transformers/clip-ViT-B-32', local_dir='/app/data/clip-ViT-B-32', local_dir_use_symlinks=False)"

# ---------- 运行时目录 ----------
RUN mkdir -p /app/news /app/data/thumbnails /app/data/clips /app/data/uploads /app/data/hf_cache

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# 当前服务只做只读 API + MCP；MCP 的 session manager 是进程级单例，仍要 --workers 1
CMD ["uvicorn", "clipquery.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
