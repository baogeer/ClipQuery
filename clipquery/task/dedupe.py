"""入队前的去重检查：避免同一任务被重复处理。

三道闸（按从快到慢的顺序）：
    1. Redis meta hash 的 state ∈ {queued, in_progress} → 跳过（正在/排队处理）
    2. Redis meta hash 的 state = failed                → 跳过（历史失败记录，永久保留）
    3. OpenSearch clipquery_articles.exists(aid)        → 跳过（成功完成的长期记录）

三者任意一个判定"已处理"就应拒绝入队。force=True 时全部绕过。

成功完成的任务会在 executor 里 DEL 掉 Redis meta（见 RedisQueue.delete_meta），
所以 state=done 不会在这里被看到——成功去重完全依赖闸 3。

对 path/upload 入库另配一套：按 video_path 查 clipquery_frames 是否有记录。
"""
from __future__ import annotations

import logging

from ..queue import TaskState, get_queue
from ..storage.article_store import get_article_store
from ..storage.vector_store import get_vector_store

logger = logging.getLogger("clipquery.task.dedupe")


class AlreadyProcessed(Exception):
    """任务已处理过。携带 reason 字段供上层返回给调用方。"""

    def __init__(self, reason: str, existing: dict | None = None):
        self.reason = reason
        self.existing = existing or {}
        super().__init__(reason)


def check_article_duplicate(article_id: str, task_id: str) -> dict | None:
    """URL / CGTN 类任务的三道闸。命中就返回现状 dict，否则返回 None。"""
    q = get_queue()

    # 闸 1 + 2：Redis meta
    existing = q.get(task_id)
    if existing:
        state = existing.get("state")
        if state in (TaskState.QUEUED, TaskState.IN_PROGRESS):
            logger.info("[dedupe] task=%s 已在队列/执行中 (state=%s)", task_id, state)
            return existing
        if state == TaskState.DONE:
            logger.info("[dedupe] task=%s 24h 内已完成", task_id)
            return existing

    # 闸 3：OpenSearch article 索引
    try:
        if get_article_store().exists(article_id):
            logger.info("[dedupe] article_id=%s 已入 clipquery_articles", article_id)
            return existing or {
                "task_id": task_id,
                "state": TaskState.DONE,
                "article_id": article_id,
                "note": "已在 article_store 中",
            }
    except Exception as e:
        logger.warning("[dedupe] 查 article_store 失败，按未入库处理: %s", e)

    return None


def check_video_path_duplicate(video_path: str, task_id: str) -> dict | None:
    """path/upload 类任务的两道闸：Redis meta + vector_store 帧数。"""
    q = get_queue()

    existing = q.get(task_id)
    if existing:
        state = existing.get("state")
        if state in (TaskState.QUEUED, TaskState.IN_PROGRESS):
            logger.info("[dedupe] task=%s 已在队列/执行中 (state=%s)", task_id, state)
            return existing
        if state == TaskState.DONE:
            logger.info("[dedupe] task=%s 24h 内已完成", task_id)
            return existing

    try:
        n = get_vector_store().video_frame_count(video_path)
        if n > 0:
            logger.info("[dedupe] video_path=%s 已有 %d 帧向量", video_path, n)
            return existing or {
                "task_id": task_id,
                "state": TaskState.DONE,
                "video_path": video_path,
                "note": f"已入 {n} 帧向量",
            }
    except Exception as e:
        logger.warning("[dedupe] 查 vector_store 失败，按未入库处理: %s", e)

    return None
