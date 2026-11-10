"""SRT 字幕解析：手写一个轻量 SubRip 解析器，不引入额外依赖。

SRT 格式：
    1
    00:00:12,340 --> 00:00:15,680
    台词第一行
    可能多行
    <空行>
    2
    00:00:15,900 --> 00:00:18,220
    下一条
    ...

产出：
    [SrtCue(index, start_sec, end_sec, text), ...]
容错：
    - 时间戳解析失败 → 跳过该 cue
    - 文本去前后空白；多行用空格拼接（保留空格，便于 CLIP tokenizer）
    - 允许 BOM、\\r\\n、\\r 多种换行
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# 00:00:12,340 --> 00:00:15,680   (逗号或点都兼容)
_TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,\.](\d{1,3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,\.](\d{1,3})"
)


@dataclass
class SrtCue:
    index: int            # 原 SRT 里的序号（1-based）；解析器忽略后用枚举填
    start_sec: float
    end_sec: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)

    @property
    def mid_sec(self) -> float:
        return (self.start_sec + self.end_sec) / 2.0


def _ts_to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_srt(content: str) -> list[SrtCue]:
    """解析 SRT 全文字符串。空文件返回 []。"""
    if not content:
        return []
    # 归一换行 + 去 BOM
    content = content.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    # 按空行切块
    blocks = re.split(r"\n\s*\n", content.strip())

    cues: list[SrtCue] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        # 找时间戳行（可能第一行是序号，第二行是时间）
        ts_line_idx = None
        for i, ln in enumerate(lines[:2]):
            if _TS_RE.search(ln):
                ts_line_idx = i
                break
        if ts_line_idx is None:
            continue
        m = _TS_RE.search(lines[ts_line_idx])
        if not m:
            continue
        start = _ts_to_sec(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _ts_to_sec(m.group(5), m.group(6), m.group(7), m.group(8))
        text_lines = lines[ts_line_idx + 1:]
        text = " ".join(l.strip() for l in text_lines).strip()
        if not text:
            continue
        cues.append(SrtCue(
            index=len(cues) + 1,
            start_sec=start,
            end_sec=end,
            text=text,
        ))
    return cues


def parse_srt_file(path: str | Path) -> list[SrtCue]:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return []
    # SRT 一般是 UTF-8，但也遇到过 GBK/Latin-1；兜底 utf-8-sig + replace
    try:
        content = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        content = p.read_text(encoding="utf-8", errors="replace")
    return parse_srt(content)


__all__ = ["SrtCue", "parse_srt", "parse_srt_file"]
