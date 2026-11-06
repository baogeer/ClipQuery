import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import DATA_DIR, NEWS_DIR, PROJECT_ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLIPQUERY_",
        env_file=str(PROJECT_ROOT / ".env"),
        extra="ignore",
    )

    project_root: Path = PROJECT_ROOT
    data_dir: Path = DATA_DIR
    news_dir: Path = NEWS_DIR

    # Hugging Face token（可选）：不带 CLIPQUERY_ 前缀，直接读 .env 里的 HF_TOKEN。
    # 公开模型（如 clip-ViT-B-32）不填也能下；私有/受限模型才需要。
    hf_token: str | None = Field(default=None, validation_alias="HF_TOKEN")

    # Hugging Face 镜像站（可选）：国内访问官方 huggingface.co 慢/被墙时用。
    # 默认走 hf-mirror.com；想回官方就在 .env 里设 HF_ENDPOINT= 或留空。
    hf_endpoint: str | None = Field(
        default="https://hf-mirror.com",
        validation_alias="HF_ENDPOINT",
    )

    # Hugging Face 缓存目录（可选）：默认落在项目根下的 data/hf_cache。
    # 相对路径按**项目根**解析（见底部回填逻辑），避免跟进程 cwd 耦合。
    hf_home: str | None = Field(default="data/hf_cache", validation_alias="HF_HOME")

    clip_model_name: str = "clip-ViT-B-32"
    embedding_dim: int = 512
    device: str = "cpu"

    frame_sample_fps: float = 1.0
    frame_max_side: int = 384
    batch_size: int = 32

    # 片段切分
    clip_target_seconds: float = 6.0        # 单片段目标长度上限
    clip_min_seconds: float = 2.0           # 短于此长度的场景并入相邻片段
    scene_detect_threshold: float = 27.0    # PySceneDetect ContentDetector 阈值

    opensearch_host: str = "vpc-sg-cgtn-test-newspal-es-bbhcyqsbrsoxbc442kc5sxkgo4.ap-southeast-1.es.amazonaws.com"
    opensearch_port: int = 443
    opensearch_username: str = "admin"
    opensearch_password: str = "NewsPal^_^es2025"
    opensearch_use_ssl: bool = True
    opensearch_verify_certs: bool = True
    opensearch_index: str = "clipquery_frames"
    opensearch_article_index: str = "clipquery_articles"
    opensearch_clip_index: str = "clipquery_clips"
    opensearch_worker_index: str = "clipquery_workers"   # worker 注册 + 心跳
    opensearch_task_index: str = "clipquery_tasks"       # 异步任务状态

    # --- 分布式调度 ---
    # worker 心跳周期（秒）。
    scheduler_heartbeat_interval: int = 10
    # worker 心跳过期阈值（秒）。超过该值不更新就视为死亡，从 hash 环中剔除。
    scheduler_worker_ttl: int = 30
    # 任务心跳过期阈值（秒）。in_progress 任务超过此未更新允许被别的 worker 抢占。
    scheduler_task_ttl: int = 300
    # 一致性 hash 环每个 worker 的虚拟节点数。越大分布越均匀但计算越慢。
    scheduler_vnodes: int = 150
    # CGTN transcript 扫描周期（分钟）。
    scheduler_cgtn_interval_minutes: int = 60
    opensearch_bulk_size: int = 128
    opensearch_knn_ef_search: int = 128
    # KNN 引擎：OpenSearch 3.0+ 废弃 nmslib，默认走 faiss（2.x 也支持）。
    # 可选 faiss / lucene；向量已 L2 归一化，space_type 用 innerproduct 等价于余弦且更快。
    opensearch_knn_engine: str = "faiss"
    opensearch_knn_space_type: str = "innerproduct"

    # 腾讯云 COS
    cos_secret_id: str = "AKIDRpALI0ThhSySiAf5OULzTkqkbJxPddOv"
    cos_secret_key: str = "KPLAnBLE0iInETTK2XpunTu9E5iDVZP3"
    cos_region: str = "ap-beijing"
    cos_bucket: str = "video-metainsight-test-1326743305"
    cos_prefix: str = "ClipQuery"
    cos_scheme: str = "https"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnails_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()

# HF_TOKEN 可选：留空就不回填环境变量，公开模型照常匿名下载。
# 显式 shell export 的值优先，这里只在未设置时写入。
_hf = (settings.hf_token or "").strip()
if _hf:
    os.environ.setdefault("HF_TOKEN", _hf)
    os.environ.setdefault("HUGGING_FACE_HUB_TOKEN", _hf)

# HF_ENDPOINT：huggingface_hub / transformers / sentence-transformers 启动时读一次，
# 之后不再读，所以必须在 import 这些库之前设置。本模块在它们的 import 链之前被加载。
_hf_endpoint = (settings.hf_endpoint or "").strip()
if _hf_endpoint:
    os.environ.setdefault("HF_ENDPOINT", _hf_endpoint)

# HF_HOME：相对路径相对**项目根**解析成绝对路径，避免受进程 cwd 影响。
# 兼容已是绝对路径的情况。目录不存在自动建。
_hf_home = (settings.hf_home or "").strip()
if _hf_home:
    _hf_home_path = Path(_hf_home)
    if not _hf_home_path.is_absolute():
        _hf_home_path = (settings.project_root / _hf_home_path).resolve()
    _hf_home_path.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(_hf_home_path))
