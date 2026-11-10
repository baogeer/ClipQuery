"""MCP (Model Context Protocol) 服务：暴露本项目的只读能力给 LLM 客户端。

工具清单（故意不含 /search —— 图片/多模态语义检索更适合走原 REST）：
    - health           健康检查
    - get_task         查询任务状态
    - list_articles    已入库文章列表（分页 + title 搜索）
    - get_article      已入库文章详情
    - search_subtitle  台词语义检索

传输协议：Streamable HTTP（含 SSE），由主 FastAPI 进程通过 ASGI 挂载在 /mcp 路由下
（见 clipquery/main.py 底部），一个端口同时提供 REST + MCP，部署无额外进程。

工具实现**直接调 Python 层**（get_queue / get_article_store / Retriever），
不走 HTTP 回环，省一层序列化和 TCP。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .queue import get_queue
from .search.retriever import Retriever
from .storage.article_store import get_article_store
from .storage.subtitle_store import get_subtitle_store
from .storage.vector_store import get_vector_store

logger = logging.getLogger("clipquery.mcp")

# MCP 自带 DNS rebinding 防护，默认只允许 Host=localhost/127.0.0.1，
# 用 IP（如 10.81.20.229:8008）直连会 421 "Invalid Host header"。
# 内网部署没有 DNS rebinding 威胁，直接关掉；线上若对外暴露应显式列白名单。
#
# 路径安排：
# - streamable-http 保持默认内部路径 /mcp，外层 main.py 用 app.mount("/", _mcp_asgi) 让 /mcp 对外
# - sse 内部路径改成 "/"，外层 app.mount("/sse", _mcp_sse) 让 /sse 对外；
#   消息端点同理从默认 /messages/ 不变（在 /sse 前缀下即 /sse/messages/）
# 两个子应用不能都挂到根 "/"：Starlette 会让后挂的覆盖前挂的，所以必须错开 mount 前缀。
mcp = FastMCP(
    "ClipQuery",
    sse_path="/",
    message_path="/messages/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"],
    ),
)


# ==================== 工具 ====================
@mcp.tool()
def health() -> dict[str, Any]:
    """返回服务健康：Redis 连通、队列长度、索引里的帧数 / 台词数。

    用于 LLM 快速确认后端是否可用。
    """
    q = get_queue()
    return {
        "status":            "ok" if q.ping() else "redis_down",
        "indexed_frames":    get_vector_store().count(),
        "indexed_subtitles": get_subtitle_store().count(),
        "queue_length":      q.queue_length(),
    }


@mcp.tool()
def get_task(task_id: str) -> dict[str, Any]:
    """按 task_id 查询入库任务当前状态。

    Args:
        task_id: 任务 ID，形如 "ingest-<article_id>" 或 "ingest-path-<hash>"。

    返回字段：state (queued/in_progress/done/failed) + 关联的 article_id /
    video_path / frames_written / error 等。任务不存在时返回 {"error": "not_found"}。
    """
    doc = get_queue().get(task_id)
    if not doc:
        return {"error": "not_found", "task_id": task_id}
    payload = doc.get("payload") or {}
    result = doc.get("result") or {}
    return {
        "task_id":        task_id,
        "state":          doc.get("state", "unknown"),
        "task_type":      doc.get("task_type"),
        "owner":          doc.get("owner") or None,
        "article_url":    payload.get("article_url"),
        "article_id":     payload.get("article_id") or result.get("article_id"),
        "video_path":     payload.get("video_path") or result.get("video_path"),
        "frames_written": result.get("frames_written"),
        "error":          doc.get("error") or None,
        "retry_count":    doc.get("retry_count", 0),
        "created_at":     doc.get("created_at") or None,
        "updated_at":     doc.get("updated_at") or None,
        "finished_at":    doc.get("finished_at") or None,
    }


@mcp.tool()
def list_articles(
    offset: int = 0,
    limit: int = 20,
    q: Optional[str] = None,
) -> dict[str, Any]:
    """已入库到 OpenSearch 的文章列表。

    Args:
        offset: 分页起点，>= 0。默认 0。
        limit:  每页条数，1-100。默认 20。
        q:      可选，按 title 做全文匹配；留空则返回全部。

    返回按 ingested_at 倒序（最新在前）。不含 paragraphs 正文，
    详情请调 get_article。
    """
    limit = max(1, min(100, int(limit)))
    offset = max(0, int(offset))
    docs, total = get_article_store().list(
        offset=offset,
        limit=limit,
        q=q,
        fields=["id", "title", "url", "cos_video_url", "ingested_at"],
    )
    return {
        "total":   total,
        "offset":  offset,
        "limit":   limit,
        "results": docs,
    }


@mcp.tool()
def get_article(article_id: str) -> dict[str, Any]:
    """按 article_id 返回单篇文章的完整元数据（含正文 paragraphs + 所有 COS URL）。"""
    doc = get_article_store().get(article_id)
    if doc is None:
        return {"error": "not_found", "article_id": article_id}
    doc.setdefault("id", article_id)
    return doc


@mcp.tool()
def search_subtitle(query: str, top_k: int = 10) -> dict[str, Any]:
    """台词语义检索：找包含某句含义的台词，返回命中台词 + 精确秒数。

    Args:
        query: 查询文本，不能为空。
        top_k: 返回命中条数，默认 10。

    每条结果带 article_id / title / start_time / end_time / text /
    source_video_url / clip_url。前端可用 start_time 精确跳到视频某秒。
    """
    query = (query or "").strip()
    if not query:
        return {"error": "empty_query", "results": []}
    top_k = max(1, min(50, int(top_k)))
    segments = Retriever().search_subtitle(text=query, top_k=top_k)
    return {
        "results": [
            {
                "article_id":       s.article_id,
                "article_url":      s.article_url,
                "title":            s.title,
                "clip_id":          s.clip_id,
                "clip_url":         s.clip_url,
                "source_video_url": s.source_video_url,
                "start_time":       s.start_time,
                "end_time":         s.end_time,
                "text":             s.text,
                "score":            s.score,
            }
            for s in segments
        ]
    }


# FastMCP 两个 transport：
#   - streamable-http: 新协议（2024-11），内部路由 /mcp
#   - sse:             旧协议（2024-03），内部路由 /sse + /messages/，兼容老客户端
# 两个 ASGI 子应用的 route 都已在 /mcp、/sse、/messages 这几个确定路径上，
# 外层 FastAPI 挂到根 "/" 即可；见 main.py 底部。
def get_mcp_asgi_app():
    """返回 streamable-http ASGI app（内部路径 /mcp）。延迟调用避免 import 副作用。"""
    return mcp.streamable_http_app()


def get_mcp_sse_app():
    """返回 SSE ASGI app（内部路径 /sse + /messages/）。兼容老 MCP 客户端（如 workbuddy）。"""
    return mcp.sse_app()
