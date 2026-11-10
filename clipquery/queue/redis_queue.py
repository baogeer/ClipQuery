"""Redis 任务队列 + 任务元数据存储。

设计：
- **队列**：list `ClipQuery:ingest:queue`。API LPUSH 任务 JSON，Worker BRPOP 消费。
- **元数据**：hash `ClipQuery:task:<task_id>`，字段：
    task_id, task_type, payload (json), state, owner,
    created_at, updated_at, finished_at, result (json), error, retry_count
  TTL 由 config.redis_task_meta_ttl 控制（默认 24h），用于前端轮询。
- **分布式锁**：`ClipQuery:lock:<task_id>` SET NX PX。Worker 执行前取锁，
  同一 task_id 并发时只有一人能执行。锁的 value 是 worker_id，释放时用 Lua
  脚本校验避免误删别人的锁。

**所有 Redis key 以 `ClipQuery:` 开头**（Redis 大小写敏感，和 COS 前缀对齐）。

定时能力：当前服务进程不跑任何定时任务，调度由本地脚本自行安排
（例如 crontab 定时跑 scripts/scan_cgtn.py）。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import redis

from ..config import settings


class TaskState:
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 释放锁的 Lua：value 匹配才删，避免误删别人的锁
_RELEASE_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# 刷新锁 TTL：value 匹配才延期
_REFRESH_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""


class RedisQueue:
    """Redis 任务队列 + 元数据 + 分布式锁的瘦封装。进程内单例。"""

    def __init__(self) -> None:
        self.client: redis.Redis = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            username=settings.redis_username,
            password=settings.redis_password,
            ssl=settings.redis_ssl,
            decode_responses=True,       # str 不 bytes，省到处 .decode()
            socket_connect_timeout=5,
            socket_keepalive=True,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        self.queue_key = settings.redis_ingest_queue_key
        self.meta_prefix = settings.redis_task_meta_prefix
        self.meta_ttl = settings.redis_task_meta_ttl

        self._release_lock = self.client.register_script(_RELEASE_LOCK_LUA)
        self._refresh_lock = self.client.register_script(_REFRESH_LOCK_LUA)

    # ---------- 元数据 key ----------
    def _meta_key(self, task_id: str) -> str:
        return f"{self.meta_prefix}{task_id}"

    def _lock_key(self, task_id: str) -> str:
        return f"ClipQuery:lock:{task_id}"

    # ---------- 入队 ----------
    def enqueue(
        self,
        task_id: str,
        task_type: str,
        payload: dict[str, Any],
        *,
        force: bool = False,
    ) -> dict:
        """幂等入队。
        - force=False：task_id 已存在且未终态（queued/in_progress） → 返回现状，不再 LPUSH。
        - force=True：无视现状强制 LPUSH 新任务 + 覆盖 meta 为 queued；payload 自动
          写入 `_force=true` 让执行端识别。调用方应该已经在入队前完成旧数据清理
          （见 clipquery.task.purge.purge_article）。
        """
        existing = self.get(task_id)
        if not force and existing and existing.get("state") in (TaskState.QUEUED, TaskState.IN_PROGRESS):
            return existing

        payload_to_store = dict(payload)
        if force:
            payload_to_store["_force"] = True

        now = _now_iso()
        meta = {
            "task_id":     task_id,
            "task_type":   task_type,
            "payload":     json.dumps(payload_to_store, ensure_ascii=False),
            "state":       TaskState.QUEUED,
            "owner":       "",
            "created_at":  now,
            "updated_at":  now,
            "finished_at": "",
            "result":      "",
            "error":       "",
            "retry_count": str((existing or {}).get("retry_count", 0)),
        }
        meta_key = self._meta_key(task_id)
        pipe = self.client.pipeline()
        pipe.hset(meta_key, mapping=meta)
        pipe.expire(meta_key, self.meta_ttl)
        pipe.lpush(self.queue_key, json.dumps({
            "task_id":   task_id,
            "task_type": task_type,
            "payload":   payload_to_store,
        }, ensure_ascii=False))
        pipe.execute()
        return self.get(task_id) or meta

    # ---------- 消费 ----------
    def pop(self, timeout: int | None = None) -> dict | None:
        """BRPOP 一条任务。超时返回 None。"""
        t = timeout if timeout is not None else settings.redis_brpop_timeout
        res = self.client.brpop(self.queue_key, timeout=t)
        if not res:
            return None
        _, raw = res
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ---------- 元数据 ----------
    def get(self, task_id: str) -> dict | None:
        raw = self.client.hgetall(self._meta_key(task_id))
        if not raw:
            return None
        out = dict(raw)
        for k in ("payload", "result"):
            if out.get(k):
                try:
                    out[k] = json.loads(out[k])
                except json.JSONDecodeError:
                    pass
            else:
                out[k] = None
        try:
            out["retry_count"] = int(out.get("retry_count") or 0)
        except (TypeError, ValueError):
            out["retry_count"] = 0
        return out

    def _update_meta(self, task_id: str, fields: dict[str, Any]) -> None:
        meta_key = self._meta_key(task_id)
        fields = {k: ("" if v is None else v) for k, v in fields.items()}
        pipe = self.client.pipeline()
        pipe.hset(meta_key, mapping={k: str(v) if not isinstance(v, str) else v for k, v in fields.items()})
        pipe.expire(meta_key, self.meta_ttl)
        pipe.execute()

    def mark_in_progress(self, task_id: str, worker_id: str) -> None:
        self._update_meta(task_id, {
            "state":      TaskState.IN_PROGRESS,
            "owner":      worker_id,
            "updated_at": _now_iso(),
        })

    def mark_done(self, task_id: str, result: dict | None = None) -> None:
        self._update_meta(task_id, {
            "state":       TaskState.DONE,
            "result":      json.dumps(result or {}, ensure_ascii=False),
            "updated_at":  _now_iso(),
            "finished_at": _now_iso(),
        })

    def mark_failed(self, task_id: str, error: str) -> None:
        self._update_meta(task_id, {
            "state":       TaskState.FAILED,
            "error":       (error or "")[:5000],
            "updated_at":  _now_iso(),
            "finished_at": _now_iso(),
        })
        # 失败任务永久保留，便于事后排查
        self.client.persist(self._meta_key(task_id))

    def delete_meta(self, task_id: str) -> None:
        """删除任务 meta hash。成功完成的任务调用，去重改由下游存储兜底。"""
        self.client.delete(self._meta_key(task_id))

    def incr_retry(self, task_id: str) -> int:
        """失败重试时调用。返回新 retry_count。"""
        meta_key = self._meta_key(task_id)
        n = self.client.hincrby(meta_key, "retry_count", 1)
        self.client.expire(meta_key, self.meta_ttl)
        return int(n)

    # ---------- 分布式锁 ----------
    def acquire_lock(self, task_id: str, worker_id: str, ttl_seconds: int) -> bool:
        """SET NX PX。成功返回 True。"""
        ok = self.client.set(
            self._lock_key(task_id),
            worker_id,
            nx=True,
            px=ttl_seconds * 1000,
        )
        return bool(ok)

    def refresh_lock(self, task_id: str, worker_id: str, ttl_seconds: int) -> bool:
        res = self._refresh_lock(
            keys=[self._lock_key(task_id)],
            args=[worker_id, ttl_seconds * 1000],
        )
        return bool(res)

    def release_lock(self, task_id: str, worker_id: str) -> bool:
        res = self._release_lock(
            keys=[self._lock_key(task_id)],
            args=[worker_id],
        )
        return bool(res)

    # ---------- 健康 ----------
    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def queue_length(self) -> int:
        try:
            return int(self.client.llen(self.queue_key))
        except Exception:
            return -1


def new_worker_id(role: str = "worker") -> str:
    """简单稳定的 worker id，用来写入锁和元数据 owner 字段。"""
    import os
    import socket
    host = socket.gethostname() or "unknown"
    return f"{role}-{host}-{os.getpid()}-{uuid.uuid4().hex[:4]}"


@lru_cache(maxsize=1)
def get_queue() -> RedisQueue:
    return RedisQueue()


__all__ = [
    "RedisQueue",
    "TaskState",
    "get_queue",
    "new_worker_id",
]
