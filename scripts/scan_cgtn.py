"""抓取脚本：扫 CGTN transcript 列表页 → 把新文章入 Redis 队列。

典型用法（你每天 / 每小时 / 想到的时候跑一次）：

    python scripts/scan_cgtn.py

脚本行为：
    1) 拉 https://www.cgtn.com/transcript 列表页。
    2) 对每个 URL 走 clipquery.task.dedupe 三道闸（Redis + OpenSearch 已入库跳过）。
    3) 剩下的 URL LPUSH 到 Redis 队列 ClipQuery:ingest:queue。
    4) 打印一行统计后退出。

**不执行任务**，只入队。执行请跑 scripts/consume_one.py。

退出码：
    0  正常（即使列表页为空）
    1  拉列表页失败或网络不通
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clipquery.task.cgtn import run_once  # noqa: E402


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="扫 CGTN 列表 → 把新文章入 Redis 队列")
    parser.add_argument("-v", "--verbose", action="store_true", help="开 DEBUG 日志")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    report = run_once()
    print(f"[DONE] {report.as_log_line()}")
    if report.errors:
        print(f"[ERRORS] {report.errors}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
