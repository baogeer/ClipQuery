from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np
from PIL import Image

from ..config import settings


@dataclass
class Frame:
    video_path: str
    frame_index: int
    timestamp: float
    image: Image.Image


def _resize_keep_ratio(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    scale = max_side / max(h, w)
    if scale >= 1.0:
        return img
    new_w = int(round(w * scale))
    new_h = int(round(h * scale))
    return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)


def iter_frames(
    video_path: str | Path,
    sample_fps: float | None = None,
    max_side: int | None = None,
) -> Iterator[Frame]:
    """按固定 FPS 从视频中采样帧。

    返回 Frame(video_path, frame_index, timestamp, PIL.Image)。
    frame_index 是原始视频里的帧编号；timestamp 是秒。
    """
    sample_fps = sample_fps or settings.frame_sample_fps
    max_side = max_side or settings.frame_max_side

    video_path = str(video_path)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")

    try:
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if src_fps <= 0:
            src_fps = 25.0

        step = max(int(round(src_fps / sample_fps)), 1)

        frame_idx = 0
        while True:
            ret, bgr = cap.read()
            if not ret:
                break

            if frame_idx % step == 0:
                bgr = _resize_keep_ratio(bgr, max_side)
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                pil = Image.fromarray(rgb)
                timestamp = frame_idx / src_fps
                yield Frame(
                    video_path=video_path,
                    frame_index=frame_idx,
                    timestamp=float(timestamp),
                    image=pil,
                )

            frame_idx += 1
            if total_frames and frame_idx >= total_frames:
                break
    finally:
        cap.release()


def save_thumbnail(image: Image.Image, out_path: Path, size: int = 320) -> None:
    img = image.copy()
    img.thumbnail((size, size))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="JPEG", quality=85)
