from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
from tqdm import tqdm

from .. import cos as cos_client
from ..config import settings
from ..storage.article_store import get_article_store
from ..storage.clip_store import get_clip_store
from ..storage.subtitle_store import get_subtitle_store
from ..storage.vector_store import VectorStore
from .clipper import Clip, cut_clips, find_clip_for_timestamp
from .encoder import ClipEncoder, get_encoder
from .extractor import Frame, iter_frames, save_thumbnail
from .srt_parser import SrtCue, parse_srt_file


def _frame_id(video_path: str, frame_index: int) -> str:
    h = hashlib.sha1(f"{video_path}:{frame_index}".encode("utf-8")).hexdigest()
    return h[:20]


def _batched(iterable: Iterable[Frame], size: int) -> Iterable[list[Frame]]:
    buf: list[Frame] = []
    for item in iterable:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def _infer_article_id(video_path: str) -> str:
    """约定：视频文件位于 news/<article_id>/video.mp4。"""
    parent = Path(video_path).parent.name
    return parent or Path(video_path).stem


def _process_clips(
    video_path: Path,
    article_id: str,
    article_meta: dict | None,
) -> tuple[list[Clip], dict[str, str]]:
    """切片 → 上传 COS → 写 clipquery_clips。返回 (clips, clip_id → clip_url)。"""
    clips = cut_clips(video_path, article_id)
    if not clips:
        return [], {}

    source_video_url = (article_meta or {}).get("cos_video_url")
    article_url = (article_meta or {}).get("url")
    title = (article_meta or {}).get("title")
    ingested_at = datetime.now(timezone.utc).isoformat()

    clip_docs: list[dict] = []
    clip_url_map: dict[str, str] = {}
    for c in clips:
        try:
            url: str | None = cos_client.upload_and_delete(
                c.local_path, cos_client.clip_key(article_id, c.id)
            )
        except Exception as e:
            print(f"[WARN] 片段上传 COS 失败 {c.id}: {e}")
            url = None   # 失败即留空，绝不写本地路径
        if url:
            clip_url_map[c.id] = url
        clip_docs.append({
            "clip_id":          c.id,
            "article_id":       article_id,
            "index":            c.index,
            "start_time":       c.start_time,
            "end_time":         c.end_time,
            "duration":         c.duration,
            "clip_url":         url,
            "source_video_url": source_video_url,
            "article_url":      article_url,
            "title":            title,
            "ingested_at":      ingested_at,
        })

    try:
        get_clip_store().upsert_many(clip_docs)
    except Exception as e:
        print(f"[WARN] 写 clipquery_clips 失败: {e}")

    # 本地片段目录通常只剩空壳，顺手删
    clips_dir = settings.data_dir / "clips" / article_id
    if clips_dir.exists():
        shutil.rmtree(clips_dir, ignore_errors=True)

    return clips, clip_url_map


def _load_article_meta(article_id: str) -> dict | None:
    """从 OpenSearch article 索引拿元数据；没有则返回 None。"""
    try:
        return get_article_store().get(article_id)
    except Exception as e:
        print(f"[WARN] 读 article 元数据失败: {e}")
        return None


def _subtitle_doc_id(article_id: str, cue_index: int) -> str:
    h = hashlib.sha1(f"{article_id}:sub:{cue_index}".encode("utf-8")).hexdigest()
    return h[:20]


def _process_subtitles(
    video_path: Path,
    article_id: str,
    article_meta: dict | None,
    clips_sorted: list[Clip],
    encoder: ClipEncoder,
) -> int:
    """读 news/<aid>/video.srt → CLIP 文本编码 → 写 clipquery_subtitles。

    每条 cue：
        - 文本过短 / 时长过短 → 跳过（避免噪声）
        - 定位到所属 clip_id（用 mid_sec，在哪个片段窗口里就归谁）
        - 文档 id = sha1(article_id:sub:<cue_index>)[:20]，幂等

    返回入库的 cue 数（跳过的不算）。
    """
    srt_path = video_path.parent / "video.srt"
    if not srt_path.exists() or srt_path.stat().st_size == 0:
        return 0

    cues: list[SrtCue] = parse_srt_file(srt_path)
    if not cues:
        print(f"[INFO] SRT 为空: {srt_path}")
        return 0

    # 过滤噪声
    filtered: list[SrtCue] = [
        c for c in cues
        if c.duration >= settings.subtitle_min_duration
        and len(c.text) >= settings.subtitle_min_text_len
    ]
    if not filtered:
        print(f"[INFO] SRT 过滤后为空（都太短）: {srt_path}")
        return 0

    # CLIP 文本编码（分批，避免一次塞太多）
    batch_size = max(1, settings.batch_size)
    all_vecs: list[np.ndarray] = []
    for i in range(0, len(filtered), batch_size):
        batch_texts = [c.text for c in filtered[i : i + batch_size]]
        vecs = encoder.encode_text(batch_texts)   # (N, 512)
        all_vecs.append(vecs)
    embeddings = np.concatenate(all_vecs, axis=0)

    source_video_url = (article_meta or {}).get("cos_video_url")
    ingested_at = datetime.now(timezone.utc).isoformat()
    ids: list[str] = []
    metadatas: list[dict] = []
    for cue in filtered:
        owning_clip = find_clip_for_timestamp(clips_sorted, cue.mid_sec)
        ids.append(_subtitle_doc_id(article_id, cue.index))
        metadatas.append({
            "article_id":       article_id,
            "clip_id":          owning_clip.id if owning_clip else None,
            "cue_index":        cue.index,
            "start_time":       cue.start_sec,
            "end_time":         cue.end_sec,
            "duration":         cue.duration,
            "text":             cue.text,
            "source_video_url": source_video_url,
            "ingested_at":      ingested_at,
        })

    store = get_subtitle_store()
    store.add(ids=ids, embeddings=embeddings, metadatas=metadatas)
    store.refresh()
    print(f"[OK] 台词索引 article_id={article_id} cues={len(ids)}")
    return len(ids)


