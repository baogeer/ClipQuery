"""端到端入库：一个 URL → COS + OpenSearch（四个索引）全部完成。

**强制重跑版本**：进入主流程前先按 article_id 清旧数据，保证从零开始。

流程：
    0) purge_article(article_id)
       - 删 OpenSearch 四个索引里该 article_id 的所有文档
         （clipquery_articles / clipquery_clips / clipquery_frames / clipquery_subtitles）
       - 删 COS <prefix>/<aid>/ 下全部对象
       - 删本地 news/<aid>/ 目录
    1) clipquery.website.cgtn.process_one_article(url)
       - 爬文章 → 下视频 → 抽音频 → ASR
       - 上传 video.mp4 / audio.mp3 / video.srt / info.json 到 COS
       - 写 clipquery_articles 索引
    2) clipquery.pipeline.indexer.ingest_video(news/<aid>/video.mp4)
       - PySceneDetect 切片 → 片段上传 COS → 写 clipquery_clips
       - SRT → CLIP 文本编码 → 写 clipquery_subtitles
       - CLIP 抽帧编码 → 缩略图上传 COS → 写 clipquery_frames
       - 清理 news/<aid>/ 本地残留

产物：
    COS:          ClipQuery/<aid>/{video.mp4, audio.mp3, video.srt, info.json,
                                   clips/<cid>.mp4, thumbnails/<fid>.jpg}
    OpenSearch:   clipquery_articles / clipquery_clips / clipquery_frames /
                  clipquery_subtitles
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clipquery import cos as cos_client                 # noqa: E402
from clipquery.config import settings                   # noqa: E402
from clipquery.pipeline.indexer import ingest_video     # noqa: E402
from clipquery.storage.article_store import get_article_store      # noqa: E402
from clipquery.storage.clip_store import get_clip_store            # noqa: E402
from clipquery.storage.subtitle_store import get_subtitle_store    # noqa: E402
from clipquery.storage.vector_store import get_vector_store        # noqa: E402
from clipquery.website.cgtn import process_one_article, url_hash_id  # noqa: E402


def purge_article(article_id: str) -> dict:
    """按 article_id 把旧数据连根拔除：OpenSearch 四索引 + COS 前缀 + 本地目录。"""
    report: dict = {}

    # 1) OpenSearch：四个索引按 article_id 删
    try:
        report["os_articles"] = int(get_article_store().delete(article_id))
    except Exception as e:
        report["os_articles"] = f"ERR: {e}"
    try:
        report["os_clips"] = get_clip_store().delete_for_article(article_id)
    except Exception as e:
        report["os_clips"] = f"ERR: {e}"
    try:
        report["os_frames"] = get_vector_store().delete_for_article(article_id)
    except Exception as e:
        report["os_frames"] = f"ERR: {e}"
    try:
        report["os_subtitles"] = get_subtitle_store().delete_for_article(article_id)
    except Exception as e:
        report["os_subtitles"] = f"ERR: {e}"

    # 2) COS：删 <prefix>/<aid>/ 下所有对象
    try:
        report["cos_objects"] = cos_client.delete_prefix(
            cos_client.article_prefix(article_id)
        )
    except Exception as e:
        report["cos_objects"] = f"ERR: {e}"

    # 3) 本地：news/<aid>/
    local_dir = settings.news_dir / article_id
    if local_dir.exists():
        shutil.rmtree(local_dir, ignore_errors=True)
        report["local_dir"] = f"removed: {local_dir}"
    else:
        report["local_dir"] = "not_exist"

    return report


def run(article_url: str) -> dict:
    """强制重跑：先 purge 再两阶段入库。返回融合后的报告。"""
    article_url = article_url.strip()
    if not article_url:
        raise ValueError("article_url 不能为空")

    article_id = url_hash_id(article_url)

    # ---------- 第 0 阶段：清旧数据 ----------
    print(f"=== [PURGE] article_id={article_id} ===")
    purge_report = purge_article(article_id)
    for k, v in purge_report.items():
        print(f"  {k}: {v}")

    # ---------- 第一阶段：爬虫 ----------
    info = process_one_article(article_url)
    if info is None:
        raise RuntimeError("爬虫未产出：可能无视频或下载失败")

    article_id = info.get("id") or article_id
    if not article_id:
        raise RuntimeError(f"info.json 缺 id 字段: {info}")

    # ---------- 第二阶段：切片 + 抽帧 + 索引 ----------
    video_path = settings.news_dir / article_id / "video.mp4"
    if not video_path.exists():
        raise RuntimeError(f"video.mp4 不存在，无法入库: {video_path}")

    frames_written = ingest_video(
        video_path,
        article_id=article_id,
        show_progress=True,
        skip_if_exists=False,   # purge 后理论为空；保险起见显式关短路
    )

    return {
        **info,
        "article_id":     article_id,
        "frames_written": frames_written,
        "local_video":    str(video_path),
        "purge_report":   purge_report,
    }


def main() -> int:
    default_url = "https://news.cgtn.com/news/2026-11-03/VHJhbnNjcmlwdDkzMTAw/index.html"
    url = sys.argv[1] if len(sys.argv) > 1 else default_url

    print(f"=== 端到端入库（强制重跑）: {url} ===")
    try:
        result = run(url)
    except Exception as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1

    print("\n=== 完成 ===")
    print(f"article_id:     {result['article_id']}")
    print(f"frames_written: {result['frames_written']}")
    print(f"cos_video_url:  {result.get('cos_video_url')}")
    print(f"cos_info_url:   {result.get('cos_info_url')}")
    print("\n完整 info.json:")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
