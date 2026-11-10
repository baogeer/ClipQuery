"""article 元数据存储：OpenSearch `clipquery_articles` 索引。

索引里存 info.json 的内容外加 COS URL，用来：
- 文章级检索（按 id / url / title）
- 片段命中后反查文章元数据（title、COS 视频 URL 等）
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from opensearchpy import OpenSearch, RequestsHttpConnection

from ..config import settings


def _article_index_body() -> dict:
    return {
        "mappings": {
            "properties": {
                "id":            {"type": "keyword"},
                "url":           {"type": "keyword"},
                "title":         {"type": "text"},
                "video_m3u8":    {"type": "keyword"},
                "paragraphs":    {"type": "text"},
                "mp4_file":      {"type": "keyword"},
                "audio_file":    {"type": "keyword"},
                "srt_file":      {"type": "keyword"},
                "cos_video_url": {"type": "keyword"},
                "cos_audio_url": {"type": "keyword"},
                "cos_srt_url":   {"type": "keyword"},
                "cos_info_url":  {"type": "keyword"},
                "ingested_at":   {"type": "date"},
            }
        }
    }


class ArticleStore:
    """文章元数据存储。索引名默认 clipquery_articles。"""

    def __init__(self) -> None:
        self.index = settings.opensearch_article_index
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
            self.client.indices.create(index=self.index, body=_article_index_body())

    def upsert(self, article_id: str, doc: dict[str, Any]) -> None:
        """按 article_id 幂等写入 / 更新。"""
        self.client.index(index=self.index, id=article_id, body=doc, refresh=True)

    def get(self, article_id: str) -> dict | None:
        res = self.client.get(index=self.index, id=article_id, ignore=[404])
        if not res.get("found"):
            return None
        return res.get("_source")

    def exists(self, article_id: str) -> bool:
        return bool(self.client.exists(index=self.index, id=article_id))

    def delete(self, article_id: str) -> bool:
        """删除单条文章元数据。不存在时返回 False，不抛错。"""
        res = self.client.delete(index=self.index, id=article_id, ignore=[404], refresh=True)
        return res.get("result") == "deleted"

    def list(
        self,
        offset: int = 0,
        limit: int = 20,
        q: str | None = None,
        fields: list[str] | None = None,
    ) -> tuple[list[dict], int]:
        """按 ingested_at 倒序返回文章列表。

        - q: 非空则按 title 做 match 查询；空则返回全部。
        - fields: _source 字段白名单；None 则返回完整 _source。
        - 返回 (hits, total)。hits 每条含 _id 回填到字段 "id"。
        """
        if q and q.strip():
            query: dict[str, Any] = {"match": {"title": q.strip()}}
        else:
            query = {"match_all": {}}

        body: dict[str, Any] = {
            "query": query,
            "from":  offset,
            "size":  limit,
            "sort":  [{"ingested_at": {"order": "desc", "missing": "_last"}}],
            "track_total_hits": True,
        }
        if fields is not None:
            body["_source"] = fields

        res = self.client.search(index=self.index, body=body)
        hits = res.get("hits", {}).get("hits", []) or []
        total = res.get("hits", {}).get("total", {}).get("value", 0)
        docs: list[dict] = []
        for h in hits:
            src = dict(h.get("_source") or {})
            src.setdefault("id", h.get("_id"))
            docs.append(src)
        return docs, int(total)


@lru_cache(maxsize=1)
def get_article_store() -> ArticleStore:
    return ArticleStore()