def ingest_video(
    video_path: str | Path,
    *,
    article_id: str | None = None,
    encoder: ClipEncoder | None = None,
    vector_store: VectorStore | None = None,
    save_thumbs: bool = True,
    show_progress: bool = True,
    skip_if_exists: bool = True,
) -> int:
    """切片 → 抽帧 → CLIP 编码 → 写 OpenSearch。返回写入的帧数。"""
    video_path = Path(video_path).resolve()
    encoder = encoder or get_encoder()
    vector_store = vector_store or VectorStore()

    article_id = article_id or _infer_article_id(str(video_path))
    article_meta = _load_article_meta(article_id)

    # 短路判断：CGTN 流程按 article_id 查（库里不存本地路径），
    # path/upload 流程按 video_path 查（article_id 是从父目录名猜的，不稳）。
    if skip_if_exists:
        if article_meta and vector_store.frame_count_for_article(article_id) > 0:
            return 0
        if not article_meta and vector_store.video_frame_count(str(video_path)) > 0:
            return 0

    # 1. 切片 + 上传 + 写 clips 索引
    clips, _clip_urls = _process_clips(video_path, article_id, article_meta)
    clips_sorted = sorted(clips, key=lambda c: c.start_time)

    # 1.5 台词索引（可选）：把 SRT 每条 cue 过 CLIP 文本编码写 clipquery_subtitles。
    # 失败不中断主流程，只打 WARN。
    if settings.index_subtitles:
        try:
            _process_subtitles(video_path, article_id, article_meta, clips_sorted, encoder)
        except Exception as e:
            print(f"[WARN] 写台词索引失败 article_id={article_id}: {e}")

    # 2. 抽帧 + CLIP 编码 + 绑定到所属 clip
    source_video_url = (article_meta or {}).get("cos_video_url")
    ingested_at = datetime.now(timezone.utc).isoformat()
    total_written = 0
    frame_iter = iter_frames(str(video_path))
    if show_progress:
        frame_iter = tqdm(frame_iter, desc=video_path.name, unit="frame")

    for batch in _batched(frame_iter, settings.batch_size):
        images = [f.image for f in batch]
        vecs: np.ndarray = encoder.encode_images(images)

        ids: list[str] = []
        metadatas: list[dict] = []

        for frame in batch:
            fid = _frame_id(frame.video_path, frame.frame_index)
            thumb_url: str | None = None
            if save_thumbs:
                thumb_path = settings.thumbnails_dir / f"{fid}.jpg"
                save_thumbnail(frame.image, thumb_path)
                try:
                    thumb_url = cos_client.upload_and_delete(
                        thumb_path, cos_client.thumbnail_key(article_id, fid)
                    )
                except Exception as e:
                    print(f"[WARN] 缩略图上传 COS 失败 {fid}: {e}")
                    thumb_url = None   # 失败即留空，绝不写本地路径

            owning_clip = find_clip_for_timestamp(clips_sorted, frame.timestamp)

            # CGTN 流程（article_meta 有 cos_video_url）→ 不落本地路径，只写 COS URL；
            # path/upload 流程 → 本地 video_path 是 dedupe/purge 的主键，必须保留。
            meta: dict = {
                "article_id":  article_id,
                "clip_id":     owning_clip.id if owning_clip else None,
                "frame_index": frame.frame_index,
                "timestamp":   frame.timestamp,
                "thumbnail":   thumb_url,
                "ingested_at": ingested_at,
            }
            if source_video_url:
                meta["source_video_url"] = source_video_url
            else:
                meta["video_path"] = frame.video_path

            ids.append(fid)
            metadatas.append(meta)

        vector_store.add(ids=ids, embeddings=vecs, metadatas=metadatas)
        total_written += len(ids)

    if total_written > 0:
        vector_store.refresh()

    # 入库全部成功：清理 news/<article_id>/ 本地副本（含 video.mp4）。
    # 失败则保留，便于排查和重试。
    if total_written > 0:
        article_dir = video_path.parent
        if article_dir.name == article_id and article_dir.exists():
            shutil.rmtree(article_dir, ignore_errors=True)
            print(f"[OK] 已清理本地 {article_dir}")

    return total_written
