# ClipQuery

基于 CLIP 的视频语义检索：**以文搜视频片段，以图搜视频**。输入自然语言或参考图，返回可直接播放的 MP4 片段 + 新闻详情。

- **向量库**：AWS OpenSearch `knn_vector`（三个索引：帧向量 / 可播放片段 / 文章元数据）
- **对象存储**：腾讯云 COS，所有静态文件（video / clip / thumbnail）都在 COS，本地只做中转
- **数据源**：CGTN transcript 页面自动抓取（也支持上传视频 / 本地路径）

## 文档

- [需求文档](docs/需求文档.md) — 产品定位、核心需求、索引 schema
- [架构文档](docs/架构文档.md) — 模块职责、数据流、关键决策
- [API 文档](docs/API文档.md) — HTTP 接口契约与前端集成示例
- [部署文档](docs/部署文档.md) — 环境准备、systemd、常见问题

## 快速开始

```bash
# 1. 装依赖
pip install -r requirements.txt
sudo apt-get install -y ffmpeg

# 2. 配置
cp .env.example .env
vim .env            # 至少填 OpenSearch 密码 + COS 凭据

# 3. 预加载 CLIP 模型到 data/clip-ViT-B-32/
#    .env 默认 HF_ENDPOINT=https://hf-mirror.com，国内直接可用
hf download sentence-transformers/clip-ViT-B-32 --local-dir ./data/clip-ViT-B-32

# 4. 起 API
uvicorn clipquery.api.main:app --reload --port 8000
```

> 第 3 步是可选加速步骤——不执行的话，`encoder.py` 首次调用时会自动触发 `snapshot_download` 下到同一目录。手动预下的好处是**日志里能看到进度**、断网能重试、便于镜像构建时提前下好。

### 一步到位：URL 入库

```bash
curl -X POST http://localhost:8000/ingest \
  -F 'url=https://news.cgtn.com/news/2026-11-03/VHJhbnNjcmlwdDkzMTAw/index.html'
# → {"job_id":"abc...","status":"queued",...}

# 轮询 queued → crawling → ingesting → done
curl http://localhost:8000/ingest/abc...
```

### 检索

```bash
# 文本
curl -X POST http://localhost:8000/search \
  -F 'query=一个人在海边奔跑' -F 'top_k=5'

# 参考图
curl -X POST http://localhost:8000/search \
  -F 'image=@sample.jpg' -F 'top_k=5'
```

返回每个片段带：`clip_url`（COS 上的 MP4，可直接 `<video src>`）、`article_url`（新闻详情页）、`title`、`paragraphs`、`thumbnail`、`start_time` / `end_time`、`score`。

## 项目结构

```
clipquery/
├── paths.py              # 项目根路径（CLIPQUERY_PROJECT_ROOT）
├── config.py             # pydantic-settings，读 .env
├── cos.py                # 腾讯云 COS 封装
├── pipeline/
│   ├── extractor.py      # OpenCV 抽帧
│   ├── clipper.py        # PySceneDetect 切片 + ffmpeg
│   ├── encoder.py        # CLIP 文本/图像编码（单例）
│   └── indexer.py        # 切片 → 上传 → 编码 → 三索引写入
├── storage/
│   ├── vector_store.py   # clipquery_frames（knn_vector）
│   ├── clip_store.py     # clipquery_clips（片段元数据 + COS URL）
│   └── article_store.py  # clipquery_articles（info.json 副本）
├── website/              # 各站点爬虫
│   └── cgtn/
│       ├── transcript.py        # CGTN 列表/详情页 HTML 解析
│       ├── video_downloader.py  # m3u8 → mp4（并发安全）
│       └── pipeline.py          # 单篇完整：下载 → ASR → COS → articles 索引
├── task/                 # 定时任务
│   └── cgtn.py           # 每小时扫 transcript 列表增量入库（APScheduler）
├── search/retriever.py   # KNN → clip_id 聚合 → JOIN
└── api/main.py           # FastAPI: /ingest /search /health

scripts/                  # 瘦入口，只负责参数解析与调用 clipquery.*
├── crawl_one.py          # 只爬，不入库
├── pipeline_one.py       # 端到端：爬 + 切片 + 编码 + 三索引
└── ingest_cli.py         # 批量把本地视频入库

docs/
├── 需求文档.md
├── 架构文档.md
├── API文档.md
└── 部署文档.md
```

## 主要依赖

- `fastapi` + `uvicorn`
- `sentence-transformers` + `torch` — CLIP
- `opensearch-py` — 向量库
- `cos-python-sdk-v5` — 腾讯云 COS
- `scenedetect[opencv]` — 场景切片
- `opencv-python` + `Pillow` — 抽帧
- `openai-whisper` — ASR
- `requests` + `lxml` — 爬虫

外部依赖：**ffmpeg** 必须在 PATH 中。

## 预加载模型指令

```
hf download sentence-transformers/clip-ViT-B-32 --local-dir ./data/clip-ViT-B-32



