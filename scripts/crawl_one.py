"""抓取单篇 CGTN 文章并生成 news/<slug>/ 目录。

只做抓取（不做 indexer）。要端到端入库请用 scripts/pipeline_one.py。

产物与 process_one_article 一致：
    <project_root>/news/<id>/
        info.json     # 文章元数据（含 id = url 的 sha1 前 16 位，上传后会删除本地）
        video.mp4     # 留给 indexer 消费
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clipquery.website.cgtn import process_one_article  # noqa: E402


def crawl_one(article_url: str) -> dict | None:
    article_url = article_url.strip()
    if not article_url:
        raise ValueError("article_url 不能为空")
    return process_one_article(article_url)


def main() -> int:
    url = 'https://news.cgtn.com/news/2026-11-03/VHJhbnNjcmlwdDkzMTAw/index.html'

    info = crawl_one(url)
    if info is None:
        print("[FAIL] 未生成产物（无视频或处理失败）", file=sys.stderr)
        return 1

    print("[DONE] info.json:")
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
