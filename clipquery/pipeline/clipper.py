"""视频片段切分：场景检测 + ffmpeg 流 copy。

流程：
    1) PySceneDetect 检测场景边界 → [(start, end), ...]（秒）
    2) 合并过短场景、拆分过长场景，使每段落在 [min, target] 区间
    3) ffmpeg -c copy 快速无损切片；copy 失败回退重编码
产物：
    data/clips/<article_id>/<clip_id>.mp4
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import settings


FFMPEG_BIN = "ffmpeg"


@dataclass
class Clip:
    id: str                 # 稳定 id = sha1(article_id:start:end)[:20]
    article_id: str
    index: int              # 片段在视频中的序号（0-based）
    start_time: float       # 秒
    end_time: float         # 秒
    local_path: Path        # 本地 mp4 路径（切片后产物）

    @property
    def duration(self) -> float:
        return max(0.0, self.end_time - self.start_time)


def _clip_id(article_id: str, start: float, end: float) -> str:
    h = hashlib.sha1(f"{article_id}:{start:.3f}:{end:.3f}".encode("utf-8")).hexdigest()
    return h[:20]


def _probe_duration(video_path: Path) -> float:
    """用 ffprobe 读时长。失败则返回 0。"""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            timeout=30,
        )
        return float(out.decode().strip() or 0.0)
    except Exception:
        return 0.0


def _detect_scenes(video_path: Path, threshold: float) -> list[tuple[float, float]]:
    """返回 [(start_sec, end_sec), ...]；检测失败返回空列表，由上层回退。"""
    try:
        from scenedetect import SceneManager, open_video
        from scenedetect.detectors import ContentDetector
    except ImportError:
        print("[WARN] 未安装 PySceneDetect，回退等长切片")
        return []

    try:
        video = open_video(str(video_path))
        sm = SceneManager()
        sm.add_detector(ContentDetector(threshold=threshold))
        sm.detect_scenes(video)
        scene_list = sm.get_scene_list()
    except Exception as e:
        print(f"[WARN] 场景检测失败: {e}，回退等长切片")
        return []

    return [(s.get_seconds(), e.get_seconds()) for s, e in scene_list]


def _fixed_windows(duration: float, target: float) -> list[tuple[float, float]]:
    """等长切片兜底：从 0 开始，每段 target 秒，最后一段不足也保留。"""
    if duration <= 0:
        return []
    out = []
    t = 0.0
    while t < duration:
        end = min(t + target, duration)
        if end - t >= 0.5:   # 丢掉零碎尾巴
            out.append((t, end))
        t = end
    return out


def _normalize(
    scenes: list[tuple[float, float]],
    *,
    target: float,
    min_dur: float,
    total_duration: float,
) -> list[tuple[float, float]]:
    """把场景列表规范化到 [min, target] 区间：

    - 过长场景按 target 二次拆分
    - 过短场景并入前一个片段（避免产生几百毫秒的碎片）
    """
    if not scenes:
        return _fixed_windows(total_duration, target)

    expanded: list[tuple[float, float]] = []
    for s, e in scenes:
        dur = e - s
        if dur <= target:
            expanded.append((s, e))
            continue
        # 场景过长，等长再切
        t = s
        while t < e:
            seg_end = min(t + target, e)
            expanded.append((t, seg_end))
            t = seg_end

    # 合并过短片段到前一段
    merged: list[list[float]] = []
    for s, e in expanded:
        if merged and (e - s) < min_dur and (e - merged[-1][0]) <= target:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def plan_clips(video_path: str | Path) -> list[tuple[float, float]]:
    """返回 [(start_sec, end_sec), ...]。"""
    video_path = Path(video_path)
    duration = _probe_duration(video_path)
    scenes = _detect_scenes(video_path, settings.scene_detect_threshold)
    return _normalize(
        scenes,
        target=settings.clip_target_seconds,
        min_dur=settings.clip_min_seconds,
        total_duration=duration,
    )


def _ffmpeg_cut(video_path: Path, start: float, end: float, out_path: Path) -> bool:
    """优先 -c copy 无损切；失败回退重编码。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _run(args: list[str]) -> bool:
        cmd = [FFMPEG_BIN, "-y", "-loglevel", "error",
               "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
               "-i", str(video_path), *args, str(out_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return False
        if proc.returncode != 0:
            return False
        return out_path.exists() and out_path.stat().st_size > 0

    if _run(["-c", "copy", "-movflags", "+faststart", "-avoid_negative_ts", "1"]):
        return True
    return _run(["-c:v", "libx264", "-preset", "veryfast",
                 "-c:a", "aac", "-movflags", "+faststart"])


def cut_clips(
    video_path: str | Path,
    article_id: str,
    out_root: Path | None = None,
) -> list[Clip]:
    """按 plan_clips() 切分视频，返回已成功切出的 Clip 列表。"""
    if not shutil.which(FFMPEG_BIN):
        print("[ERROR] 未找到 ffmpeg，无法切片")
        return []

    video_path = Path(video_path).resolve()
    out_dir = (out_root or settings.data_dir / "clips") / article_id
    out_dir.mkdir(parents=True, exist_ok=True)

    windows = plan_clips(video_path)
    clips: list[Clip] = []
    for i, (s, e) in enumerate(windows):
        cid = _clip_id(article_id, s, e)
        out_path = out_dir / f"{cid}.mp4"
        if out_path.exists() and out_path.stat().st_size > 0:
            clips.append(Clip(cid, article_id, i, s, e, out_path))
            continue
        if _ffmpeg_cut(video_path, s, e, out_path):
            clips.append(Clip(cid, article_id, i, s, e, out_path))
        else:
            print(f"[WARN] 切片失败 {article_id} [{s:.2f},{e:.2f}]")
    return clips


def find_clip_for_timestamp(clips: list[Clip], ts: float) -> Clip | None:
    """找到包含该时间戳的片段。clips 需按 start_time 升序。"""
    for c in clips:
        if c.start_time <= ts < c.end_time:
            return c
    if clips and ts >= clips[-1].end_time:
        return clips[-1]
    return None
