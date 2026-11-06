from __future__ import annotations

import logging
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ..config import settings
from ..pipeline.indexer import ingest_video
from ..scheduler.runtime import WorkerRuntime, get_runtime, set_runtime
from ..scheduler.task_store import TaskState, get_task_store
from ..search.retriever import ClipSegment, Retriever
from ..storage.vector_store import get_vector_store

logger = logging.getLogger("clipquery.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """每个 uvicorn worker 启动时注册到 hash 环，关停时 deregister。"""
    runtime = WorkerRuntime()
    runtime.start()
    set_runtime(runtime)
    logger.info("API lifespan: worker_id=%s", runtime.worker_id)
    try:
        yield
    finally:
        runtime.shutdown()


app = FastAPI(title="ClipQuery", version="0.4.0", lifespan=lifespan)


# ==================== Pydantic 模型 ====================
class SegmentOut(BaseModel):
    """检索返回的单个片段：能直接播放 + 能跳回新闻详情。"""
    clip_id: str
    article_id: str
    article_url: Optional[str] = None
    title: Optional[str] = None
    start_time: float
    end_time: float
    clip_url: str                      # COS 上的 MP4，可直接 <video src>
    thumbnail: Optional[str] = None
    score: float
    hit_count: int
    best_frame_id: Optional[str] = None
    paragraphs: list[str] = []


class SearchResponse(BaseModel):
    results: list[SegmentOut]


class IngestResponse(BaseModel):
    task_id: str
    state: str
    owner_worker_id: Optional[str] = None
    article_url: Optional[str] = None
    video_path: Optional[str] = None


class TaskStatusOut(BaseModel):
    task_id: str
    state: str                              # queued | in_progress | done | failed
    task_type: Optional[str] = None
    owner_worker_id: Optional[str] = None
    article_url: Optional[str] = None
    article_id: Optional[str] = None
    video_path: Optional[str] = None
    frames_written: Optional[int] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    finished_at: Optional[str] = None


def _to_out(seg: ClipSegment) -> SegmentOut:
    return SegmentOut(
        clip_id=seg.clip_id,
        article_id=seg.article_id,
        article_url=seg.article_url,
        title=seg.title,
        start_time=seg.start_time,
        end_time=seg.end_time,
        clip_url=seg.clip_url,
        thumbnail=seg.thumbnail,
        score=seg.score,
        hit_count=seg.hit_count,
        best_frame_id=seg.best_frame_id,
        paragraphs=seg.paragraphs,
    )


# ==================== 健康检查 ====================
@app.get("/health")
def health() -> dict:
    vs = get_vector_store()
    rt = get_runtime()
    return {
        "status": "ok",
        "indexed_frames": vs.count(),
        "worker_id": rt.worker_id,
        "ring_members": rt.ring().members,
    }


# ==================== 后台任务执行体 ====================
def _execute_path_task(task_id: str, video_path: str) -> None:
    store = get_task_store()
    rt = get_runtime()
    claimed = store.claim(task_id, rt.worker_id)
    if not claimed:
        logger.info("[task %s] claim 失败，交给别的 worker", task_id)
        return

    try:
        n = ingest_video(video_path, show_progress=False)
        store.mark_done(task_id, result={"frames_written": n})
    except Exception as e:
        logger.exception("[task %s] ingest 失败", task_id)
        store.mark_failed(task_id, error=str(e))


def _execute_url_task(task_id: str, article_url: str) -> None:
    store = get_task_store()
    rt = get_runtime()
    claimed = store.claim(task_id, rt.worker_id)
    if not claimed:
        logger.info("[task %s] claim 失败，交给别的 worker", task_id)
        return

    try:
        from ..website.cgtn import process_one_article, url_hash_id
        info = process_one_article(article_url)
        if info is None:
            store.mark_failed(task_id, error="爬虫未产出：可能无视频或下载失败")
            return
        article_id = info.get("id") or url_hash_id(article_url)
        video_path = settings.news_dir / article_id / "video.mp4"
        if not video_path.exists():
            store.mark_failed(task_id, error=f"video.mp4 不存在: {video_path}")
            return
        frames = ingest_video(video_path, article_id=article_id, show_progress=False)
        store.mark_done(task_id, result={
            "article_id":    article_id,
            "video_path":    str(video_path),
            "frames_written": frames,
        })
    except Exception as e:
        logger.exception("[task %s] 处理失败", task_id)
        store.mark_failed(task_id, error=str(e))


# ==================== /ingest ====================
@app.post("/ingest", response_model=IngestResponse)
async def ingest(
    background: BackgroundTasks,
    file: UploadFile | None = File(default=None),
    path: str | None = Form(default=None),
    url: str | None = Form(default=None),
) -> IngestResponse:
    """入库：三选一
    - `file`：上传视频
    - `path`：服务器本地视频绝对路径
    - `url`：CGTN 文章 URL，后台自动抓取 + 入库

    任务状态存 OpenSearch 的 clipquery_tasks 索引；多 worker 下
    任务被路由到 hash 环上归属的 worker 执行。
    """
    provided = sum(x is not None and x != "" for x in (file, path, url))
    if provided == 0:
        raise HTTPException(400, "必须提供 file / path / url 之一")
    if provided > 1:
        raise HTTPException(400, "file / path / url 只能提供一个")

    store = get_task_store()
    rt = get_runtime()
    ring = rt.ring()

    # ---------- URL 入库：task_id = ingest-<article_id>，天然幂等 ----------
    if url:
        from ..website.cgtn import url_hash_id
        url = url.strip()
        article_id = url_hash_id(url)
        task_id = f"ingest-{article_id}"

        task = store.enqueue(
            task_id=task_id,
            task_type="ingest_cgtn_article",
            payload={"article_url": url, "article_id": article_id},
        )
        owner = ring.owner(article_id) or rt.worker_id

        if owner == rt.worker_id and task.get("state") == TaskState.QUEUED:
            # 归属本 worker 且未被认领：当前进程后台执行
            background.add_task(_execute_url_task, task_id, url)

        return IngestResponse(
            task_id=task_id,
            state=task.get("state", TaskState.QUEUED),
            owner_worker_id=owner,
            article_url=url,
        )

    # ---------- 文件 / 本地路径入库 ----------
    if file is not None:
        upload_dir = settings.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
        target = upload_dir / f"{uuid.uuid4().hex}{suffix}"
        with target.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        video_path = str(target.resolve())
    else:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise HTTPException(404, f"文件不存在: {p}")
        video_path = str(p)

    task_id = f"ingest-path-{uuid.uuid4().hex}"
    task = store.enqueue(
        task_id=task_id,
        task_type="ingest_path",
        payload={"video_path": video_path},
    )
    owner = ring.owner(task_id) or rt.worker_id
    if owner == rt.worker_id:
        background.add_task(_execute_path_task, task_id, video_path)

    return IngestResponse(
        task_id=task_id,
        state=task.get("state", TaskState.QUEUED),
        owner_worker_id=owner,
        video_path=video_path,
    )


# ==================== /task/{task_id} ====================
@app.get("/task/{task_id}", response_model=TaskStatusOut)
def get_task(task_id: str) -> TaskStatusOut:
    doc = get_task_store().get_source(task_id)
    if not doc:
        raise HTTPException(404, "任务不存在")
    payload = doc.get("payload") or {}
    result = doc.get("result") or {}
    return TaskStatusOut(
        task_id=task_id,
        state=doc.get("state", "unknown"),
        task_type=doc.get("task_type"),
        owner_worker_id=doc.get("owner_worker_id"),
        article_url=payload.get("article_url"),
        article_id=payload.get("article_id") or result.get("article_id"),
        video_path=payload.get("video_path") or result.get("video_path"),
        frames_written=result.get("frames_written"),
        error=doc.get("error"),
        created_at=doc.get("created_at"),
        heartbeat_at=doc.get("heartbeat_at"),
        finished_at=doc.get("finished_at"),
    )


# 兼容老接口：/ingest/{task_id} 等价 /task/{task_id}
@app.get("/ingest/{task_id}", response_model=TaskStatusOut)
def ingest_status(task_id: str) -> TaskStatusOut:
    return get_task(task_id)


# ==================== /search ====================
@app.post("/search", response_model=SearchResponse)
async def search(
    query: str | None = Form(default=None),
    top_k: int = Form(default=10),
    image: UploadFile | None = File(default=None),
) -> SearchResponse:
    if not query and image is None:
        raise HTTPException(400, "必须提供 query 或 image")

    image_bytes = await image.read() if image is not None else None
    retriever = Retriever()
    segments = retriever.search(
        text=query,
        image_bytes=image_bytes,
        top_k=top_k,
    )
    return SearchResponse(results=[_to_out(s) for s in segments])
