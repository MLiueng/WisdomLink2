"""对象存储（ADR-007）：本地目录 / S3 协议（boto3 可选），原件与预览缓存。"""
import os
import shutil
from pathlib import Path
from app.config import get_settings


class ObjectStore:
    def put(self, key: str, data: bytes) -> str:
        raise NotImplementedError

    def get(self, key: str) -> bytes | None:
        raise NotImplementedError

    def path(self, key: str) -> str | None:  # 本地文件路径（预览等需要）；S3 为 None
        return None


class LocalStore(ObjectStore):
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key, data):
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)   # 原子写入：中断不会产生半成品；replace 在 Windows 上覆盖已存在目标（rename 不行）
        return key

    def get(self, key):
        p = self.root / key
        return p.read_bytes() if p.exists() else None

    def path(self, key):
        p = self.root / key
        return str(p) if p.exists() else None


class S3Store(ObjectStore):
    def __init__(self, endpoint: str, key: str, secret: str, bucket: str):
        import boto3  # 可选依赖
        self.cli = boto3.client("s3", endpoint_url=endpoint or None, aws_access_key_id=key,
                                aws_secret_access_key=secret)
        self.bucket = bucket

    def put(self, key, data):
        self.cli.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key

    def get(self, key):
        try:
            return self.cli.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.cli.exceptions.NoSuchKey:
            return None


_store: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    global _store
    if _store is None:
        cfg = get_settings()
        if cfg.obj_driver == "s3" and cfg.s3_endpoint:
            _store = S3Store(cfg.s3_endpoint, cfg.s3_key, cfg.s3_secret, cfg.s3_bucket)
        else:
            _store = LocalStore(cfg.obj_local_dir)
    return _store


def wipe_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
