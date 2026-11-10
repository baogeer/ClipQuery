"""CGTN 单篇文章端到端处理。

流程：
    parse_transcript_detail(url)
      → download_m3u8_as_mp4
      → 抽 MP3 / Whisper SRT
      → 上传 COS（audio/srt 即传即删；video 留给 indexer）
      → 原子写 info.json → 上传 info.json 到 COS → 删本地
      → upsert clipquery_articles

对外入口：
    - process_one_article(article_url) -> dict | None
    - url_hash_id(url) -> str
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ... import cos as cos_client
from ...paths import NEWS_DIR
from ...storage.article_store import get_article_store
from .transcript import parse_transcript_detail
from .video_downloader import download_m3u8_as_mp4

# ---------------- 配置 ----------------
BASE_SAVE_DIR = str(NEWS_DIR)

FFMPEG_BIN = "ffmpeg"
WHISPER_BIN = "whisper"           # pip install openai-whisper 后可用
WHISPER_MODEL = "small"           # tiny/base/small/medium/large
ASR_LANG = "en"


# ---------------- 工具 ----------------
def url_hash_id(url: str) -> str:
    """基于 URL 的稳定 id：sha1 前 16 位。同一 URL 永远拿到同一个 id。"""
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:16]


@contextmanager
def article_lock(article_dir: str, timeout: float = 1800.0, poll: float = 1.0):
    """按文章目录加独占锁，保证同一 URL 不会被多个任务同时处理。

    - 跨平台：Windows 用 msvcrt.locking，POSIX 用 fcntl.flock
    - 跨进程：锁文件 <article_dir>/.lock，由内核守护
    - 阻塞等待：若别的任务正在跑，这里会轮询直到拿到锁或超时
    """
    os.makedirs(article_dir, exist_ok=True)
    lock_path = os.path.join(article_dir, ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout

    try:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"获取锁超时: {lock_path}")
                    time.sleep(poll)
            try:
                yield
            finally:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"获取锁超时: {lock_path}")
                    time.sleep(poll)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
    finally:
        os.close(fd)


def has_whisper() -> bool:
    return shutil.which(WHISPER_BIN) is not None


def extract_audio_from_mp4(mp4_path: str, audio_path: str, timeout: int = 600) -> bool:
    """用 ffmpeg 提取 16k 单声道 wav，供 ASR 使用。"""
    cmd = [
        FFMPEG_BIN, "-y", "-loglevel", "error",
        "-i", mp4_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        audio_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        return os.path.exists(audio_path) and os.path.getsize(audio_path) > 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[ERROR] 音频提取失败: {e}")
        return False


def export_audio_mp3(mp4_path: str, mp3_path: str, timeout: int = 600) -> bool:
    """从 MP4 抽取音频为 MP3（128k），保留在文件夹中供分发/复听。"""
    cmd = [
        FFMPEG_BIN, "-y", "-loglevel", "error",
        "-i", mp4_path,
        "-vn", "-acodec", "libmp3lame", "-b:a", "128k",
        mp3_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        return os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[ERROR] 音频导出失败: {e}")
        return False


def transcribe_with_whisper(audio_path: str, out_dir: str) -> str | None:
    """调用 whisper CLI 生成 SRT，返回 SRT 完整路径。

    不捕获输出，让 whisper 的进度条实时显示，避免"假死"。
    """
    cmd = [
        WHISPER_BIN, audio_path,
        "--model", WHISPER_MODEL,
        "--language", ASR_LANG,
        "--output_format", "srt",
        "--output_dir", out_dir,
        "--task", "transcribe",
        "--fp16", "False",
    ]
    try:
        print(f"[ASR] 运行 whisper（进度将实时输出）: {' '.join(cmd)}")
        subprocess.run(cmd, check=True, timeout=3600)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] whisper 失败，返回码: {e.returncode}")
        return None
    except subprocess.TimeoutExpired:
        print("[ERROR] whisper 超时（>1 小时）")
        return None

    base = os.path.splitext(os.path.basename(audio_path))[0]
    srt_path = os.path.join(out_dir, base + ".srt")
    if os.path.exists(srt_path) and os.path.getsize(srt_path) > 0:
        return srt_path
    print(f"[WARN] whisper 未产出 SRT: {srt_path}")
    return None


def paragraphs_to_srt(paragraphs: list[str], srt_path: str) -> None:
    """兜底方案：把正文段落按 10 秒/段粗排成合法 SRT。

    注意：时间轴是伪造的，仅保证文件格式正确，不对齐音频。
    """
    def _fmt(sec: float) -> str:
        h, r = divmod(int(sec), 3600)
        m, s = divmod(r, 60)
        ms = int(round((sec - int(sec)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, para in enumerate(paragraphs, 1):
            para = para.strip()
            if not para:
                continue
            start, end = (i - 1) * 10.0, i * 10.0
            f.write(f"{i}\n{_fmt(start)} --> {_fmt(end)}\n{para}\n\n")
    print(f"[WARN] SRT 由正文段落粗略生成（时间轴为伪造，未对齐音频）: {srt_path}")


def _load_info(info_path: str) -> dict | None:
    """读旧 info.json，补 id 字段（兼容老数据）。"""
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[WARN] 读取 info.json 失败: {info_path}: {e}")
        return None
    if "id" not in data and data.get("url"):
        data["id"] = url_hash_id(data["url"])
        try:
            with open(info_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] 回写 id 失败: {info_path}: {e}")
    return data


# ---------------- 单个新闻处理 ----------------
def process_one_article(article_url: str) -> dict | None:
    """处理单篇文章。成功返回 info.json 内容（含 id）；跳过返回已有 info；失败返回 None。"""
    print("=" * 60)
    print(f"[INFO] 处理: {article_url}")

    article_id = url_hash_id(article_url)

    # 0. 解析详情
    detail = parse_transcript_detail(article_url)
    title = (detail.get("title") or "").strip() or "untitled"
    paragraphs = detail.get("paragraphs") or []
    m3u8_url = detail.get("video_m3u8")

    if not m3u8_url:
        print(f"[WARN] 未发现视频，跳过: {article_url}")
        return None

    # 1. 以 id 作为目录名，天然避免重名
    article_dir = os.path.join(BASE_SAVE_DIR, article_id)
    os.makedirs(article_dir, exist_ok=True)

    info_path = os.path.join(article_dir, "info.json")

    # 2+. 整段关键区上锁：同一 URL 任何时刻只有一个任务在处理
    with article_lock(article_dir):
        # 锁内二次检查：等锁期间别的任务可能已经完成
        if os.path.exists(info_path):
            print(f"[SKIP] 已处理过: {article_dir}")
            return _load_info(info_path)

        # 2. 下载 MP4
        raw_mp4 = download_m3u8_as_mp4(
            m3u8_url,
            save_dir=article_dir,
            referer=article_url,
        )
        if not raw_mp4 or not os.path.exists(raw_mp4):
            print(f"[ERROR] 视频下载失败: {article_url}")
            return None

        # 3. 统一命名为 video.mp4
        final_mp4 = os.path.join(article_dir, "video.mp4")
        if os.path.abspath(raw_mp4) != os.path.abspath(final_mp4):
            if os.path.exists(final_mp4):
                os.remove(final_mp4)
            os.replace(raw_mp4, final_mp4)
        mp4_name = "video.mp4"

        # 3.5 导出音频 MP3
        audio_name: str | None = "audio.mp3"
        audio_mp3_path = os.path.join(article_dir, audio_name)
        audio_ok = export_audio_mp3(final_mp4, audio_mp3_path)
        if audio_ok:
            print(f"[OK] 音频已导出: {audio_mp3_path}")
        else:
            print(f"[WARN] 音频导出失败: {final_mp4}")
            audio_name = None

        # 4. 生成 SRT：优先 Whisper，回退正文占位
        srt_name = "video.srt"
        srt_path = os.path.join(article_dir, srt_name)

        if os.path.exists(srt_path) and os.path.getsize(srt_path) > 0:
            print(f"[SKIP] SRT 已存在: {srt_path}")
        elif has_whisper():
            tmp_wav = os.path.join(article_dir, "_asr_tmp.wav")
            if extract_audio_from_mp4(final_mp4, tmp_wav):
                generated = transcribe_with_whisper(tmp_wav, article_dir)
                if generated:
                    if os.path.abspath(generated) != os.path.abspath(srt_path):
                        os.replace(generated, srt_path)
                    print(f"[OK] Whisper 字幕: {srt_path}")
                else:
                    print("[WARN] Whisper 生成失败，回退正文占位")
                    paragraphs_to_srt(paragraphs, srt_path)
            else:
                print("[WARN] 音频提取失败，回退正文占位")
                paragraphs_to_srt(paragraphs, srt_path)
            if os.path.exists(tmp_wav):
                try:
                    os.remove(tmp_wav)
                except OSError:
                    pass
        else:
            print(f"[INFO] 未检测到 whisper（{WHISPER_BIN}），使用正文占位 SRT")
            paragraphs_to_srt(paragraphs, srt_path)

        # 5. 组装 meta
        meta: dict = {
            "id": article_id,
            "url": article_url,
            "title": title,
            "mp4_file": mp4_name,
            "audio_file": audio_name,
            "srt_file": srt_name,
            "video_m3u8": m3u8_url,
            "paragraphs": paragraphs,
        }

        # 6. 上传静态文件到 COS。
        #    - video.mp4 / video.srt 保留本地，等 indexer 跑完再删
        #      （SRT 要留给 indexer 读取做台词索引）
        #    - audio.mp3 上传后立即删
        try:
            meta["cos_video_url"] = cos_client.upload_file(
                final_mp4, cos_client.article_key(article_id, mp4_name)
            )
            if audio_name:
                meta["cos_audio_url"] = cos_client.upload_and_delete(
                    audio_mp3_path, cos_client.article_key(article_id, audio_name)
                )
            else:
                meta["cos_audio_url"] = None
            # SRT 只上传，不删本地。indexer 读完后由 ingest_video 的最后一步 rmtree 清掉。
            meta["cos_srt_url"] = cos_client.upload_file(
                srt_path, cos_client.article_key(article_id, srt_name)
            )
        except Exception as e:
            print(f"[WARN] COS 上传失败（不中断流程）: {e}")

        # 7. 临时写 info.json（原子）
        tmp_info = info_path + ".tmp"
        with open(tmp_info, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp_info, info_path)

        # 8. info.json 上传到 COS 后删本地
        try:
            meta["cos_info_url"] = cos_client.upload_file(
                info_path,
                cos_client.article_key(article_id, "info.json"),
                skip_if_exists=False,
            )
            # cos_info_url 回填后再重写一次，保证 OpenSearch 文档含该字段
            with open(tmp_info, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            os.replace(tmp_info, info_path)
            cos_client.upload_file(
                info_path,
                cos_client.article_key(article_id, "info.json"),
                skip_if_exists=False,
            )
            try:
                os.remove(info_path)
            except OSError:
                pass
        except Exception as e:
            print(f"[WARN] info.json 上传 COS 失败: {e}")

        # 9. 写 OpenSearch clipquery_articles
        try:
            doc = dict(meta)
            doc["ingested_at"] = datetime.now(timezone.utc).isoformat()
            get_article_store().upsert(article_id, doc)
        except Exception as e:
            print(f"[WARN] 写 OpenSearch article 索引失败: {e}")

        print(f"[OK] 完成: {article_dir}（video.mp4 待 indexer 清理）")
        return meta
