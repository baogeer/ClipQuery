"""腾讯云 COS 客户端封装。

对外暴露：
- get_cos()           单例客户端
- upload_file(path)   本地文件 → COS，返回 https URL
- object_url(key)     拼 COS 公网 URL（不请求网络）
- exists(key)         判断对象是否已存在，用于幂等上传
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from qcloud_cos import CosConfig, CosS3Client
from qcloud_cos.cos_exception import CosServiceError

from .config import settings


@lru_cache(maxsize=1)
def get_cos() -> CosS3Client:
    cfg = CosConfig(
        Region=settings.cos_region,
        SecretId=settings.cos_secret_id,
        SecretKey=settings.cos_secret_key,
        Scheme=settings.cos_scheme,
    )
    return CosS3Client(cfg)


def object_url(key: str) -> str:
    """拼 COS 公网 URL，不发请求。key 不要带前导斜杠。"""
    key = key.lstrip("/")
    return f"{settings.cos_scheme}://{settings.cos_bucket}.cos.{settings.cos_region}.myqcloud.com/{key}"


def exists(key: str) -> bool:
    client = get_cos()
    try:
        client.head_object(Bucket=settings.cos_bucket, Key=key)
        return True
    except CosServiceError as e:
        if e.get_status_code() == 404:
            return False
        raise


def upload_file(local_path: str | Path, key: str, *, skip_if_exists: bool = True) -> str:
    """上传本地文件到 COS key，返回对象的 https URL。

    skip_if_exists=True 时，如果对象已存在则不重传，直接返回 URL（幂等）。
    """
    local_path = Path(local_path)
    if not local_path.exists():
        raise FileNotFoundError(local_path)

    key = key.lstrip("/")
    if skip_if_exists and exists(key):
        return object_url(key)

    client = get_cos()
    # upload_file 内部会自动分片处理大文件（> 5MB 自动走分片上传）
    client.upload_file(
        Bucket=settings.cos_bucket,
        LocalFilePath=str(local_path),
        Key=key,
        EnableMD5=False,
        progress_callback=None,
    )
    return object_url(key)


def upload_and_delete(local_path: str | Path, key: str, *, skip_if_exists: bool = True) -> str:
    """上传后立即删除本地文件。上传失败则保留本地文件，便于重试。"""
    url = upload_file(local_path, key, skip_if_exists=skip_if_exists)
    try:
        Path(local_path).unlink(missing_ok=True)
    except OSError as e:
        print(f"[WARN] 删除本地文件失败 {local_path}: {e}")
    return url


def article_key(article_id: str, filename: str) -> str:
    """约定：文章产物路径为 <prefix>/<id>/<filename>。"""
    return f"{settings.cos_prefix.strip('/')}/{article_id}/{filename}"


def thumbnail_key(article_id: str, frame_id: str) -> str:
    """缩略图路径：<prefix>/<article_id>/thumbnails/<frame_id>.jpg"""
    return f"{settings.cos_prefix.strip('/')}/{article_id}/thumbnails/{frame_id}.jpg"


def clip_key(article_id: str, clip_id: str) -> str:
    """片段 MP4 路径：<prefix>/<article_id>/clips/<clip_id>.mp4"""
    return f"{settings.cos_prefix.strip('/')}/{article_id}/clips/{clip_id}.mp4"


def article_prefix(article_id: str) -> str:
    """一篇文章在 COS 上所有产物的公共前缀：<prefix>/<article_id>/"""
    return f"{settings.cos_prefix.strip('/')}/{article_id}/"


def delete_prefix(prefix: str) -> int:
    """删除某前缀下所有对象。返回删除对象数。用于 purge_article 强制重跑。"""
    prefix = prefix.lstrip("/")
    client = get_cos()
    deleted = 0
    marker = ""
    while True:
        res = client.list_objects(
            Bucket=settings.cos_bucket,
            Prefix=prefix,
            Marker=marker,
            MaxKeys=1000,
        )
        objs = res.get("Contents") or []
        if not objs:
            break
        batch = [{"Key": o["Key"]} for o in objs]
        # delete_objects 一次最多 1000 个
        client.delete_objects(
            Bucket=settings.cos_bucket,
            Delete={"Object": batch, "Quiet": "true"},
        )
        deleted += len(batch)
        if res.get("IsTruncated") in ("true", True):
            marker = res.get("NextMarker") or batch[-1]["Key"]
        else:
            break
    return deleted

