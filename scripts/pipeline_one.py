"""端到端入库：一个 URL → COS + OpenSearch（三个索引）全部完成。

流程：
    1) clipquery.website.cgtn.process_one_article(url)
       - 爬文章 → 下视频 → 抽音频 → ASR
       - 上传 video.mp4 / audio.mp3 / video.srt / info.json 到 COS
       - 写 clipquery_articles 索引
    2) clipquery.pipeline.indexer.ingest_video(news/<aid>/video.mp4)
       - PySceneDetect 切片 → 片段上传 COS → 写 clipquery_clips
       - CLIP 抽帧编码 → 缩略图上传 COS → 写 clipquery_frames
       - 清理 news/<aid>/ 本地残留

产物：
    COS:          ClipQuery/<aid>/{video.mp4, audio.mp3, video.srt, info.json,
                                   clips/<cid>.mp4, thumbnails/<fid>.jpg}
    OpenSearch:   clipquery_articles / clipquery_clips / clipquery_frames
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clipquery.config import settings                  # noqa: E402
from clipquery.pipeline.indexer import ingest_video    # noqa: E402
from clipquery.website.cgtn import process_one_article # noqa: E402


def run(article_url: str) -> dict:
    """返回融合后的报告：info.json + 本次写入的帧数。"""
    article_url = article_url.strip()
    if not article_url:
        raise ValueError("article_url 不能为空")

    # ---------- 第一阶段：爬虫 ----------
    info = process_one_article(article_url)
    if info is None:
        raise RuntimeError("爬虫未产出：可能无视频或下载失败")

    article_id = info.get("id")
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
    )

    return {
        **info,
        "article_id":     article_id,
        "frames_written": frames_written,
        "local_video":    str(video_path),
    }


def main() -> int:
    default_url = "https://news.cgtn.com/news/2026-11-03/VHJhbnNjcmlwdDkzMTAw/index.html"
    url = sys.argv[1] if len(sys.argv) > 1 else default_url

    print(f"=== 端到端入库: {url} ===")
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
