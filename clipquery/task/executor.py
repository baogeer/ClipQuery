"""任务执行器：从 Redis 队列拿到一条任务，获取分布式锁，按 task_type 分发执行。

Worker 容器的消费端调用：
    consume_once(worker_id) -> bool   # 执行了一条返回 True，队列空返回 False

按 task_type 分发：
    - ingest_cgtn_article: payload.article_url → 爬 + 转码 + 入库
    - ingest_path:         payload.video_path → 本地 MP4 直接入库
    - ingest_upload:       payload.video_path → 上传落盘后的 MP4 入库（同 ingest_path）

分布式锁保证同一 task_id 并发安全。锁 ttl = scheduler_task_ttl，
执行体内部定时刷新（_run_with_lock_refresh）。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from ..config import settings
from ..pipeline.indexer import ingest_video
from ..queue import TaskState, get_queue

logger = logging.getLogger("clipquery.task.executor")

TASK_INGEST_CGTN = "ingest_cgtn_article"
TASK_INGEST_PATH = "ingest_path"
TASK_INGEST_UPLOAD = "ingest_upload"


def _run_cgtn(payload: dict[str, Any]) -> dict:
    from ..website.cgtn import process_one_article, url_hash_id

    article_url = (payload.get("article_url") or "").strip()
    if not article_url:
        raise ValueError("payload 缺少 article_url")

    force = bool(payload.get("_force"))
    article_id = payload.get("article_id") or url_hash_id(article_url)
    info = process_one_article(article_url)
    if info is None:
        raise RuntimeError("process_one_article 返回 None（无视频或下载失败）")

    video_path = settings.news_dir / article_id / "video.mp4"
    if not video_path.exists():
        raise FileNotFoundError(f"video.mp4 不存在: {video_path}")

    frames = ingest_video(
        video_path,
        article_id=article_id,
        show_progress=False,
        skip_if_exists=not force,   # force=True → 即使库里有也重跑
    )
    return {
        "article_id":    article_id,
        "video_path":    str(video_path),
        "frames_written": frames,
        "force":         force,
    }


def _run_path(payload: dict[str, Any]) -> dict:
    video_path = (payload.get("video_path") or "").strip()
    if not video_path:
        raise ValueError("payload 缺少 video_path")
    force = bool(payload.get("_force"))
    frames = ingest_video(
        video_path,
        show_progress=False,
        skip_if_exists=not force,
    )
    return {"video_path": video_path, "frames_written": frames, "force": force}


_DISPATCH: dict[str, Callable[[dict[str, Any]], dict]] = {
    TASK_INGEST_CGTN:   _run_cgtn,
    TASK_INGEST_PATH:   _run_path,
    TASK_INGEST_UPLOAD: _run_path,
}


def _run_with_lock_refresh(
    task_id: str,
    worker_id: str,
    func: Callable[[], dict],
) -> dict:
    """执行 func，期间定时刷新 Redis 锁 TTL，避免长任务被误释放。"""
    q = get_queue()
    stop = threading.Event()
    interval = max(5, settings.scheduler_task_heartbeat)
    ttl = settings.scheduler_task_ttl

    def _refresh_loop() -> None:
        while not stop.wait(interval):
            try:
                ok = q.refresh_lock(task_id, worker_id, ttl)
                if not ok:
                    logger.warning("[task %s] 锁已丢失，worker=%s", task_id, worker_id)
            except Exception:
                logger.exception("[task %s] 刷新锁异常", task_id)

    t = threading.Thread(target=_refresh_loop, name=f"lock-refresh-{task_id}", daemon=True)
    t.start()
    try:
        return func()
    finally:
        stop.set()
        t.join(timeout=2)


def execute_task(task_doc: dict, worker_id: str) -> bool:
    """执行一条任务。获取不到锁 / 未知类型 时返回 False 表示跳过。"""
    task_id = task_doc.get("task_id")
    task_type = task_doc.get("task_type")
    payload = task_doc.get("payload") or {}

    if not task_id or not task_type:
        logger.warning("非法任务文档，跳过: %r", task_doc)
        return False

    runner = _DISPATCH.get(task_type)
    if runner is None:
        logger.warning("未知 task_type=%s task_id=%s，mark_failed", task_type, task_id)
        get_queue().mark_failed(task_id, error=f"unknown task_type={task_type}")
        return False

    q = get_queue()
    if not q.acquire_lock(task_id, worker_id, settings.scheduler_task_ttl):
        logger.info("[task %s] 锁已被他人持有，跳过", task_id)
        return False

    q.mark_in_progress(task_id, worker_id)
    logger.info("[task %s] 开始执行 type=%s worker=%s", task_id, task_type, worker_id)
    try:
        result = _run_with_lock_refresh(
            task_id,
            worker_id,
            lambda: runner(payload),
        )
    except Exception as e:
        logger.exception("[task %s] 执行失败", task_id)
        q.mark_failed(task_id, error=str(e))
        q.incr_retry(task_id)
        q.release_lock(task_id, worker_id)
        return True   # 执行过了，只是失败

    q.mark_done(task_id, result=result)
    q.release_lock(task_id, worker_id)
    logger.info("[task %s] 执行完成 result=%s", task_id, result)
    # 结果已落 OpenSearch/向量库，去重由下游兜底，这里清掉 Redis meta
    q.delete_meta(task_id)
    return True


def consume_once(worker_id: str, timeout: int | None = None) -> bool:
    """BRPOP 一条执行。超时 / 队列空返回 False。"""
    q = get_queue()
    doc = q.pop(timeout=timeout)
    if doc is None:
        return False
    return execute_task(doc, worker_id)
