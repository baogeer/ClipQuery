"""worker 节点在 OpenSearch 上的注册 + 心跳。

索引 clipquery_workers 的文档：
    {
        "worker_id":    "host-pid-rand",
        "host":         "cos-node-1",
        "pid":          12345,
        "started_at":   "2026-11-06T09:00:00Z",
        "heartbeat_at": "2026-11-06T09:05:00Z",
    }

活跃成员 = heartbeat_at > now - worker_ttl 的全部 doc。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache

from opensearchpy import OpenSearch, RequestsHttpConnection

from ..config import settings


def _index_body() -> dict:
    return {
        "mappings": {
            "properties": {
                "worker_id":    {"type": "keyword"},
                "host":         {"type": "keyword"},
                "pid":          {"type": "integer"},
                "started_at":   {"type": "date"},
                "heartbeat_at": {"type": "date"},
            }
        }
    }


class WorkerRegistry:
    def __init__(self) -> None:
        self.index = settings.opensearch_worker_index
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

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def register(self, worker_id: str, host: str, pid: int) -> None:
        now = self._now_iso()
        self.client.index(
            index=self.index,
            id=worker_id,
            body={
                "worker_id":    worker_id,
                "host":         host,
                "pid":          pid,
                "started_at":   now,
                "heartbeat_at": now,
            },
            refresh=False,
        )

    def heartbeat(self, worker_id: str) -> None:
        """只刷新 heartbeat_at 字段，不覆盖其他字段。"""
        self.client.update(
            index=self.index,
            id=worker_id,
            body={"doc": {"heartbeat_at": self._now_iso()}},
            refresh=False,
            retry_on_conflict=3,
        )

    def deregister(self, worker_id: str) -> None:
        """优雅停机时调用，立刻从环中剔除。"""
        try:
            self.client.delete(index=self.index, id=worker_id, refresh=False)
        except Exception:
            pass

    def alive_workers(self, ttl_seconds: int | None = None) -> list[str]:
        """返回心跳未过期的 worker_id 列表（已排序，保证 ring 构建稳定）。"""
        ttl = ttl_seconds or settings.scheduler_worker_ttl
        threshold = datetime.now(timezone.utc) - timedelta(seconds=ttl)
        body = {
            "size": 1000,
            "_source": ["worker_id"],
            "query": {
                "range": {"heartbeat_at": {"gte": threshold.isoformat()}}
            },
        }
        res = self.client.search(index=self.index, body=body)
        ids = [h["_source"]["worker_id"] for h in res["hits"]["hits"]]
        return sorted(ids)


@lru_cache(maxsize=1)
def get_worker_registry() -> WorkerRegistry:
    return WorkerRegistry()
