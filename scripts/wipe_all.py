"""一键清理 Redis / OpenSearch / COS 的所有 ClipQuery 数据。

**危险操作，不可恢复**。本脚本直接执行、不弹确认。

清理范围：
    - Redis   : ClipQuery:ingest:queue
                ClipQuery:task:*
                ClipQuery:lock:*
    - OpenSearch : 删掉三个业务索引（下次程序启动自动重建 mapping）
                   clipquery_frames     (OpenSearch 索引名硬性要求小写)
                   clipquery_clips
                   clipquery_articles
    - COS     : 删 <cos_prefix>/* 下所有对象（原片/音频/字幕/片段/缩略图/info.json）

默认把三类都清。可用命令行开关只清一部分：
    python scripts/wipe_all.py                   # 全清
    python scripts/wipe_all.py --only redis
    python scripts/wipe_all.py --only opensearch
    python scripts/wipe_all.py --only cos
    python scripts/wipe_all.py --skip cos        # 清 Redis + OpenSearch，COS 不动

退出码：
    0  全部成功
    1  有部分失败（stderr 会列出错误；已清理的部分不会回滚）
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clipquery.config import settings  # noqa: E402

logger = logging.getLogger("wipe_all")


# ==================== Redis ====================
def wipe_redis() -> dict:
    """删 clipquery 相关的所有 Redis key。返回每类的删除计数。"""
    from clipquery.queue import get_queue

    q = get_queue()
    client = q.client

    patterns = [
        settings.redis_ingest_queue_key,                       # 队列单 key
        f"{settings.redis_task_meta_prefix}*",                 # 任务元数据
        "ClipQuery:lock:*",                                    # 分布式锁
    ]

    report: dict[str, int] = {}
    for pat in patterns:
        if "*" in pat:
            # SCAN + DEL（避免 KEYS 阻塞大库）
            count = 0
            for key in client.scan_iter(match=pat, count=500):
                client.delete(key)
                count += 1
            report[pat] = count
        else:
            report[pat] = int(client.delete(pat))
    return report


# ==================== OpenSearch ====================
def wipe_opensearch() -> dict:
    """删 clipquery 三个业务索引。下次程序启动会自动重建 mapping。"""
    # 懒加载：避免还没安装 opensearch 时 import 挂掉
    from opensearchpy import OpenSearch, RequestsHttpConnection

    client = OpenSearch(
        hosts=[{"host": settings.opensearch_host, "port": settings.opensearch_port}],
        http_auth=(settings.opensearch_username, settings.opensearch_password),
        use_ssl=settings.opensearch_use_ssl,
        verify_certs=settings.opensearch_verify_certs,
        ssl_show_warn=False,
        connection_class=RequestsHttpConnection,
        timeout=30,
    )

    indices = [
        settings.opensearch_index,          # clipquery_frames
        settings.opensearch_clip_index,     # clipquery_clips
        settings.opensearch_article_index,  # clipquery_articles
        settings.opensearch_subtitle_index, # clipquery_subtitles
    ]

    report: dict[str, str] = {}
    for idx in indices:
        res = client.indices.delete(index=idx, ignore=[404])
        if res.get("acknowledged"):
            report[idx] = "deleted"
        elif res.get("status") == 404:
            report[idx] = "not_exist"
        else:
            report[idx] = f"unknown: {res}"
    return report


# ==================== COS ====================
def wipe_cos() -> dict:
    """删 COS 上 <cos_prefix>/ 下所有对象。"""
    from clipquery import cos as cos_client

    prefix = settings.cos_prefix.strip("/") + "/"
    deleted = cos_client.delete_prefix(prefix)
    return {"prefix": prefix, "objects_deleted": deleted}


# ==================== 入口 ====================
def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="一键清理 ClipQuery 的所有数据")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--only",
        choices=["redis", "opensearch", "cos"],
        help="只清指定存储；不指定则全清。",
    )
    group.add_argument(
        "--skip",
        action="append",
        default=[],
        choices=["redis", "opensearch", "cos"],
        help="跳过指定存储（可多次指定）。",
    )
    args = parser.parse_args()

    _setup_logging()

    targets = {"redis", "opensearch", "cos"}
    if args.only:
        targets = {args.only}
    else:
        targets -= set(args.skip)

    logger.warning("=== WIPE START ===  targets=%s", sorted(targets))
    logger.warning("Redis  : %s:%s/%d  prefix=%s",
                   settings.redis_host, settings.redis_port, settings.redis_db,
                   settings.redis_task_meta_prefix)
    logger.warning("OpenSearch: %s  indices=[%s, %s, %s]",
                   settings.opensearch_host,
                   settings.opensearch_index,
                   settings.opensearch_clip_index,
                   settings.opensearch_article_index)
    logger.warning("COS    : bucket=%s  prefix=%s/",
                   settings.cos_bucket, settings.cos_prefix.strip("/"))

    exit_code = 0
    jobs = [
        ("redis",      wipe_redis),
        ("opensearch", wipe_opensearch),
        ("cos",        wipe_cos),
    ]
    for name, fn in jobs:
        if name not in targets:
            continue
        logger.info("[%s] 开始清理...", name)
        try:
            report = fn()
            logger.info("[%s] 完成 report=%s", name, report)
        except Exception as e:
            logger.exception("[%s] 失败: %s", name, e)
            exit_code = 1

    logger.warning("=== WIPE DONE === exit=%d", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
