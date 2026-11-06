"""CGTN 爬虫。

对外稳定接口：
    - crawl_transcript_page()       列表页 → 详情 URL 列表
    - parse_transcript_detail(url)  详情页 → {title, paragraphs, video_m3u8, ...}
    - download_m3u8_as_mp4(...)     m3u8 → 本地 mp4
    - process_one_article(url)      端到端：抓 + 下载 + ASR + 上传 COS + 写 articles 索引
    - url_hash_id(url)              URL → 16 位稳定 id（article_id）
"""
from .pipeline import (
    article_lock,
    export_audio_mp3,
    extract_audio_from_mp4,
    has_whisper,
    paragraphs_to_srt,
    process_one_article,
    transcribe_with_whisper,
    url_hash_id,
)
from .transcript import (
    crawl_transcript_page,
    extract_links_from_html,
    parse_transcript_detail,
)
from .video_downloader import (
    download_m3u8_as_mp4,
    fetch_and_download_video,
    parse_video_url,
)

__all__ = [
    # transcript
    "crawl_transcript_page",
    "extract_links_from_html",
    "parse_transcript_detail",
    # video downloader
    "download_m3u8_as_mp4",
    "fetch_and_download_video",
    "parse_video_url",
    # pipeline
    "article_lock",
    "export_audio_mp3",
    "extract_audio_from_mp4",
    "has_whisper",
    "paragraphs_to_srt",
    "process_one_article",
    "transcribe_with_whisper",
    "url_hash_id",
]
