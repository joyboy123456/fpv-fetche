"""Cloudflare R2（S3 兼容）：上传、列举、预签名直链。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from web.library import VIDEO_EXTS, VideoItem, item_from_relpath

if TYPE_CHECKING:
    from fetcher import Config


class R2Store:
    def __init__(
        self,
        *,
        account_id: str,
        access_key: str,
        secret: str,
        bucket: str,
    ):
        import boto3
        from botocore.config import Config as BotoConfig

        kwargs = dict(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        )
        try:
            cfg = BotoConfig(
                **kwargs,
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            )
        except TypeError:
            cfg = BotoConfig(**kwargs)
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret,
            region_name="auto",
            config=cfg,
        )

    @classmethod
    def from_config(cls, cfg: Config) -> R2Store | None:
        if not (
            cfg.r2_account_id
            and cfg.r2_access_key
            and cfg.r2_secret
            and cfg.r2_bucket
        ):
            return None
        return cls(
            account_id=cfg.r2_account_id,
            access_key=cfg.r2_access_key,
            secret=cfg.r2_secret,
            bucket=cfg.r2_bucket,
        )

    def upload_file(self, local_path, key: str, content_type: str) -> None:
        from boto3.s3.transfer import TransferConfig

        xfer = TransferConfig(
            multipart_threshold=64 * 1024 * 1024,
            multipart_chunksize=16 * 1024 * 1024,
            max_concurrency=2,
        )
        extra = {"ContentType": content_type}
        self.client.upload_file(
            str(local_path),
            self.bucket,
            key,
            ExtraArgs=extra,
            Config=xfer,
        )

    def head_size(self, key: str) -> int | None:
        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=key)
        except self.client.exceptions.ClientError:
            return None
        return int(resp.get("ContentLength") or 0)

    def exists(self, key: str) -> bool:
        return self.head_size(key) is not None

    def presign(self, key: str, expires: int = 21600) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )

    def list_videos(self) -> list[VideoItem]:
        items: list[VideoItem] = []
        token = None
        while True:
            kwargs = {"Bucket": self.bucket, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents") or []:
                key = obj.get("Key") or ""
                if key.startswith(".") or "/." in key:
                    continue
                suffix = key[key.rfind(".") :].lower() if "." in key else ""
                if suffix not in VIDEO_EXTS:
                    continue
                if key.endswith(".part"):
                    continue
                last = obj.get("LastModified")
                mtime = last.timestamp() if last is not None else 0.0
                parsed = item_from_relpath(key, int(obj.get("Size") or 0), mtime)
                if parsed:
                    items.append(parsed)
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
            if not token:
                break
        items.sort(key=lambda v: (v.mtime, v.relpath), reverse=True)
        return items


def publish_local_video(cfg: Config, local_path, delete_local: bool = True) -> str:
    """把本地成片（和封面）推上 R2，成功后删掉本地视频省盘。"""
    store = R2Store.from_config(cfg)
    if store is None:
        raise RuntimeError("R2 未配置")
    rel = local_path.relative_to(cfg.download_root).as_posix()
    store.upload_file(local_path, rel, "video/mp4")
    from web.thumbs import ThumbStore

    thumbs = ThumbStore(cfg.download_root)
    poster = thumbs.ensure(rel, local_path, wait=90.0)
    if poster is not None:
        store.upload_file(poster, thumbs.r2_key(rel), "image/jpeg")
    if delete_local:
        local_path.unlink(missing_ok=True)
    return rel
