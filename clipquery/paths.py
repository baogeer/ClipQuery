"""项目路径集中管理。

所有需要绝对路径的地方都从这里取，避免散落在各处。
部署到服务器时只需设置环境变量 CLIPQUERY_PROJECT_ROOT，即可把数据/news
目录迁到任意位置，无需改代码。
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_ROOT = Path(__file__).resolve().parent.parent

PROJECT_ROOT: Path = Path(os.environ.get("CLIPQUERY_PROJECT_ROOT", _DEFAULT_ROOT)).resolve()

NEWS_DIR: Path = Path(os.environ.get("CLIPQUERY_NEWS_DIR", PROJECT_ROOT / "news")).resolve()
DATA_DIR: Path = Path(os.environ.get("CLIPQUERY_DATA_DIR", PROJECT_ROOT / "data")).resolve()


def ensure_dirs() -> None:
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


ensure_dirs()
