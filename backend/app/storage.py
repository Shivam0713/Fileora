"""Private local or Cloudflare R2 object storage for temporary user files."""
from functools import lru_cache
from pathlib import Path
import shutil
from typing import BinaryIO

from app.core.config import settings


def is_r2() -> bool:
    return settings.storage_backend.strip().lower() == "r2"


def storage_is_configured() -> bool:
    if not is_r2():
        return True
    return bool(
        settings.r2_bucket_name
        and settings.r2_access_key_id
        and settings.r2_secret_access_key
        and (settings.r2_endpoint_url or settings.r2_account_id)
    )


def _key(storage_key: str) -> str:
    prefix = settings.r2_prefix.strip("/")
    return f"{prefix}/{storage_key}" if is_r2() and prefix else storage_key


@lru_cache(maxsize=1)
def _r2_client():
    if not storage_is_configured():
        raise RuntimeError("Cloudflare R2 storage is selected but its credentials are incomplete.")
    import boto3
    from botocore.config import Config

    endpoint = settings.r2_endpoint_url or f"https://{settings.r2_account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="auto",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def put_fileobj(storage_key: str, fileobj: BinaryIO) -> None:
    if is_r2():
        _r2_client().upload_fileobj(fileobj, settings.r2_bucket_name, _key(storage_key))
        return
    target = Path(settings.storage_dir) / storage_key
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as destination:
        while chunk := fileobj.read(1024 * 1024):
            destination.write(chunk)


def put_path(storage_key: str, source: Path) -> None:
    with source.open("rb") as fileobj:
        put_fileobj(storage_key, fileobj)


def download_to_path(storage_key: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if is_r2():
        _r2_client().download_file(settings.r2_bucket_name, _key(storage_key), str(target))
        return
    source = Path(settings.storage_dir) / storage_key
    shutil.copyfile(source, target)


def open_read(storage_key: str):
    if is_r2():
        return _r2_client().get_object(Bucket=settings.r2_bucket_name, Key=_key(storage_key))["Body"]
    return (Path(settings.storage_dir) / storage_key).open("rb")


def delete(storage_key: str) -> None:
    if is_r2():
        _r2_client().delete_object(Bucket=settings.r2_bucket_name, Key=_key(storage_key))
        return
    (Path(settings.storage_dir) / storage_key).unlink(missing_ok=True)
