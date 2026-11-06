"""分布式任务调度。

设计见 docs/架构文档.md 分布式调度章节。对外接口：

    from clipquery.scheduler import WorkerRuntime, get_runtime

    runtime = WorkerRuntime.start()      # 注册 + 心跳 + APScheduler 一条龙
    runtime.is_owner(key)                # 判定 key 是否属于本 worker
    runtime.shutdown()                   # 优雅停机
"""
from .ring import ConsistentHashRing
from .runtime import WorkerRuntime, get_runtime
from .task_store import TaskState, TaskStore, get_task_store
from .worker_registry import WorkerRegistry, get_worker_registry

__all__ = [
    "ConsistentHashRing",
    "TaskState",
    "TaskStore",
    "WorkerRegistry",
    "WorkerRuntime",
    "get_runtime",
    "get_task_store",
    "get_worker_registry",
]
