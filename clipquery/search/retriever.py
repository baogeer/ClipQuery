from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO

import numpy as np
from PIL import Image

from ..pipeline.encoder import ClipEncoder, get_encoder
from ..storage.article_store import ArticleStore, get_article_store
from ..storage.clip_store import ClipStore, get_clip_store
from ..storage.vector_store import VectorStore


@dataclass
class ClipSegment:
    """检索返回的核心单元：一个可直接播放的片段。"""
    clip_id: str
    article_id: str
    article_url: str | None
    title: str | None
    start_time: float
    end_time: float
    clip_url: str                      # COS 上的 MP4 URL，可直接 <video src>
    thumbnail: str | None              # 片段里命中帧的缩略图
    score: float
    hit_count: int = 1
    paragraphs: list[str] = field(default_factory=list)
    best_frame_id: str | None = None


class Retriever:
    def __init__(
        self,
        encoder: ClipEncoder | None = None,
        vector_store: VectorStore | None = None,
        clip_store: ClipStore | None = None,
        article_store: ArticleStore | None = None,
    ) -> None:
        self.encoder = encoder or get_encoder()
        self.vector_store = vector_store or VectorStore()
        self.clip_store = clip_store or get_clip_store()
        self.article_store = article_store or get_article_store()

    def _encode_query(self, text: str | None, image_bytes: bytes | None) -> np.ndarray:
        if text and image_bytes:
            tvec = self.encoder.encode_text(text)[0]
            img = Image.open(BytesIO(image_bytes)).convert("RGB")
            ivec = self.encoder.encode_images([img])[0]
            vec = (tvec + ivec) / 2.0
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            return vec.astype(np.float32)
        if text:
            return self.encoder.encode_text(text)[0]
        if image_bytes:
            img = Image.open(BytesIO(image_bytes)).convert("RGB")
            return self.encoder.encode_images([img])[0]
        raise ValueError("必须提供 text 或 image 之一")

    def search(
        self,
        text: str | None = None,
        image_bytes: bytes | None = None,
        top_k: int = 10,
        candidate_k: int | None = None,
    ) -> list[ClipSegment]:
        """KNN → 按 clip_id 聚合 → 关联 clip URL 和文章元数据 → 返回。"""
        candidate_k = candidate_k or max(top_k * 5, 30)
        qvec = self._encode_query(text, image_bytes)
        hits = self.vector_store.query(qvec, top_k=candidate_k)
        if not hits:
            return []

        # 按 clip_id 聚合：同一片段内只保留最高分的那个命中帧
        per_clip: dict[str, dict] = {}
        for h in hits:
            cid = h.metadata.get("clip_id")
            if not cid:
                continue
            slot = per_clip.get(cid)
            if slot is None:
                per_clip[cid] = {
                    "best_hit": h,
                    "hit_count": 1,
                }
            else:
                slot["hit_count"] += 1
                if h.score > slot["best_hit"].score:
                    slot["best_hit"] = h

        if not per_clip:
            return []

        # 拉片段元数据和文章元数据
        clip_docs = self.clip_store.get_many(list(per_clip.keys()))

        # 按文章去重拉 article 元数据
        article_ids = {d.get("article_id") for d in clip_docs.values() if d.get("article_id")}
        article_docs: dict[str, dict] = {}
        for aid in article_ids:
            doc = self.article_store.get(aid)
            if doc:
                article_docs[aid] = doc

        segments: list[ClipSegment] = []
        for cid, slot in per_clip.items():
            cdoc = clip_docs.get(cid)
            if not cdoc:
                continue
            aid = cdoc.get("article_id") or ""
            adoc = article_docs.get(aid) or {}
            hit = slot["best_hit"]

            segments.append(ClipSegment(
                clip_id=cid,
                article_id=aid,
                article_url=cdoc.get("article_url") or adoc.get("url"),
                title=cdoc.get("title") or adoc.get("title"),
                start_time=float(cdoc.get("start_time", 0.0)),
                end_time=float(cdoc.get("end_time", 0.0)),
                clip_url=cdoc.get("clip_url") or "",
                thumbnail=hit.metadata.get("thumbnail"),
                score=hit.score,
                hit_count=slot["hit_count"],
                paragraphs=adoc.get("paragraphs") or [],
                best_frame_id=hit.id,
            ))

        segments.sort(key=lambda s: s.score, reverse=True)
        return segments[:top_k]
