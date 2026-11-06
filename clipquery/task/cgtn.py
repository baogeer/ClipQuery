"""CGTN transcript 页面增量监控。

两种触发方式：
    A) uvicorn worker 内：由 WorkerRuntime 的 APScheduler 每 N 分钟调 tick(runtime)
    B) 命令行：python -m clipquery.task.cgtn --run-once  走单进程模式

分布式语义：
    - 所有 worker 都会跑 tick，但只处理 hash 环判定归属自己的 URL
    - 已入库的 URL 查 clipquery_articles 跳过
    - 新增 URL 入 clipquery_tasks（queued）；由本 worker 直接 claim 执行
    - 其他 worker 可以抢占超时 in_progress 任务（WorkerRuntime._reclaim_tick 做）
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..config import settings
from ..pipeline.indexer import ingest_video
from ..scheduler.task_store import TaskState, get_task_store
from ..storage.article_store import get_article_store
from ..website.cgtn import (
    crawl_transcript_page,
    process_one_article,
    url_hash_id,
)

if TYPE_CHECKING:
    from ..scheduler.runtime import WorkerRuntime

logger = logging.getLogger("clipquery.task.cgtn")

TASK_TYPE = "ingest_cgtn_article"


@dataclass
class RunReport:
    listed: int = 0            # 列表页返回的 URL 总数
    not_owned: int = 0         # ring 判定不归自己管
    already_ingested: int = 0  # article 索引已存在
    enqueued: int = 0          # 入 task 队列并由本 worker 执行
    processed: int = 0         # 成功入库
    failed: int = 0            # 本 worker 执行失败
    errors: list[str] = field(default_factory=list)

    def as_log_line(self) -> str:
        return (
            f"listed={self.listed} not_owned={self.not_owned} "
            f"already_ingested={self.already_ingested} "
            f"enqueued={self.enqueued} processed={self.processed} failed={self.failed}"
        )


def _task_id_for(article_id: str) -> str:
    return f"ingest-{article_id}"


def _is_already_ingested(article_id: str) -> bool:
    try:
        return get_article_store().exists(article_id)
    except Exception as e:
        logger.warning("查询 article 索引失败，按未入库处理: %s", e)
        return False


def _ingest_url(article_url: str, article_id: str) -> tuple[bool, dict | None]:
    """端到端处理一条 URL。返回 (成功?, 简要结果)。"""
    try:
        info = process_one_article(article_url)
    except Exception:
        logger.exception("process_one_article 抛错 url=%s", article_url)
        return False, None

    if info is None:
        return False, {"reason": "no_video_or_download_failed"}

    video_path = settings.news_dir / article_id / "video.mp4"
    if not video_path.exists():
        logger.warning("video.mp4 不存在，跳过向量入库: %s", video_path)
        return True, {"frames_written": 0, "note": "video_missing"}

    try:
        frames = ingest_video(video_path, article_id=article_id, show_progress=False)
        logger.info("向量入库完成 article_id=%s frames=%d", article_id, frames)
        return True, {"frames_written": frames}
    except Exception:
        logger.exception("ingest_video 抛错 article_id=%s", article_id)
        return False, None


def _process_with_task_store(
    article_url: str,
    article_id: str,
    worker_id: str,
) -> tuple[bool, str]:
    """用 clipquery_tasks 包一层：入队 → 认领 → 执行 → 落结果。

    返回 (是否入库成功, 结论标签)，用于 RunReport 统计。
    """
    store = get_task_store()
    task_id = _task_id_for(article_id)

    task = store.enqueue(
        task_id=task_id,
        task_type=TASK_TYPE,
        payload={"article_url": article_url, "article_id": article_id},
        owner_hint=worker_id,
    )
    if task.get("state") == TaskState.IN_PROGRESS:
        # 别的 worker 正在跑，不重复抢
        return False, "in_progress_elsewhere"

    claimed = store.claim(task_id, worker_id)
    if not claimed:
        return False, "claim_lost"

    ok, result = _ingest_url(article_url, article_id)
    if ok:
        store.mark_done(task_id, result=result)
        return True, "done"
    store.mark_failed(task_id, error=(result or {}).get("reason", "ingest_failed"))
    return False, "failed"


def _run_scan(runtime: "WorkerRuntime | None") -> RunReport:
    """核心扫描：拉列表 → ring 过滤 → 入 task 表 → 认领执行。

    runtime=None 时退化为"单进程模式"：所有 URL 都算自己的。
    """
    report = RunReport()

    try:
        links = crawl_transcript_page()
    except Exception as e:
        logger.exception("拉列表页失败")
        report.errors.append(f"list_page: {e}")
        return report

    report.listed = len(links)
    if not links:
        logger.info("列表页为空，本轮结束")
        return report

    worker_id = runtime.worker_id if runtime else "standalone"

    for i, url in enumerate(links, 1):
        aid = url_hash_id(url)

        # 1) ring 过滤：是别人的就跳
        if runtime is not None and not runtime.is_owner(aid):
            report.not_owned += 1
            continue

        # 2) 已入库跳过
        if _is_already_ingested(aid):
            report.already_ingested += 1
            continue

        logger.info("[%d/%d] 新增 url=%s id=%s", i, len(links), url, aid)
        report.enqueued += 1

        ok, _ = _process_with_task_store(url, aid, worker_id)
        if ok:
            report.processed += 1
        else:
            report.failed += 1
            report.errors.append(f"{aid}: {url}")

    logger.info("本轮完成 worker=%s %s", worker_id, report.as_log_line())
    return report


# ---------- APScheduler 入口（WorkerRuntime 调用） ----------
def tick(runtime: "WorkerRuntime | None" = None) -> None:
    """APScheduler 任务体。异常不能冒出去，否则会把 job 停掉。"""
    start = time.monotonic()
    try:
        report = _run_scan(runtime)
        logger.info(
            "cgtn.tick 结束 耗时=%.1fs %s",
            time.monotonic() - start, report.as_log_line(),
        )
    except Exception:
        logger.error("cgtn.tick 未捕获异常:\n%s", traceback.format_exc())


# ---------- CLI 入口（独立进程调试用） ----------
def run_once(use_ring: bool = False) -> RunReport:
    """单次扫描。use_ring=False 时忽略 hash 环，所有 URL 都算自己的（调试模式）。"""
    runtime = None
    if use_ring:
        from ..scheduler.runtime import _lazy_runtime
        runtime = _lazy_runtime()
    return _run_scan(runtime)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="CGTN transcript 增量监控任务")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="只跑一次就退出，不走 hash 环（单机调试）",
    )
    parser.add_argument(
        "--with-ring",
        action="store_true",
        help="启用 hash 环过滤（会往 clipquery_workers 注册本进程）",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    if args.run_once:
        report = run_once(use_ring=args.with_ring)
        print(f"[DONE] {report.as_log_line()}")
        if report.errors:
            print(f"[ERRORS] {report.errors}", file=sys.stderr)
        return 0 if report.failed == 0 else 1

    # 常驻模式：建议直接跑 API，WorkerRuntime 会自动调度 cgtn.tick。
    # 这里只保留一个简易常驻（兼容老 systemd 配置）。
    logger.warning(
        "单机常驻模式已弃用，请使用 uvicorn 启动 clipquery.api.main:app；"
        "每个 uvicorn worker 会自动注册到 hash 环并调度本任务。"
    )
    logger.info("将进入每 %d 分钟一次的简易常驻循环",
                settings.scheduler_cgtn_interval_minutes)
    try:
        while True:
            tick(None)
            time.sleep(settings.scheduler_cgtn_interval_minutes * 60)
    except KeyboardInterrupt:
        logger.info("退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
