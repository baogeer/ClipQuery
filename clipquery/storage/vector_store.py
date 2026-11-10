from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np
from opensearchpy import OpenSearch, RequestsHttpConnection
from opensearchpy.helpers import bulk

from ..config import settings


@dataclass
class VectorHit:
    id: str
    score: float
    metadata: dict[str, Any]


def _index_body() -> dict:
    return {
        "settings": {
            "index": {
                "knn": True,
                "knn.algo_param.ef_search": settings.opensearch_knn_ef_search,
            }
        },
        "mappings": {
            "properties": {
                "embedding": {
                    "type": "knn_vector",
                    "dimension": settings.embedding_dim,
                    "method": {
                        "name": "hnsw",
                        "space_type": settings.opensearch_knn_space_type,
                        "engine": settings.opensearch_knn_engine,
                        "parameters": {"ef_construction": 256, "m": 16},
                    },
                },
                "video_path": {"type": "keyword"},        # 本地绝对路径，仅 path/upload 入库场景写入
                "source_video_url": {"type": "keyword"},  # 原片 COS URL（CGTN 流程写入）
                "article_id": {"type": "keyword"},
                "clip_id": {"type": "keyword"},
                "frame_index": {"type": "integer"},
                "timestamp": {"type": "float"},
                "thumbnail": {"type": "keyword"},
                "ingested_at": {"type": "date"},
            }
        },
    }


class VectorStore:
    """AWS OpenSearch 向量 + 元数据存储。

    一个 index 同时承载 knn_vector 字段和业务元数据，查询一步到位。
    """

    def __init__(self) -> None:
        self.index = settings.opensearch_index
        self.client = OpenSearch(
            hosts=[{"host": settings.opensearch_host, "port": settings.opensearch_port}],
            http_auth=(settings.opensearch_username, settings.opensearch_password),
            use_ssl=settings.opensearch_use_ssl,
            verify_certs=settings.opensearch_verify_certs,
            ssl_show_warn=False,
            connection_class=RequestsHttpConnection,
            timeout=30,
            max_retries=3,
            retry_on_timeout=True,
        )
        self._ensure_index()

    def _ensure_index(self) -> None:
        if not self.client.indices.exists(index=self.index):
            self.client.indices.create(index=self.index, body=_index_body())

    def add(
        self,
        ids: list[str],
        embeddings: np.ndarray,
        metadatas: list[dict],
    ) -> None:
        if not ids:
            return
        actions = []
        for _id, vec, meta in zip(ids, embeddings, metadatas):
            doc = {
                "embedding": vec.tolist(),
                **meta,
            }
            actions.append({"_op_type": "index", "_index": self.index, "_id": _id, "_source": doc})
        bulk(self.client, actions, chunk_size=settings.opensearch_bulk_size, refresh=False)

    def refresh(self) -> None:
        self.client.indices.refresh(index=self.index)

    def query(self, embedding: np.ndarray, top_k: int = 20) -> list[VectorHit]:
        if embedding.ndim == 2:
            embedding = embedding[0]
        body = {
            "size": top_k,
            "_source": {"excludes": ["embedding"]},
            "query": {
                "knn": {
                    "embedding": {
                        "vector": embedding.tolist(),
                        "k": top_k,
                    }
                }
            },
        }
        res = self.client.search(index=self.index, body=body)
        hits_raw = res.get("hits", {}).get("hits", [])
        hits = []
        for h in hits_raw:
            hits.append(VectorHit(
                id=h["_id"],
                score=float(h.get("_score", 0.0)),
                metadata=h.get("_source", {}) or {},
            ))
        return hits

    def count(self) -> int:
        res = self.client.count(index=self.index)
        return int(res.get("count", 0))

    def video_frame_count(self, video_path: str) -> int:
        body = {"query": {"term": {"video_path": video_path}}}
        res = self.client.count(index=self.index, body=body)
        return int(res.get("count", 0))

    def frame_count_for_article(self, article_id: str) -> int:
        body = {"query": {"term": {"article_id": article_id}}}
        res = self.client.count(index=self.index, body=body)
        return int(res.get("count", 0))

    def delete_for_article(self, article_id: str) -> int:
        """按 article_id 批量删帧向量。返回删除条数。"""
        body = {"query": {"term": {"article_id": article_id}}}
        res = self.client.delete_by_query(
            index=self.index, body=body, refresh=True, conflicts="proceed",
        )
        return int(res.get("deleted", 0))

    def delete_for_video(self, video_path: str) -> int:
        """按 video_path 批量删帧向量（path/upload 入库 force 重跑用）。"""
        body = {"query": {"term": {"video_path": video_path}}}
        res = self.client.delete_by_query(
            index=self.index, body=body, refresh=True, conflicts="proceed",
        )
        return int(res.get("deleted", 0))


@lru_cache(maxsize=1)
def get_vector_store() -> VectorStore:
    return VectorStore()
