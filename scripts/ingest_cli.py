"""批量入库：遍历目录下所有视频，调用 pipeline 入库。

用法:
    python scripts/ingest_cli.py <video_or_dir> [--ext .mp4,.mov,.mkv] [--force]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clipquery.pipeline.encoder import get_encoder
from clipquery.pipeline.indexer import ingest_video
from clipquery.storage.vector_store import VectorStore


DEFAULT_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def collect_videos(target: Path, exts: set[str]) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted([p for p in target.rglob("*") if p.is_file() and p.suffix.lower() in exts])


def main() -> int:
    parser = argparse.ArgumentParser(description="ClipQuery 批量入库")
    parser.add_argument("target", help="视频文件或目录")
    parser.add_argument("--ext", default=",".join(sorted(DEFAULT_EXTS)), help="逗号分隔的扩展名")
    parser.add_argument("--force", action="store_true", help="已入库的视频也重做")
    args = parser.parse_args()

    target = Path(args.target).expanduser().resolve()
    if not target.exists():
        print(f"路径不存在: {target}", file=sys.stderr)
        return 2

    exts = {e.strip().lower() if e.startswith(".") else "." + e.strip().lower()
            for e in args.ext.split(",") if e.strip()}
    videos = collect_videos(target, exts)
    if not videos:
        print("未找到可处理的视频")
        return 0

    print(f"待处理视频: {len(videos)}")
    encoder = get_encoder()
    vector_store = VectorStore()

    total = 0
    for i, v in enumerate(videos, 1):
        print(f"[{i}/{len(videos)}] {v}")
        try:
            n = ingest_video(
                v,
                encoder=encoder,
                vector_store=vector_store,
                skip_if_exists=not args.force,
            )
            total += n
            print(f"  -> 写入 {n} 帧")
        except Exception as e:
            print(f"  !! 失败: {e}", file=sys.stderr)

    print(f"完成，累计写入 {total} 帧")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
