"""WorkerRuntime：uvicorn worker 进程内的调度总装。

生命周期（和 FastAPI lifespan 对齐）：

    startup:
        1) 生成 worker_id = host-pid-rand
        2) 注册到 clipquery_workers 索引
        3) 启动心跳线程（每 10s 刷 heartbeat_at）
        4) 启动 APScheduler，注册所有定时任务
        5) 返回 runtime 单例

    shutdown:
        1) 停 APScheduler
        2) 停心跳线程
        3) 从 clipquery_workers 索引里 deregister

对外主要接口：
    - get_runtime() 返回当前进程的 WorkerRuntime（在 lifespan 里初始化）
    - runtime.is_owner(key) 问 hash 环「这个 key 归我管吗」
    - runtime.ring() 拿当前环快照（活跃 worker + 虚拟节点）
    - runtime.worker_id 本 worker 的稳定 id
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from functools import lru_cache

from ..config import settings
from .ring import ConsistentHashRing
from .task_store import get_task_store
from .worker_registry import get_worker_registry

logger = logging.getLogger("clipquery.scheduler")


def _build_worker_id() -> str:
    """worker_id 稳定但带随机后缀：host-pid-rand4。

    pid 在同一容器重启后会变，所以单看 pid 不稳；加 rand4 防止极端巧合碰撞。
    """
    host = socket.gethostname() or "unknown"
    pid = os.getpid()
    return f"{host}-{pid}-{uuid.uuid4().hex[:4]}"


class WorkerRuntime:
    """单实例。uvicorn 每个 worker 进程有自己的一份。"""

    def __init__(self) -> None:
        self.worker_id = _build_worker_id()
        self.host = socket.gethostname() or "unknown"
        self.pid = os.getpid()

        self._stop_evt = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._scheduler = None  # BlockingScheduler / BackgroundScheduler；懒加载
        self._started = False

        # 环快照缓存：避免每次 is_owner 都查 OpenSearch
        self._ring_cache: ConsistentHashRing | None = None
        self._ring_cached_at: float = 0.0
        self._ring_ttl_seconds = max(3, settings.scheduler_heartbeat_interval // 2)

    # ---------- 生命周期 ----------
    def start(self) -> "WorkerRuntime":
        if self._started:
            return self
        logger.info("WorkerRuntime starting worker_id=%s", self.worker_id)

        # 1. 注册
        registry = get_worker_registry()
        registry.register(self.worker_id, self.host, self.pid)

        # 2. 心跳线程
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"hb-{self.worker_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

        # 3. APScheduler（后台模式，不阻塞 uvicorn）
        from apscheduler.schedulers.background import BackgroundScheduler
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._register_jobs(self._scheduler)
        self._scheduler.start()

        self._started = True
        logger.info("WorkerRuntime started worker_id=%s", self.worker_id)
        return self

    def shutdown(self) -> None:
        if not self._started:
            return
        logger.info("WorkerRuntime shutting down worker_id=%s", self.worker_id)
        self._stop_evt.set()

        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass

        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5)

        try:
            get_worker_registry().deregister(self.worker_id)
        except Exception:
            logger.exception("deregister 失败")

        self._started = False
        logger.info("WorkerRuntime stopped worker_id=%s", self.worker_id)

    # ---------- Hash 环 ----------
    def ring(self, force_refresh: bool = False) -> ConsistentHashRing:
        """返回当前活跃 worker 构成的 hash 环。带短 TTL 缓存。"""
        now = time.monotonic()
        if (
            not force_refresh
            and self._ring_cache is not None
            and (now - self._ring_cached_at) < self._ring_ttl_seconds
        ):
            return self._ring_cache

        members = get_worker_registry().alive_workers()
        # 兜底：注册刚写入可能查不到自己（刷新延迟），强行把自己加进来
        if self.worker_id not in members:
            members.append(self.worker_id)
            members.sort()
        self._ring_cache = ConsistentHashRing(members, vnodes=settings.scheduler_vnodes)
        self._ring_cached_at = now
        return self._ring_cache

    def is_owner(self, key: str) -> bool:
        return self.ring().is_owner(key, self.worker_id)

    # ---------- 心跳 ----------
    def _heartbeat_loop(self) -> None:
        registry = get_worker_registry()
        interval = settings.scheduler_heartbeat_interval
        while not self._stop_evt.wait(interval):
            try:
                registry.heartbeat(self.worker_id)
            except Exception:
                logger.exception("心跳失败 worker_id=%s", self.worker_id)

    # ---------- 定时任务注册 ----------
    def _register_jobs(self, scheduler) -> None:
        from apscheduler.triggers.interval import IntervalTrigger

        # 1. CGTN transcript 扫描：每 N 分钟
        from ..task.cgtn import tick as cgtn_tick
        cgtn_minutes = settings.scheduler_cgtn_interval_minutes
        scheduler.add_job(
            cgtn_tick,
            trigger=IntervalTrigger(minutes=cgtn_minutes),
            id="cgtn-transcript-scan",
            name=f"CGTN transcript scan every {cgtn_minutes}m",
            max_instances=1,
            coalesce=True,
            kwargs={"runtime": self},
        )

        # 2. 失败任务回收：比心跳稍慢，全体 worker 都跑无妨（CAS 去重）
        scheduler.add_job(
            self._reclaim_tick,
            trigger=IntervalTrigger(seconds=max(60, settings.scheduler_task_ttl // 5)),
            id="task-reclaim",
            name="reclaim stale in_progress tasks",
            max_instances=1,
            coalesce=True,
        )

        # 3. 启动后立刻跑一次 CGTN（便于观察效果，不是补跑）
        scheduler.add_job(
            cgtn_tick,
            id="cgtn-initial",
            kwargs={"runtime": self},
        )

    def _reclaim_tick(self) -> None:
        try:
            reclaimed = get_task_store().reclaim_stale()
            if reclaimed:
                logger.info(
                    "[reclaim] worker=%s reclaimed=%d",
                    self.worker_id, len(reclaimed),
                )
        except Exception:
            logger.exception("reclaim_stale 失败")


_RUNTIME: WorkerRuntime | None = None


def get_runtime() -> WorkerRuntime:
    """只在当前进程内返回 runtime。未启动则抛错——必须在 FastAPI lifespan 中启动。"""
    if _RUNTIME is None:
        raise RuntimeError("WorkerRuntime 未启动。请在 FastAPI lifespan 中 await runtime.start()")
    return _RUNTIME


def set_runtime(runtime: WorkerRuntime) -> None:
    global _RUNTIME
    _RUNTIME = runtime


@lru_cache(maxsize=1)
def _lazy_runtime() -> WorkerRuntime:
    """lifespan 之外的场景（比如 CLI）用它来临时启一个。"""
    rt = WorkerRuntime().start()
    set_runtime(rt)
    return rt
