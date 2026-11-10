import os
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import DATA_DIR, NEWS_DIR, PROJECT_ROOT


class Settings(BaseSettings):
    project_root: Path = PROJECT_ROOT
    data_dir: Path = DATA_DIR
    news_dir: Path = NEWS_DIR

    # Hugging Face token（可选）：不带 CLIPQUERY_ 前缀，直接读 .env 里的 HF_TOKEN。
    # 公开模型（如 clip-ViT-B-32）不填也能下；私有/受限模型才需要。
    # pydantic v2：Field(env=...) 已废弃，改用 validation_alias。
    hf_token: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("HF_TOKEN", "CLIPQUERY_HF_TOKEN")
    )

    # Hugging Face 镜像站（可选）：国内访问官方 huggingface.co 慢/被墙时用。
    # 默认走 hf-mirror.com；想回官方就在 .env 里设 HF_ENDPOINT= 或留空。
    hf_endpoint: Optional[str] = Field(
        default="https://hf-mirror.com",
        validation_alias=AliasChoices("HF_ENDPOINT", "CLIPQUERY_HF_ENDPOINT"),
    )

    # Hugging Face 缓存目录（可选）：默认落在项目根下的 data/hf_cache。
    # 相对路径按**项目根**解析（见底部回填逻辑），避免跟进程 cwd 耦合。
    hf_home: Optional[str] = Field(
        default="data/hf_cache",
        validation_alias=AliasChoices("HF_HOME", "CLIPQUERY_HF_HOME"),
    )

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

    # 台词索引：indexer 入库时顺手把 SRT 过 CLIP 文本编码写 clipquery_subtitles
    index_subtitles: bool = True
    # 过短的 cue（秒）跳过不入库——通常是语气词 / 字幕错切，噪声大
    subtitle_min_duration: float = 0.3
    # 过短的文本（字符数）也跳过
    subtitle_min_text_len: int = 2

    opensearch_host: str = "vpc-sg-cgtn-test-newspal-es-bbhcyqsbrsoxbc442kc5sxkgo4.ap-southeast-1.es.amazonaws.com"
    opensearch_port: int = 443
    opensearch_username: str = "admin"
    opensearch_password: str = "NewsPal^_^es2025"
    opensearch_use_ssl: bool = True
    opensearch_verify_certs: bool = True
    opensearch_index: str = "clipquery_frames"
    opensearch_article_index: str = "clipquery_articles"
    opensearch_clip_index: str = "clipquery_clips"
    opensearch_subtitle_index: str = "clipquery_subtitles"

    # --- Redis（任务队列 + 分布式锁，仅本地脚本使用） ---
    # 用于：
    #   1) 异步任务队列（本地脚本 LPUSH → 本地 consume 脚本 BRPOP）
    #   2) 任务元数据 hash（task:<task_id>）
    #   3) executor 的分布式锁 ClipQuery:lock:<task_id>
    redis_host: str = "master.sg-cgtn-test-newspal-redis.k1f3xq.apse1.cache.amazonaws.com"
    redis_port: int = 6379
    redis_db: int = 0
    redis_username: Optional[str] = None
    redis_password: Optional[str] = "m7&xK!pQ4#rT8^nYsvL2"
    redis_ssl: bool = True

    # Redis key 命名空间（大小写敏感，与 COS 前缀对齐）
    project_name: str = "ClipQuery"
    # 保留字段：早期依赖留下的 pool size，当前代码未读取。
    thread_pool_size: int = 32

    # 任务队列 key：脚本 LPUSH 到这里，consume 脚本 BRPOP 消费
    redis_ingest_queue_key: str = "ClipQuery:ingest:queue"
    # 任务元数据 key 前缀：<prefix>:<task_id> 存成 hash
    redis_task_meta_prefix: str = "ClipQuery:task:"
    # 任务元数据 TTL（秒）。done/failed 后保留 24h 供轮询
    redis_task_meta_ttl: int = 86400
    # BRPOP 单次阻塞时长（秒）
    redis_brpop_timeout: int = 5

    # --- 任务执行参数 ---
    # 服务进程完全不读以下 3 项，保留只为 .env 老文件不报错：
    #   worker_enabled                      —— 历史开关，无处读取
    #   scheduler_cgtn_interval_seconds     —— 仅 clipquery.task.cgtn.wait_run_forever
    #                                          (scripts/scan_cgtn.py --loop 入口) 会读
    #   scheduler_consume_interval_seconds  —— 历史字段，无处读取
    worker_enabled: bool = False
    scheduler_cgtn_interval_seconds: int = 3600
    scheduler_consume_interval_seconds: int = 10
    # 下面两项 scripts/consume_one.py → executor 真会读
    # executor 执行任务时每多少秒刷新一次 Redis 锁
    scheduler_task_heartbeat: int = 60
    # 单条任务执行上限（秒），超时自动释放锁让别人接管
    scheduler_task_ttl: int = 1800

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

    model_config = SettingsConfigDict(
        env_prefix="CLIPQUERY_",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


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
