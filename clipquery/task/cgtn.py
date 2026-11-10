"""CGTN transcript 页面增量监控。

触发方式：
    命令行：python scripts/scan_cgtn.py
    或：  python -m clipquery.task.cgtn --run-once

本模块只做"扫列表 → 入 Redis 队列"，不执行任务。执行请跑
scripts/consume_one.py。

分布式语义：
    - 已入库的 URL 查 clipquery_articles 跳过（幂等）。
    - 新增 URL LPUSH 到 Redis 队列 ClipQuery:ingest:queue；
      由 scripts/consume_one.py 消费。
    - 执行时的互斥靠 Redis 锁 ClipQuery:lock:<task_id>。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from dataclasses import dataclass, field

from ..config import settings
from ..queue import get_queue
from ..website.cgtn import (
    crawl_transcript_page,
    url_hash_id,
)
from .dedupe import check_article_duplicate

logger = logging.getLogger("clipquery.task.cgtn")

TASK_TYPE = "ingest_cgtn_article"


@dataclass
class RunReport:
    listed: int = 0            # 列表页返回的 URL 总数
    deduped: int = 0           # 三道闸命中（已入库 / 已 queued / 已 done）
    enqueued: int = 0          # 新入队到 Redis 队列
    errors: list[str] = field(default_factory=list)

    def as_log_line(self) -> str:
        return (
            f"listed={self.listed} "
            f"deduped={self.deduped} "
            f"enqueued={self.enqueued}"
        )


def _task_id_for(article_id: str) -> str:
    return f"ingest-{article_id}"


def _scan_and_enqueue() -> RunReport:
    """核心：拉列表 → dedupe 三道闸 → 新 URL 入 Redis 队列。

    定时任务永远 force=False；force 重跑只能由人为跑 scripts/pipeline_one.py --force 触发。
    """
    report = RunReport()
    q = get_queue()

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

    for i, url in enumerate(links, 1):
        aid = url_hash_id(url)
        task_id = _task_id_for(aid)

        # 三道闸：Redis meta (queued/in_progress/done) + article_store.exists
        if check_article_duplicate(aid, task_id) is not None:
            report.deduped += 1
            continue

        logger.info("[%d/%d] 入队 url=%s id=%s", i, len(links), url, aid)
        q.enqueue(
            task_id=task_id,
            task_type=TASK_TYPE,
            payload={"article_url": url, "article_id": aid},
            force=False,
        )
        report.enqueued += 1

    logger.info("本轮完成 %s", report.as_log_line())
    return report


# ---------- 调度入口（供脚本或未来重新接回定时器用） ----------
def tick() -> None:
    """job 函数：扫列表 + 入队。异常不能冒出去。"""
    start = time.monotonic()
    try:
        report = _scan_and_enqueue()
        logger.info(
            "cgtn.tick 结束 耗时=%.1fs %s",
            time.monotonic() - start, report.as_log_line(),
        )
    except Exception:
        logger.error("cgtn.tick 未捕获异常:\n%s", traceback.format_exc())


# ---------- CLI 入口（独立进程调试用） ----------
def run_once() -> RunReport:
    """单次扫描 + 入队。不执行任务（执行由 Worker 消费端做）。"""
    return _scan_and_enqueue()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="CGTN transcript 增量监控任务")
    parser.add_argument("--run-once", action="store_true", help="只跑一次就退出")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    if args.run_once:
        report = run_once()
        print(f"[DONE] {report.as_log_line()}")
        if report.errors:
            print(f"[ERRORS] {report.errors}", file=sys.stderr)
        return 0

    logger.warning(
        "常驻模式已废弃。请手动跑 scripts/scan_cgtn.py，"
        "或在 cron / 系统计划任务里定时触发。"
    )
    logger.info("将进入每 %d 秒一次的简易常驻循环",
                settings.scheduler_cgtn_interval_seconds)
    try:
        while True:
            tick()
            time.sleep(settings.scheduler_cgtn_interval_seconds)
    except KeyboardInterrupt:
        logger.info("退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
