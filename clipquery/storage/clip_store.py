"""clipquery_clips 索引：存视频片段的 COS URL 与元数据。

一个文档 = 一个可播放的 MP4 片段。
"""
from __future__ import annotations

from functools import lru_cache

from opensearchpy import OpenSearch, RequestsHttpConnection
from opensearchpy.helpers import bulk

from ..config import settings


def _clip_index_body() -> dict:
    return {
        "mappings": {
            "properties": {
                "clip_id":      {"type": "keyword"},
                "article_id":   {"type": "keyword"},
                "index":        {"type": "integer"},
                "start_time":   {"type": "float"},
                "end_time":     {"type": "float"},
                "duration":     {"type": "float"},
                "clip_url":     {"type": "keyword"},   # COS 片段 MP4
                "source_video_url": {"type": "keyword"},  # 原片 COS URL
                "article_url":  {"type": "keyword"},   # CGTN 原文详情页
                "title":        {"type": "text"},
                "thumbnail":    {"type": "keyword"},   # 片段首帧缩略图 URL
                "ingested_at":  {"type": "date"},
            }
        }
    }


class ClipStore:
    def __init__(self) -> None:
        self.index = settings.opensearch_clip_index
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
            self.client.indices.create(index=self.index, body=_clip_index_body())

    def upsert_many(self, docs: list[dict]) -> None:
        if not docs:
            return
        actions = [
            {"_op_type": "index", "_index": self.index, "_id": d["clip_id"], "_source": d}
            for d in docs
        ]
        bulk(self.client, actions, chunk_size=settings.opensearch_bulk_size, refresh=True)

    def get_many(self, clip_ids: list[str]) -> dict[str, dict]:
        if not clip_ids:
            return {}
        res = self.client.mget(index=self.index, body={"ids": clip_ids})
        out: dict[str, dict] = {}
        for item in res.get("docs", []):
            if item.get("found"):
                out[item["_id"]] = item.get("_source", {})
        return out

    def count_for_article(self, article_id: str) -> int:
        body = {"query": {"term": {"article_id": article_id}}}
        return int(self.client.count(index=self.index, body=body).get("count", 0))

    def delete_for_article(self, article_id: str) -> int:
        """按 article_id 批量删片段元数据。返回删除条数。"""
        body = {"query": {"term": {"article_id": article_id}}}
        res = self.client.delete_by_query(
            index=self.index, body=body, refresh=True, conflicts="proceed",
        )
        return int(res.get("deleted", 0))


@lru_cache(maxsize=1)
def get_clip_store() -> ClipStore:
    return ClipStore()
