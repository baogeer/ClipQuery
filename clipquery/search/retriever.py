from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO

import numpy as np
from PIL import Image

from ..pipeline.encoder import ClipEncoder, get_encoder
from ..storage.article_store import ArticleStore, get_article_store
from ..storage.clip_store import ClipStore, get_clip_store
from ..storage.subtitle_store import SubtitleStore, get_subtitle_store
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


@dataclass
class SubtitleSegment:
    """台词搜返回的单元：一句台词 + 它在视频里的精确时间。"""
    article_id: str
    article_url: str | None
    title: str | None
    clip_id: str | None                # 台词时间点落在哪个片段（可能为空）
    clip_url: str | None               # 对应片段的 COS MP4（可直接 <video src>）
    start_time: float                  # 台词起始秒（相对于原视频）
    end_time: float                    # 台词结束秒
    text: str                          # 原台词文本
    score: float                       # CLIP 余弦相似度
    source_video_url: str | None = None  # 原片 COS URL，前端可 seek 到 start_time


class Retriever:
    def __init__(
        self,
        encoder: ClipEncoder | None = None,
        vector_store: VectorStore | None = None,
        clip_store: ClipStore | None = None,
        article_store: ArticleStore | None = None,
        subtitle_store: SubtitleStore | None = None,
    ) -> None:
        self.encoder = encoder or get_encoder()
        self.vector_store = vector_store or VectorStore()
        self.clip_store = clip_store or get_clip_store()
        self.article_store = article_store or get_article_store()
        self.subtitle_store = subtitle_store or get_subtitle_store()

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

    # ==================== 台词搜 ====================
    def search_subtitle(
        self,
        text: str,
        top_k: int = 10,
        candidate_k: int | None = None,
    ) -> list[SubtitleSegment]:
        """按台词语义搜 → 命中带精确 start_time / end_time。

        流程：
          1) CLIP 文本编码（和 frames 同一个 512d 空间）
          2) KNN clipquery_subtitles
          3) 按 clip_id 拉片段 URL；按 article_id 拉文章元数据
          4) 装配 SubtitleSegment 返回

        candidate_k 一般 = top_k，台词索引里已经一条一个粒度，不像 frames 需要
        多召回后聚合。
        """
        if not text or not text.strip():
            return []

        candidate_k = candidate_k or max(top_k, 20)
        qvec = self.encoder.encode_text(text)[0]

        hits = self.subtitle_store.query(qvec, top_k=candidate_k)
        if not hits:
            return []

        clip_ids = {h.metadata.get("clip_id") for h in hits if h.metadata.get("clip_id")}
        clip_docs = self.clip_store.get_many(list(clip_ids)) if clip_ids else {}

        article_ids = {h.metadata.get("article_id") for h in hits if h.metadata.get("article_id")}
        article_docs: dict[str, dict] = {}
        for aid in article_ids:
            doc = self.article_store.get(aid)
            if doc:
                article_docs[aid] = doc

        out: list[SubtitleSegment] = []
        for h in hits:
            aid = h.metadata.get("article_id") or ""
            cid = h.metadata.get("clip_id")
            adoc = article_docs.get(aid) or {}
            cdoc = clip_docs.get(cid) if cid else None
            out.append(SubtitleSegment(
                article_id=aid,
                article_url=(cdoc or {}).get("article_url") or adoc.get("url"),
                title=(cdoc or {}).get("title") or adoc.get("title"),
                clip_id=cid,
                clip_url=(cdoc or {}).get("clip_url"),
                start_time=float(h.metadata.get("start_time", 0.0)),
                end_time=float(h.metadata.get("end_time", 0.0)),
                text=h.metadata.get("text") or "",
                score=h.score,
                source_video_url=adoc.get("cos_video_url"),
            ))

        out.sort(key=lambda s: s.score, reverse=True)
        return out[:top_k]
