"""删除一篇文章在 OpenSearch / COS / 本地 / Redis 上的所有痕迹。

供本地脚本 force 入库前调用（如 scripts/pipeline_one.py --force）。全部"尽力而为"：某一步失败只打 WARN
不抛错，避免半清理状态永远卡住。
"""
from __future__ import annotations

import logging
import shutil

from .. import cos as cos_client
from ..config import settings
from ..queue import get_queue
from ..storage.article_store import get_article_store
from ..storage.clip_store import get_clip_store
from ..storage.subtitle_store import get_subtitle_store
from ..storage.vector_store import get_vector_store

logger = logging.getLogger("clipquery.task.purge")


def purge_article(article_id: str, *, drop_task_meta: bool = False) -> dict:
    """删除一篇文章的全部数据。返回各项的删除计数。

    drop_task_meta=True 时顺手删 Redis 任务元数据 (ingest-<article_id>)。
    默认不删，因为 force 入队时下一步就会覆盖写 queued。
    """
    report: dict[str, int | str] = {"article_id": article_id}

    # 1. OpenSearch
    try:
        ok = get_article_store().delete(article_id)
        report["article_deleted"] = 1 if ok else 0
    except Exception as e:
        logger.warning("删 clipquery_articles 失败 article_id=%s: %s", article_id, e)
        report["article_deleted"] = f"error: {e}"

    try:
        n = get_vector_store().delete_for_article(article_id)
        report["frames_deleted"] = n
    except Exception as e:
        logger.warning("删 clipquery_frames 失败 article_id=%s: %s", article_id, e)
        report["frames_deleted"] = f"error: {e}"

    try:
        n = get_clip_store().delete_for_article(article_id)
        report["clips_deleted"] = n
    except Exception as e:
        logger.warning("删 clipquery_clips 失败 article_id=%s: %s", article_id, e)
        report["clips_deleted"] = f"error: {e}"

    try:
        n = get_subtitle_store().delete_for_article(article_id)
        report["subtitles_deleted"] = n
    except Exception as e:
        logger.warning("删 clipquery_subtitles 失败 article_id=%s: %s", article_id, e)
        report["subtitles_deleted"] = f"error: {e}"

    # 2. COS：ClipQuery/<article_id>/ 下全部对象（原片 / 音频 / 字幕 / 片段 / 缩略图 / info.json）
    try:
        n = cos_client.delete_prefix(cos_client.article_prefix(article_id))
        report["cos_objects_deleted"] = n
    except Exception as e:
        logger.warning("删 COS 对象失败 article_id=%s: %s", article_id, e)
        report["cos_objects_deleted"] = f"error: {e}"

    # 3. 本地 news/<article_id>/
    local_dir = settings.news_dir / article_id
    if local_dir.exists():
        try:
            shutil.rmtree(local_dir, ignore_errors=True)
            report["local_dir_removed"] = 1
        except Exception as e:
            logger.warning("删本地目录失败 %s: %s", local_dir, e)
            report["local_dir_removed"] = f"error: {e}"
    else:
        report["local_dir_removed"] = 0

    # 4. Redis 任务元数据（可选）
    if drop_task_meta:
        try:
            q = get_queue()
            key = q._meta_key(f"ingest-{article_id}")  # noqa: SLF001
            q.client.delete(key)
            report["task_meta_removed"] = 1
        except Exception as e:
            logger.warning("删 Redis task meta 失败 article_id=%s: %s", article_id, e)
            report["task_meta_removed"] = f"error: {e}"

    logger.info("purge_article article_id=%s result=%s", article_id, report)
    return report


def purge_video_path(video_path: str) -> dict:
    """path/upload 入库的 force 重跑：按 video_path 删帧向量。

    不碰 article_store / clip_store / COS —— 这两类数据和 article_id 绑定，
    而 path 入库没有 article_id（video_path 的父目录名可能不是 sha1 前缀）。
    """
    report: dict[str, int | str] = {"video_path": video_path}
    try:
        n = get_vector_store().delete_for_video(video_path)
        report["frames_deleted"] = n
    except Exception as e:
        logger.warning("删 clipquery_frames 失败 video_path=%s: %s", video_path, e)
        report["frames_deleted"] = f"error: {e}"
    logger.info("purge_video_path video_path=%s result=%s", video_path, report)
    return report
