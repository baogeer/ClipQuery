"""m3u8 → MP4 下载器。

两阶段 ffmpeg 流水线：
    1) m3u8 → ts 分片落到独占临时目录（tempfile.mkdtemp，并发安全）
    2) ts concat → mp4（-c copy 优先，失败回退重编码）

对外主要接口：
    - download_m3u8_as_mp4(m3u8_url, save_dir, referer, ...)  → 本地 mp4 路径
    - fetch_and_download_video(article_url, ...)              → 通用入口（含解析 m3u8）
    - parse_video_url(html, article_url)                      → 从 HTML 里找 m3u8
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from lxml import etree

from ...paths import NEWS_DIR

FFMPEG_BIN = "ffmpeg"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_SAVE_DIR = str(NEWS_DIR)
DEFAULT_TS_TMP_ROOT = str(NEWS_DIR / ".ts_tmp")


def parse_video_url(html: str, article_url: str) -> Optional[str]:
    """从任意 CGTN 文章 HTML 中解析 m3u8 视频地址（与分类无关）。"""
    root = etree.HTML(html, base_url=article_url)
    if root is None:
        return None

    def _norm(u: str) -> str:
        return urljoin(article_url, u.strip())

    # 1) JSON-LD
    for script in root.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            data = json.loads(script)
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            if obj.get("@type") == "VideoObject":
                cu = obj.get("contentUrl")
                if isinstance(cu, str) and cu:
                    return _norm(cu)
            vf = obj.get("video")
            if isinstance(vf, dict):
                cu = vf.get("contentUrl")
                if isinstance(cu, str) and cu:
                    return _norm(cu)

    # 2) <video> / <source>
    for src in root.xpath("//video/@src | //video/source/@src"):
        if src:
            return _norm(src)

    # 3) meta
    for xp in (
        '//meta[@property="og:video"]/@content',
        '//meta[@property="og:video:url"]/@content',
        '//meta[@property="og:video:secure_url"]/@content',
        '//meta[@name="twitter:player:stream"]/@content',
    ):
        for val in root.xpath(xp):
            if val:
                return _norm(val)

    # 4) 兜底
    m = re.search(r'https?://[^\s"\'<>]+?\.m3u8(?:\?[^\s"\'<>]*)?', html)
    if m:
        return m.group(0)
    m = re.search(r'https?://video\.cgtn\.com/[^\s"\'<>]+', html)
    if m:
        return m.group(0)

    return None


def _ffmpeg_common_args(m3u8_url: str, referer: str) -> list[str]:
    return [
        FFMPEG_BIN,
        "-y",
        "-loglevel", "error",
        "-headers", f"User-Agent: {UA}\r\nReferer: {referer}\r\n",
        "-i", m3u8_url,
    ]


def _step1_dump_ts_to_tmp(
    m3u8_url: str,
    referer: str,
    ts_tmp_dir: str,
    timeout: int,
) -> bool:
    """m3u8 的 ts 分片落到 ts_tmp_dir（无损 remux 到 ts 容器）。"""
    os.makedirs(ts_tmp_dir, exist_ok=True)
    seg_pattern = os.path.join(ts_tmp_dir, "seg_%05d.ts")
    cmd = _ffmpeg_common_args(m3u8_url, referer) + [
        "-c", "copy",
        "-f", "segment",
        "-segment_format", "mpegts",
        "-segment_list", os.path.join(ts_tmp_dir, "index.m3u8"),
        "-segment_time", "30",
        "-reset_timestamps", "1",
        seg_pattern,
    ]
    print(f"[FFMPEG-STAGE1] ts 临时目录: {ts_tmp_dir}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[FFMPEG-ERR] 阶段1 超时: {m3u8_url}")
        return False
    if proc.returncode != 0:
        print(f"[FFMPEG-ERR] 阶段1 失败: {proc.stderr.strip()[:500]}")
        return False
    has_seg = any(f.endswith(".ts") for f in os.listdir(ts_tmp_dir))
    if not has_seg:
        print(f"[FFMPEG-ERR] 阶段1 未生成 ts: {m3u8_url}")
        return False
    return True


def _step2_ts_to_mp4(ts_tmp_dir: str, local_mp4: str, timeout: int) -> bool:
    """ts concat → MP4。优先 copy，失败回退重编码。"""
    list_file = os.path.join(ts_tmp_dir, "concat.txt")
    ts_files = sorted(
        os.path.join(ts_tmp_dir, f)
        for f in os.listdir(ts_tmp_dir)
        if f.endswith(".ts")
    )
    if not ts_files:
        print(f"[ERROR] 无 ts 可合并: {ts_tmp_dir}")
        return False
    with open(list_file, "w", encoding="utf-8") as f:
        for p in ts_files:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")

    def _run(extra_args: list[str]) -> bool:
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-loglevel", "error",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            *extra_args,
            "-movflags", "+faststart",
            local_mp4,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            print("[FFMPEG-ERR] 阶段2 超时")
            return False
        if proc.returncode != 0:
            print(f"[FFMPEG-ERR] 阶段2 失败: {proc.stderr.strip()[:500]}")
            return False
        return True

    if _run(["-c", "copy", "-bsf:a", "aac_adtstoasc"]):
        return True
    return _run(["-c:v", "libx264", "-c:a", "aac", "-preset", "veryfast"])


def download_m3u8_as_mp4(
    m3u8_url: str,
    save_dir: str,
    referer: str,
    ts_tmp_root: str = DEFAULT_TS_TMP_ROOT,
    timeout: int = 900,
) -> Optional[str]:
    """把 m3u8 转成 MP4：

    1) ts 分片落到 ts_tmp_root/<base>-<随机后缀>/ 独占临时目录
       —— 用 tempfile.mkdtemp 保证多进程 / 多线程并发下目录绝不冲突
    2) ts → mp4 输出到 save_dir/<base>.mp4
    3) 无论成功失败，finally 删除本任务的临时目录
    """
    os.makedirs(save_dir, exist_ok=True)
    os.makedirs(ts_tmp_root, exist_ok=True)

    if not shutil.which(FFMPEG_BIN) and not os.path.isfile(FFMPEG_BIN):
        print(f"[ERROR] 未找到 ffmpeg，无法转码: {m3u8_url}")
        return None

    base = os.path.splitext(os.path.basename(urlparse(m3u8_url).path))[0] or "video"
    local_mp4 = os.path.join(save_dir, f"{base}.mp4")

    ts_tmp_dir = tempfile.mkdtemp(prefix=f"{base}-", dir=ts_tmp_root)

    ok = False
    try:
        if not _step1_dump_ts_to_tmp(m3u8_url, referer, ts_tmp_dir, timeout):
            return None
        ok = _step2_ts_to_mp4(ts_tmp_dir, local_mp4, timeout)
    finally:
        shutil.rmtree(ts_tmp_dir, ignore_errors=True)

    if ok and os.path.exists(local_mp4) and os.path.getsize(local_mp4) > 0:
        print(f"[OK] 已保存: {local_mp4}")
        return local_mp4

    if os.path.exists(local_mp4):
        os.remove(local_mp4)
    print(f"[ERROR] m3u8 转 MP4 失败: {m3u8_url}")
    return None


def fetch_and_download_video(
    article_url: str,
    save_dir: str = DEFAULT_SAVE_DIR,
    ts_tmp_root: str = DEFAULT_TS_TMP_ROOT,
) -> Optional[str]:
    """通用入口：传入任意 CGTN 文章 URL，解析并下载视频为 MP4。"""
    try:
        resp = requests.get(article_url, headers={"User-Agent": UA}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] 请求文章失败: {article_url}: {e}")
        return None

    html = resp.content.decode("utf-8", errors="ignore")
    m3u8_url = parse_video_url(html, article_url)
    if not m3u8_url:
        print(f"[WARN] 未发现视频: {article_url}")
        return None

    print(f"[INFO] 找到视频: {m3u8_url}")
    return download_m3u8_as_mp4(
        m3u8_url,
        save_dir=save_dir,
        referer=article_url,
        ts_tmp_root=ts_tmp_root,
    )
