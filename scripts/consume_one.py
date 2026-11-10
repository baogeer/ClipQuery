"""处理脚本：从 Redis 队列拿一条任务执行完退出。

典型用法：

    # 处理一条就退出
    python scripts/consume_one.py

    # 把队列里的任务全部处理完（遇到空队列就退）
    python scripts/consume_one.py --all

    # 自定义 BRPOP 等待（秒），默认 5
    python scripts/consume_one.py --timeout 30

脚本行为：
    1) 连接 Redis，BRPOP 一条任务（最多阻塞 timeout 秒）。
    2) 走 clipquery.task.executor.execute_task：
        - 获取 Redis 分布式锁 ClipQuery:lock:<task_id>
        - 根据 task_type 执行（爬虫 / 切片 / CLIP 编码 / 三索引写入）
        - mark_done / mark_failed
        - 释放锁
    3) 退出，或者（--all 模式）继续下一轮直到队列空。

**同一时刻只建议跑一个** consume_one.py 进程（CLIP + Whisper 占内存）。
分布式锁保证就算你同时跑多个也不会冲突，但 OOM 风险大。

退出码：
    0  成功（可能处理了 0 条、1 条、或多条）
    1  执行中有任务 mark_failed（你可以看脚本输出里的任务 id 继续排查）
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clipquery.queue import new_worker_id  # noqa: E402
from clipquery.task.executor import consume_once  # noqa: E402


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="从 Redis 队列拿一条任务执行")
    parser.add_argument(
        "--all", action="store_true",
        help="处理完一条继续下一条，直到队列空。等价于 --count 0。",
    )
    parser.add_argument(
        "--count", "-n", type=int, default=1,
        help="最多处理多少条任务后退出（默认 1）。0 表示不限（直到队列空）。--all 等价 --count 0。",
    )
    parser.add_argument(
        "--timeout", type=int, default=5,
        help="BRPOP 最长阻塞秒数（默认 5）。队列为空且超过这个时间就退出。",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    worker_id = new_worker_id("script")

    # --all 覆盖为 0 = 不限；--count 必须 >= 0
    target = 0 if args.all else args.count
    if target < 0:
        parser.error("--count 必须 >= 0")

    processed = 0
    while target == 0 or processed < target:
        consumed = consume_once(worker_id, timeout=args.timeout)
        if consumed:
            processed += 1
        else:
            # BRPOP 超时：队列空，直接退
            break

    print(f"[DONE] processed={processed} target={'unlimited' if target == 0 else target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
