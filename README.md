# ClipQuery

基于 CLIP 的视频语义检索：**以文搜视频片段，以图搜视频**。输入自然语言或参考图，返回可直接播放的 MP4 片段 + 新闻详情。

- **向量库**：AWS OpenSearch `knn_vector`（三个索引：帧向量 / 可播放片段 / 文章元数据）
- **对象存储**：腾讯云 COS，所有静态文件（video / clip / thumbnail）都在 COS，本地只做中转
- **数据源**：CGTN transcript 页面自动抓取（也支持上传视频 / 本地路径）

## 文档

- [需求文档](docs/需求文档.md) — 产品定位、核心需求、索引 schema
- [架构文档](docs/架构文档.md) — 模块职责、数据流、关键决策
- [接口文档](docs/接口文档.md) — HTTP 端点、Python 内部契约、Redis / OpenSearch schema
- [部署文档](docs/部署文档.md) — Docker 构建、启动、运维

## 快速开始

两种启动方式任选：

- **本地 Python 启动（开发 / 调试推荐）**：见 [部署文档 §5 本地启动](docs/部署文档.md#5-本地启动不走-docker)，一键跑 venv + uvicorn。
- **Docker 启动（生产）**：见 [部署文档 §6 Docker 部署](docs/部署文档.md#6-docker-部署)。

30 秒上手的 Docker 命令：

```bash
# 1. 配置
cp .env.example .env
vim .env            # 至少填 OpenSearch 密码 + COS 凭据 + Redis 密码

# 2. 构建镜像
docker build -t clipquery:latest .

# 3. 启动（服务只跑 API，不做任何异步处理）
docker run -d --name clipquery \
  -p 8008:8000 \
  -v $PWD/.env:/app/.env:ro \
  -v clipquery-news:/app/news \
  -v clipquery-data:/app/data \
  --restart unless-stopped \
  clipquery:latest

# 4. 验证（容器内监听 8000，宿主机映射到 8008）
curl http://localhost:8008/health
```

**本地启动 TL;DR**（Windows / Linux / macOS 通用，完整版见部署文档 §5）：

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.2.0"
pip install -r requirements.txt
cp .env.example .env     # 填密钥
uvicorn clipquery.main:app --host 0.0.0.0 --port 8000 --reload
```

另开一个终端跑数据处理脚本：

```bash
python scripts/pipeline_one.py "https://news.cgtn.com/.../index.html"   # 端到端一篇
python scripts/scan_cgtn.py && python scripts/consume_one.py --all       # 扫列表 + 消费队列
```

### 工作流：API 负责入队，脚本负责执行

服务容器**不消费队列也不定时扫 CGTN**。数据处理一律由本地脚本手动触发：

```bash
# A. 抓一次 CGTN 列表 → 把新文章入 Redis 队列
python scripts/scan_cgtn.py

# B. 从队列拿一条处理（爬 + 切片 + CLIP 编码 + 三索引写入）
python scripts/consume_one.py

# 或者把队列里积压的任务全跑完
python scripts/consume_one.py --all
```

本地跑脚本连的是**同一个 Redis / OpenSearch / COS**，处理结果写回去，
线上 `/search` 立刻查得到。

### URL 入库（本地脚本，不走 HTTP）

> 服务端**不对外暴露任务入队接口**，任务只能从本地脚本投入 Redis 队列。

```bash
# 把一篇 CGTN 文章端到端跑完（爬 + 切片 + 编码 + 索引），本地直接执行
python scripts/pipeline_one.py "https://news.cgtn.com/news/.../index.html"

# 批量：扫 CGTN 列表 → 入 Redis 队列
python scripts/scan_cgtn.py

# 从 Redis 队列拿一条执行
python scripts/consume_one.py

# 任务状态 HTTP 仍可查（前端轮询用，Docker 部署时端口替换成宿主机映射端口如 8008）
curl http://localhost:8000/task/ingest-<article_id>
```

### 检索

> 本地启动走 `localhost:8000`；Docker 部署端口映射 `8008:8000`，把下面的 `8000` 换成 `8008`。

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
│   ├── subtitle_store.py # clipquery_subtitles（台词向量 + 时间轴）
│   └── article_store.py  # clipquery_articles（info.json 副本）
├── website/              # 各站点爬虫
│   └── cgtn/
│       ├── transcript.py        # CGTN 列表/详情页 HTML 解析
│       ├── video_downloader.py  # m3u8 → mp4（并发安全）
│       └── pipeline.py          # 单篇完整：下载 → ASR → COS → articles 索引
├── task/                 # 定时任务
│   └── cgtn.py           # 每小时扫 transcript 列表增量入库（APScheduler）
├── search/retriever.py   # KNN → clip_id 聚合 → JOIN
├── mcp_server.py        # MCP: 5 个只读工具（health/get_task/list_articles/get_article/search_subtitle）
└── api/main.py           # FastAPI: /search /task/{id} /articles /health + /mcp（挂载 MCP 服务）

scripts/                  # 瘦入口，只负责参数解析与调用 clipquery.*
├── crawl_one.py          # 只爬，不入库（调试爬虫）
├── pipeline_one.py       # 端到端（已带强制重跑：先 purge 再入库）
├── scan_cgtn.py          # 扫 CGTN 列表 → 入 Redis 队列
├── consume_one.py        # 从 Redis 队列取一条执行（--all 跑完所有）
└── wipe_all.py           # 一键清空 Redis / OpenSearch / COS（危险）

docs/
├── 需求文档.md
├── 架构文档.md
├── 接口文档.md
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



```



##  配置同步到txt

```
pip freeze > unins.txt

```

## 卸载命令

```
pip uninstall -y -r unins.txt



