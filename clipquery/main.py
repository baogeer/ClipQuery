"""ClipQuery 服务入口：只提供只读 API。

本进程**不做任何异步调度 / 消费**，也**不对外暴露任何任务投入接口**。承担:
    - /search               在线检索（CLIP 查询编码 → OpenSearch KNN，仅 REST）
    - /search_subtitle      台词语义检索（REST + MCP）
    - /task/{id}            任务状态查询（REST + MCP）
    - /articles             已入库文章列表（REST + MCP）
    - /articles/{id}        已入库文章详情（REST + MCP）
    - /health               健康检查（REST + MCP）
    - /mcp                  MCP Streamable-HTTP 端点（供 Claude Desktop / Cursor 等接入）

任务投入完全由本地脚本手动触发（不走 HTTP）：
    - python scripts/scan_cgtn.py       拉 CGTN 列表 → 入 Redis 队列
    - python scripts/consume_one.py     从 Redis 队列拿一条执行

这样服务进程完全是无状态只读 API，占用稳定，内存紧张的机器也能长期跑；
数据处理由开发机按需跑，不会在服务上消耗 CPU/内存。
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from .queue import get_queue, new_worker_id
from .search.retriever import ClipSegment, Retriever, SubtitleSegment
from .storage.article_store import get_article_store
from .storage.vector_store import get_vector_store

logger = logging.getLogger("clipquery")

WORKER_ID = new_worker_id("app")


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


class SubtitleHit(BaseModel):
    """台词搜命中：一句台词 + 它在视频里的精确时间。"""
    article_id: str
    article_url: Optional[str] = None
    title: Optional[str] = None
    clip_id: Optional[str] = None
    clip_url: Optional[str] = None            # 对应片段 MP4；前端 <video src=...>
    source_video_url: Optional[str] = None    # 原片 COS URL；前端可 seek 到 start_time
    start_time: float                         # 台词起始秒（相对于原视频）
    end_time: float                           # 台词结束秒
    text: str
    score: float


class SubtitleSearchResponse(BaseModel):
    results: list[SubtitleHit]


class ArticleListItem(BaseModel):
    """已入库文章列表项。paragraphs 不含，走 /articles/{id} 拿。"""
    id: str
    title: Optional[str] = None
    url: Optional[str] = None
    cos_video_url: Optional[str] = None
    ingested_at: Optional[str] = None


class ArticleListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    results: list[ArticleListItem]


class TaskStatusOut(BaseModel):
    task_id: str
    state: str                              # queued | in_progress | done | failed
    task_type: Optional[str] = None
    owner: Optional[str] = None
    article_url: Optional[str] = None
    article_id: Optional[str] = None
    video_path: Optional[str] = None
    frames_written: Optional[int] = None
    error: Optional[str] = None
    retry_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
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


def _to_subtitle_out(seg: SubtitleSegment) -> SubtitleHit:
    return SubtitleHit(
        article_id=seg.article_id,
        article_url=seg.article_url,
        title=seg.title,
        clip_id=seg.clip_id,
        clip_url=seg.clip_url,
        source_video_url=seg.source_video_url,
        start_time=seg.start_time,
        end_time=seg.end_time,
        text=seg.text,
        score=seg.score,
    )


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


# ==================== FastAPI 应用 ====================
# MCP 的 Streamable-HTTP transport 内部有 session manager 需要 lifespan 管理，
# 这里把它的 lifespan 组合进主 app，确保 MCP 启动/关闭都被执行。
from .mcp_server import get_mcp_asgi_app, get_mcp_sse_app  # noqa: E402

_mcp_asgi = get_mcp_asgi_app()   # Streamable-HTTP transport（新协议）
_mcp_sse  = get_mcp_sse_app()    # SSE transport（旧协议，兼容老客户端）


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    logger.info("ClipQuery API 启动 worker_id=%s (只做 API，不消费队列)", WORKER_ID)
    async with _mcp_asgi.router.lifespan_context(_mcp_asgi), \
               _mcp_sse.router.lifespan_context(_mcp_sse):
        try:
            yield
        finally:
            logger.info("ClipQuery API 退出 worker_id=%s", WORKER_ID)


app = FastAPI(title="ClipQuery", version="0.7.0", lifespan=lifespan)
# FastMCP 构造时已把 streamable_http_path/sse_path 都设为 "/"，
# 这里直接挂到 /mcp 和 /sse 就对外呈现 /mcp 和 /sse 两个端点。
app.mount("/mcp", _mcp_asgi)   # Streamable-HTTP（新协议，推荐）
app.mount("/sse", _mcp_sse)    # SSE（旧协议，兼容只支持 SSE 的客户端）


# ==================== 健康检查 ====================
@app.get("/health")
def health() -> dict:
    from .storage.subtitle_store import get_subtitle_store
    vs = get_vector_store()
    q = get_queue()
    sub = get_subtitle_store()
    return {
        "status":            "ok" if q.ping() else "redis_down",
        "worker_id":         WORKER_ID,
        "indexed_frames":    vs.count(),
        "indexed_subtitles": sub.count(),
        "queue_length":      q.queue_length(),
    }


# ==================== /task/{task_id} ====================
@app.get("/task/{task_id}", response_model=TaskStatusOut)
def get_task(task_id: str) -> TaskStatusOut:
    doc = get_queue().get(task_id)
    if not doc:
        raise HTTPException(404, "任务不存在")
    payload = doc.get("payload") or {}
    result = doc.get("result") or {}
    return TaskStatusOut(
        task_id=task_id,
        state=doc.get("state", "unknown"),
        task_type=doc.get("task_type"),
        owner=doc.get("owner") or None,
        article_url=payload.get("article_url"),
        article_id=payload.get("article_id") or result.get("article_id"),
        video_path=payload.get("video_path") or result.get("video_path"),
        frames_written=result.get("frames_written"),
        error=doc.get("error") or None,
        retry_count=doc.get("retry_count", 0),
        created_at=doc.get("created_at") or None,
        updated_at=doc.get("updated_at") or None,
        finished_at=doc.get("finished_at") or None,
    )


# ==================== /articles ====================
_ARTICLE_LIST_FIELDS = ["id", "title", "url", "cos_video_url", "ingested_at"]


@app.get("/articles", response_model=ArticleListResponse)
def list_articles(
    offset: int = Query(default=0, ge=0, description="分页起点"),
    limit: int = Query(default=20, ge=1, le=100, description="每页条数，上限 100"),
    q: Optional[str] = Query(default=None, description="按 title 做 match 查询；空则列全部"),
) -> ArticleListResponse:
    """已入库文章列表，按 ingested_at 倒序。不返 paragraphs（见详情接口）。"""
    docs, total = get_article_store().list(
        offset=offset,
        limit=limit,
        q=q,
        fields=_ARTICLE_LIST_FIELDS,
    )
    return ArticleListResponse(
        total=total,
        offset=offset,
        limit=limit,
        results=[ArticleListItem(**d) for d in docs],
    )


@app.get("/articles/{article_id}")
def get_article(article_id: str) -> dict:
    """已入库文章详情，返回 clipquery_articles 里的完整 _source（含 paragraphs）。"""
    doc = get_article_store().get(article_id)
    if doc is None:
        raise HTTPException(404, "文章不存在")
    doc.setdefault("id", article_id)
    return doc


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


# ==================== /search_subtitle ====================
@app.post("/search_subtitle", response_model=SubtitleSearchResponse)
async def search_subtitle(
    query: str = Form(...),
    top_k: int = Form(default=10),
) -> SubtitleSearchResponse:
    """按台词语义搜 → 命中带精确 start_time / end_time。

    前端拿到 start_time 后可以：
      · 跳到原片 source_video_url 的 start_time 秒（HTML5 <video>.currentTime = start_time）
      · 或者直接播 clip_url（当前片段 MP4，从 0 开始播到命中台词那段）
    """
    query = (query or "").strip()
    if not query:
        raise HTTPException(400, "query 不能为空")
    retriever = Retriever()
    segments = retriever.search_subtitle(text=query, top_k=top_k)
    return SubtitleSearchResponse(results=[_to_subtitle_out(s) for s in segments])
