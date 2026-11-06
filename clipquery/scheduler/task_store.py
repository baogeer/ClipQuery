"""异步任务在 OpenSearch 上的状态管理。

索引 clipquery_tasks 文档：
    {
        "task_id":         "ingest-a1b2c3d4e5f6",
        "task_type":       "ingest_article",
        "payload":         {"article_url": "..."},
        "state":           "queued" | "in_progress" | "done" | "failed",
        "owner_worker_id": "host-pid-rand" | null,
        "created_at":      "...",
        "heartbeat_at":    "...",
        "finished_at":     "..." | null,
        "result":          {...} | null,
        "error":           "..." | null,
        "retry_count":     0,
    }

认领/完成都用 OpenSearch 的 **乐观锁**（_seq_no + _primary_term）：
- claim(): if_seq_no + if_primary_term CAS，谁先写入谁拿到
- reclaim_stale(): 对 heartbeat 过期的 in_progress 任务 CAS 改回 queued
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from opensearchpy import OpenSearch, RequestsHttpConnection
from opensearchpy.exceptions import ConflictError, NotFoundError

from ..config import settings


class TaskState:
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


def _index_body() -> dict:
    return {
        "mappings": {
            "properties": {
                "task_id":         {"type": "keyword"},
                "task_type":       {"type": "keyword"},
                "payload":         {"type": "object", "enabled": False},  # 不索引，原样存
                "state":           {"type": "keyword"},
                "owner_worker_id": {"type": "keyword"},
                "created_at":      {"type": "date"},
                "heartbeat_at":    {"type": "date"},
                "finished_at":     {"type": "date"},
                "result":          {"type": "object", "enabled": False},
                "error":           {"type": "text"},
                "retry_count":     {"type": "integer"},
            }
        }
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self) -> None:
        self.index = settings.opensearch_task_index
        self.client = OpenSearch(
            hosts=[{"host": settings.opensearch_host, "port": settings.opensearch_port}],
            http_auth=(settings.opensearch_username, settings.opensearch_password),
            use_ssl=settings.opensearch_use_ssl,
            verify_certs=settings.opensearch_verify_certs,
            ssl_show_warn=False,
            connection_class=RequestsHttpConnection,
            timeout=15,
            max_retries=3,
            retry_on_timeout=True,
        )
        self._ensure_index()

    def _ensure_index(self) -> None:
        if not self.client.indices.exists(index=self.index):
            self.client.indices.create(index=self.index, body=_index_body())

    # ---------- 读 ----------
    def get(self, task_id: str) -> dict | None:
        try:
            res = self.client.get(index=self.index, id=task_id)
        except NotFoundError:
            return None
        src = res.get("_source") or {}
        src["_seq_no"] = res["_seq_no"]
        src["_primary_term"] = res["_primary_term"]
        return src

    def get_source(self, task_id: str) -> dict | None:
        doc = self.get(task_id)
        if not doc:
            return None
        doc.pop("_seq_no", None)
        doc.pop("_primary_term", None)
        return doc

    # ---------- 入队 ----------
    def enqueue(
        self,
        task_id: str,
        task_type: str,
        payload: dict[str, Any],
        owner_hint: str | None = None,
    ) -> dict:
        """幂等入队：已存在且未终态则返回现有 doc，否则新建 queued 任务。"""
        existing = self.get_source(task_id)
        if existing and existing.get("state") in (TaskState.QUEUED, TaskState.IN_PROGRESS):
            return existing

        now = _now_iso()
        doc = {
            "task_id":         task_id,
            "task_type":       task_type,
            "payload":         payload,
            "state":           TaskState.QUEUED,
            "owner_worker_id": owner_hint,
            "created_at":      now,
            "heartbeat_at":    now,
            "finished_at":     None,
            "result":          None,
            "error":           None,
            "retry_count":     (existing or {}).get("retry_count", 0),
        }
        self.client.index(index=self.index, id=task_id, body=doc, refresh=True)
        return doc

    # ---------- CAS 认领 ----------
    def claim(self, task_id: str, worker_id: str) -> dict | None:
        """乐观锁认领：只把 queued 的任务改成 in_progress。成功返回新 doc，失败返回 None。"""
        doc = self.get(task_id)
        if not doc:
            return None
        if doc.get("state") != TaskState.QUEUED:
            return None

        updated = {
            **{k: v for k, v in doc.items() if not k.startswith("_")},
            "state":           TaskState.IN_PROGRESS,
            "owner_worker_id": worker_id,
            "heartbeat_at":    _now_iso(),
        }
        try:
            self.client.index(
                index=self.index,
                id=task_id,
                body=updated,
                if_seq_no=doc["_seq_no"],
                if_primary_term=doc["_primary_term"],
                refresh=True,
            )
        except ConflictError:
            return None
        return updated

    # ---------- 心跳 ----------
    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        """只刷新 heartbeat_at。被别的 worker 抢走时返回 False。"""
        doc = self.get(task_id)
        if not doc or doc.get("owner_worker_id") != worker_id:
            return False
        try:
            self.client.update(
                index=self.index,
                id=task_id,
                body={"doc": {"heartbeat_at": _now_iso()}},
                refresh=False,
                retry_on_conflict=3,
            )
            return True
        except Exception:
            return False

    # ---------- 完成 ----------
    def mark_done(self, task_id: str, result: dict | None = None) -> None:
        self.client.update(
            index=self.index,
            id=task_id,
            body={
                "doc": {
                    "state":        TaskState.DONE,
                    "result":       result,
                    "finished_at":  _now_iso(),
                    "heartbeat_at": _now_iso(),
                }
            },
            refresh=True,
            retry_on_conflict=3,
        )

    def mark_failed(self, task_id: str, error: str) -> None:
        self.client.update(
            index=self.index,
            id=task_id,
            body={
                "doc": {
                    "state":        TaskState.FAILED,
                    "error":        error[:5000],
                    "finished_at":  _now_iso(),
                    "heartbeat_at": _now_iso(),
                }
            },
            refresh=True,
            retry_on_conflict=3,
        )

    # ---------- 失败任务回收 ----------
    def reclaim_stale(self, ttl_seconds: int | None = None, limit: int = 50) -> list[dict]:
        """扫 in_progress 但心跳过期的任务，CAS 改回 queued。返回被回收的 task 列表。"""
        ttl = ttl_seconds or settings.scheduler_task_ttl
        threshold = datetime.now(timezone.utc) - timedelta(seconds=ttl)
        body = {
            "size": limit,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"state": TaskState.IN_PROGRESS}},
                        {"range": {"heartbeat_at": {"lt": threshold.isoformat()}}},
                    ]
                }
            },
        }
        res = self.client.search(index=self.index, body=body)
        reclaimed: list[dict] = []
        for hit in res["hits"]["hits"]:
            src = hit["_source"]
            src["_seq_no"] = hit["_seq_no"]
            src["_primary_term"] = hit["_primary_term"]
            updated = {
                **{k: v for k, v in src.items() if not k.startswith("_")},
                "state":           TaskState.QUEUED,
                "owner_worker_id": None,
                "heartbeat_at":    _now_iso(),
                "retry_count":     (src.get("retry_count") or 0) + 1,
            }
            try:
                self.client.index(
                    index=self.index,
                    id=src["task_id"],
                    body=updated,
                    if_seq_no=src["_seq_no"],
                    if_primary_term=src["_primary_term"],
                    refresh=True,
                )
                reclaimed.append(updated)
            except ConflictError:
                continue
        return reclaimed


@lru_cache(maxsize=1)
def get_task_store() -> TaskStore:
    return TaskStore()
