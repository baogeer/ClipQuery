# ClipQuery API 文档

> 版本：v0.3
> Base URL（本地默认）：`http://localhost:8000`
> 交互式文档（FastAPI 自带）：`/docs` / `/redoc`

---

## 1. 认证

当前版本**无认证**，内部网络部署。生产环境上线前需在反向代理层加 IP 白名单或 Token（见 [部署文档](./部署文档.md#安全)）。

---

## 2. 端点列表

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `POST` | `/ingest` | 异步入库视频 |
| `GET` | `/ingest/{job_id}` | 查询入库任务状态 |
| `POST` | `/search` | 语义检索（文本 / 图片 / 混合） |

---

## 3. `GET /health`

**用途**：确认服务存活 + OpenSearch 连通。

**响应 200**：
```json
{
  "status": "ok",
  "indexed_frames": 12480
}
```

- `indexed_frames` 是 `clipquery_frames` 索引的当前文档数。为 0 说明还没入库。

---

## 4. `POST /search`

**用途**：根据文本或图片在视频库中找最相近的片段。

### 4.1 请求（`multipart/form-data`）

| 字段 | 类型 | 必需 | 说明 |
|---|---|---|---|
| `query` | string | 二选一 | 自然语言描述 |
| `image` | file (jpg/png/...) | 二选一 | 参考图 |
| `top_k` | int | 否（默认 10） | 返回片段数 |

**规则**：`query` 和 `image` **至少提供一个**。两个都给时内部取向量平均再归一化（文字 + 图参考混合语义）。

### 4.2 请求示例

```bash
# 以文搜
curl -X POST http://localhost:8000/search \
  -F 'query=一个人在海边奔跑' \
  -F 'top_k=5'

# 以图搜
curl -X POST http://localhost:8000/search \
  -F 'image=@reference.jpg' \
  -F 'top_k=5'

# 混合
curl -X POST http://localhost:8000/search \
  -F 'query=sunset over mountains' \
  -F 'image=@mood_ref.jpg'
```

### 4.3 响应 200

```json
{
  "results": [
    {
      "clip_id":       "f3a8c1d9e2b5a7c4d6e9",
      "article_id":    "a1b2c3d4e5f6a7b8",
      "article_url":   "https://news.cgtn.com/news/2026-11-03/....../index.html",
      "title":         "Along the Coast: Running and the Rhythm of the Sea",
      "start_time":    12.0,
      "end_time":      18.0,
      "clip_url":      "https://video-metainsight-test-1326743305.cos.ap-beijing.myqcloud.com/ClipQuery/<article_id>/clips/<clip_id>.mp4",
      "thumbnail":     "https://.../ClipQuery/<article_id>/thumbnails/<frame_id>.jpg",
      "score":         0.874,
      "hit_count":     6,
      "best_frame_id": "d6e9f3a8c1d9e2b5a7c4",
      "paragraphs": [
        "The morning tide brings runners from across the city...",
        "Local residents say the beach run has become a..."
      ]
    }
  ]
}
```

### 4.4 字段语义

| 字段 | 说明 |
|---|---|
| `clip_id` | 片段唯一 id，可用于前端去重 |
| `article_id` | 所属文章 id = sha1(article_url)[:16] |
| `article_url` | CGTN 原文详情页，前端可做"查看原文"链接 |
| `title` | 新闻标题 |
| `start_time` / `end_time` | 片段在原视频中的时间范围（秒） |
| `clip_url` | **COS 上独立的 MP4 文件**，可直接丢 `<video>` 标签播放 |
| `thumbnail` | 片段中命中帧的缩略图 URL（也在 COS 上） |
| `score` | 余弦相似度 [-1, 1]，通常 > 0.2 才有意义 |
| `hit_count` | 该片段里命中 KNN top-K 的帧数，越高越可信 |
| `paragraphs` | 原新闻正文段落，可做上下文展示 |

### 4.5 错误

| 状态 | 场景 |
|---|---|
| 400 | `query` 和 `image` 都没给 |
| 500 | OpenSearch 不可达 / CLIP 模型加载失败 |

### 4.6 前端最小集成示例

```html
<div>
  <h3>{{ result.title }}</h3>
  <a href="{{ result.article_url }}" target="_blank">查看原文</a>
  <video src="{{ result.clip_url }}" poster="{{ result.thumbnail }}" controls></video>
  <p>命中时间段：{{ result.start_time }}s - {{ result.end_time }}s</p>
  <p>相似度：{{ (result.score * 100).toFixed(1) }}%</p>
</div>
```

---

## 5. `POST /ingest`

**用途**：把一个视频推入"切片 + 编码 + 索引"流水线，异步执行。

### 5.1 请求（`multipart/form-data`）

三选一，**只能提供一个**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `file` | file | 上传视频文件（MP4 推荐） |
| `path` | string | **服务器本地绝对路径**，已有视频就不用重传 |
| `url` | string | **CGTN 文章 URL**，后台自动抓取 + 入库 |

**典型用法**：
- 服务化入库：`url=https://news.cgtn.com/news/.../index.html`，一步到位
- 已经爬过：`path=/srv/clipquery/news/<aid>/video.mp4`
- 临时视频：`file=@video.mp4`（限 FastAPI 上传大小）

### 5.2 请求示例

```bash
# 自动抓取 + 入库
curl -X POST http://localhost:8000/ingest \
  -F 'url=https://news.cgtn.com/news/2026-11-03/VHJhbnNjcmlwdDkzMTAw/index.html'

# 本地路径
curl -X POST http://localhost:8000/ingest \
  -F 'path=/srv/clipquery/news/a1b2c3d4e5f6a7b8/video.mp4'

# 上传
curl -X POST http://localhost:8000/ingest -F 'file=@video.mp4'
```

### 5.3 响应 200

```json
// url 入库
{
  "job_id":      "9f8e7d6c5b4a39281...",
  "status":      "queued",
  "video_path":  null,
  "article_url": "https://news.cgtn.com/..."
}

// file / path 入库
{
  "job_id":      "9f8e7d6c5b4a39281...",
  "status":      "queued",
  "video_path":  "/srv/clipquery/news/<aid>/video.mp4",
  "article_url": null
}
```

### 5.4 错误

| 状态 | 场景 |
|---|---|
| 400 | `file` / `path` / `url` 都没给，或给了多个 |
| 404 | `path` 指定的文件不存在 |

### 5.5 后台执行细节

调用立即返回。后台顺序执行：

**`url` 入库（多一步爬虫）**：
1. `clipquery.website.cgtn.process_one_article(url)` — 爬页面 → 下视频 → 上传 COS → 写 `clipquery_articles`
2. 后面步骤同 `path` / `file` 入库

**`path` / `file` 入库**：
1. `cut_clips()` — 场景检测 + ffmpeg 切片
2. 每个片段上传 COS → 写 `clipquery_clips`
3. 抽帧（1 FPS） + CLIP 编码
4. 缩略图上传 COS
5. `vector_store.add()` 写 `clipquery_frames`
6. `rmtree(news/<aid>/)` 清本地

**并发**：同一 URL 的爬虫阶段有文件锁保护（`news/<aid>/.lock`）；同一视频的 indexer 阶段没有锁，依靠 OpenSearch id 幂等。生产环境最好在上层调度器确保同 URL 只投一次。

### 5.6 任务状态生命周期

```
url 入库:  queued → crawling → ingesting → done
                              └→ failed
path/file: queued → ingesting → done
                              └→ failed
```

**重要限制**：
- `_JOBS` 是**进程内 dict**，服务重启丢所有历史任务。
- 多 worker（`uvicorn --workers N`）下，task 创建在哪个 worker 就只在哪个 worker 的内存里能查。生产部署要么单 worker，要么上 Celery。

---

## 6. `GET /ingest/{job_id}`

### 6.1 请求

```bash
curl http://localhost:8000/ingest/9f8e7d6c5b4a39281...
```

### 6.2 响应 200

```json
// url 入库，爬完 + 入库完
{
  "job_id":         "9f8e7d6c5b4a39281...",
  "status":         "done",
  "video_path":     "/srv/clipquery/news/<aid>/video.mp4",
  "article_url":    "https://news.cgtn.com/...",
  "article_id":     "a1b2c3d4e5f6a7b8",
  "frames_written": 182,
  "error":          null
}

// path/file 入库
{
  "job_id":         "9f8e7d6c5b4a39281...",
  "status":         "done",
  "video_path":     "/srv/clipquery/news/<aid>/video.mp4",
  "article_url":    null,
  "article_id":     null,
  "frames_written": 182,
  "error":          null
}
```

**字段**：
- `status`: `queued` / `crawling` / `ingesting` / `done` / `failed`
- `article_url` / `article_id`: 仅 url 入库才有，爬完后填充
- `video_path`: url 入库在 `crawling` 阶段为 null，爬完后填充
- `frames_written`: 只有 `done` 时有值，等于写入 `clipquery_frames` 的帧数
- `error`: 只有 `failed` 时有值，字符串化的异常

### 6.3 错误

| 状态 | 场景 |
|---|---|
| 404 | `job_id` 不存在（本进程没见过该 id） |

---

## 7. Pydantic 模型清单

全部模型定义在 `clipquery/api/main.py`。以下为合约结构，字段改动需发版说明。

### 7.1 `SegmentOut`
```python
class SegmentOut(BaseModel):
    clip_id: str
    article_id: str
    article_url: Optional[str] = None
    title: Optional[str] = None
    start_time: float
    end_time: float
    clip_url: str
    thumbnail: Optional[str] = None
    score: float
    hit_count: int
    best_frame_id: Optional[str] = None
    paragraphs: list[str] = []
```

### 7.2 `SearchResponse`
```python
class SearchResponse(BaseModel):
    results: list[SegmentOut]
```

### 7.3 `IngestResponse`
```python
class IngestResponse(BaseModel):
    job_id: str
    status: str                             # "queued" | "crawling" | "ingesting" | "done" | "failed"
    video_path: Optional[str] = None        # path/file 入库才有
    article_url: Optional[str] = None       # url 入库才有
```

### 7.4 `JobStatus`
```python
class JobStatus(BaseModel):
    job_id: str
    status: str
    video_path: Optional[str] = None        # crawling 阶段尚未就绪
    article_url: Optional[str] = None
    article_id: Optional[str] = None        # 爬取完成后才有
    frames_written: Optional[int] = None    # done 时有
    error: Optional[str] = None             # failed 时有
```

---

## 8. 速率与成本

| 操作 | 典型耗时（CPU） | 典型耗时（GPU） | 主要成本 |
|---|---|---|---|
| `POST /search` | 150–400 ms | 80–200 ms | CLIP 文本编码 + 1 次 KNN |
| `POST /ingest` 返回 | < 50 ms | — | 只入队 |
| `POST /ingest` 后台处理（1 分钟视频） | 2–5 分钟 | 30–60 秒 | 主要是 CLIP 编码 |
| `GET /ingest/{job_id}` | < 10 ms | — | 内存 dict 查询 |

---

## 9. 相关文档

- [需求文档](./需求文档.md)
- [架构文档](./架构文档.md)
- [部署文档](./部署文档.md)
