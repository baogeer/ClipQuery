from __future__ import annotations

import logging
import os
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image

from ..config import settings


# 让模型下载/加载的进度条和日志都显示出来。sentence-transformers / huggingface_hub
# 默认 WARNING，下载时只有一个无进度的静默等待。
def _enable_hf_progress_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            stream=sys.stderr,
        )
    for name in ("sentence_transformers", "huggingface_hub", "transformers"):
        logging.getLogger(name).setLevel(logging.INFO)
    # 显式打开 hf_hub 的下载进度条（环境变量方式，import 后再设也生效）
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "0")


_enable_hf_progress_logging()


# 延后 import：让上面的日志配置先生效，再触发 sentence_transformers 的 import
from sentence_transformers import SentenceTransformer  # noqa: E402


# `data/<model_short_name>/` 作为本地模型目录。
# model 名字里可能含 `/`（如 `sentence-transformers/clip-ViT-B-32`），取最后一段作为文件夹名。
def _local_model_dir(model_name: str) -> Path:
    short = model_name.split("/")[-1]
    return (settings.data_dir / short).resolve()


def _looks_complete(model_dir: Path) -> bool:
    """判断 data/<model>/ 下是否已经是一份可用的 sentence-transformers 模型。

    最低要求：根目录有 modules.json，且至少一个子模块里有大于 1MB 的权重文件。
    sentence-transformers 加载时会从 modules.json 列出的每个子目录读权重。
    """
    if not (model_dir / "modules.json").exists():
        return False
    weight_names = ("model.safetensors", "pytorch_model.bin", "tf_model.h5")
    for p in model_dir.rglob("*"):
        if p.name in weight_names and p.is_file() and p.stat().st_size > 1_000_000:
            return True
    return False


def _download_to_local(model_name: str, target_dir: Path) -> None:
    """用 huggingface_hub.snapshot_download 把整个 repo 拉到 target_dir。

    走 HF_ENDPOINT（默认 hf-mirror.com）；进度条由 hf_hub 自己打印。
    """
    from huggingface_hub import snapshot_download

    target_dir.mkdir(parents=True, exist_ok=True)
    # sentence-transformers 的 CLIP 仓库都在 sentence-transformers/ 组织下
    repo_id = model_name if "/" in model_name else f"sentence-transformers/{model_name}"
    snapshot_download(
        repo_id=repo_id,
        local_dir=str(target_dir),
        local_dir_use_symlinks=False,
    )


class ClipEncoder:
    """CLIP 多模态编码器。离线入库和在线查询共用同一个实例。

    加载策略：
        1) 先看 settings.data_dir/<model>/ 下是否已经是完整模型；是则直接本地加载
        2) 否则用 huggingface_hub 下载到该目录（走 HF_ENDPOINT 镜像），下次就本地命中
    """

    def __init__(self, model_name: str | None = None, device: str | None = None) -> None:
        self.model_name = model_name or settings.clip_model_name
        self.device = device or settings.device
        self.local_dir = _local_model_dir(self.model_name)

        if _looks_complete(self.local_dir):
            print(
                f"[ENCODER] 命中本地模型 path={self.local_dir} device={self.device}",
                flush=True,
            )
        else:
            print(
                f"[ENCODER] 本地未找到完整模型 {self.local_dir}，开始从 Hugging Face 下载",
                flush=True,
            )
            endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
            print(f"[ENCODER] HF_ENDPOINT={endpoint}  （~350MB，首次较慢）", flush=True)
            t_dl = time.time()
            try:
                _download_to_local(self.model_name, self.local_dir)
            except Exception as e:
                raise RuntimeError(
                    f"下载模型失败: {e}。可在 .env 里把 HF_ENDPOINT 切回官方，"
                    f"或手动把权重放到 {self.local_dir}"
                ) from e
            print(f"[ENCODER] 下载完成，耗时 {time.time() - t_dl:.1f}s", flush=True)

        source = str(self.local_dir)
        print(f"[ENCODER] 加载模型 source={source}", flush=True)
        t0 = time.time()
        self.model = SentenceTransformer(source, device=self.device)
        print(f"[ENCODER] 模型就绪，耗时 {time.time() - t0:.1f}s", flush=True)

    def encode_images(self, images: Iterable[Image.Image], batch_size: int | None = None) -> np.ndarray:
        images = list(images)
        if not images:
            return np.zeros((0, settings.embedding_dim), dtype=np.float32)
        vecs = self.model.encode(
            images,
            batch_size=batch_size or settings.batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,   # 批次很小、indexer 自身有 tqdm，不叠两层
        )
        return vecs.astype(np.float32)

    def encode_text(self, texts: str | list[str]) -> np.ndarray:
        if isinstance(texts, str):
            texts = [texts]
        vecs = self.model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vecs.astype(np.float32)


@lru_cache(maxsize=1)
def get_encoder() -> ClipEncoder:
    return ClipEncoder()
