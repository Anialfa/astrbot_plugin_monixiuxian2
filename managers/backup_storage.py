"""Upload complete update snapshots to an isolated Cloudflare R2 prefix."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import tarfile
import tempfile
from uuid import uuid4


class BackupUploadError(RuntimeError):
    pass


def load_r2_config(data_dir):
    path = Path(data_dir) / "backup_r2.json"
    if not path.exists():
        return None
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict) or type(config.get("enabled", True)) is not bool:
            raise ValueError
        if not config.get("enabled", True):
            return None
        for key in ("endpoint_url", "bucket", "access_key_id", "secret_access_key"):
            if not isinstance(config.get(key), str) or not config[key].strip():
                raise ValueError
        if not re.fullmatch(r"https://[a-z0-9]+(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com/?", config["endpoint_url"]):
            raise ValueError
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", config["bucket"]):
            raise ValueError
        return config
    except (OSError, ValueError):
        raise BackupUploadError("R2 配置无效，请检查数据目录的 backup_r2.json。") from None


def _client(config):
    import boto3
    from botocore.config import Config

    return boto3.client("s3", endpoint_url=config["endpoint_url"], region_name="auto",
        aws_access_key_id=config["access_key_id"], aws_secret_access_key=config["secret_access_key"],
        config=Config(connect_timeout=15, read_timeout=60, retries={"mode": "standard", "max_attempts": 2},
                      request_checksum_calculation="when_required", response_checksum_validation="when_required"))


def _pack(backup, archive):
    # Only the completed snapshot is included; credentials/status/other backups stay outside it.
    names = ["plugin", "database.db", "plugin-settings.json", "manifest.json"]
    if (backup / "config").exists():
        names.append("config")
    def regular_only(info):
        if not (info.isfile() or info.isdir()):
            raise BackupUploadError("备份包含链接或特殊文件，已取消 R2 上传。")
        return info
    with tarfile.open(archive, "w:gz") as bundle:
        for name in names:
            bundle.add(backup / name, arcname=name, filter=regular_only)


def upload_backup(backup, config, started_at):
    """Store a new object, then read it back to verify its bytes; never delete objects."""
    backup = Path(backup)
    date = datetime.fromtimestamp(started_at, timezone(timedelta(hours=8)))
    key = date.strftime("xiuxian/%Y/%m/%d/xiuxian_%Y%m%d_%H%M%S_") + uuid4().hex + ".tar.gz"
    try:
        with tempfile.TemporaryDirectory(prefix="r2-upload-", dir=backup) as directory:
            archive = Path(directory) / "backup.tar.gz"
            _pack(backup, archive)
            size = archive.stat().st_size
            if size > 5 * 1024 ** 3:
                raise BackupUploadError("备份超过单次上传的 5 GiB 上限，已取消更新。")
            with archive.open("rb") as stream:
                sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
                stream.seek(0)
                md5 = base64.b64encode(hashlib.file_digest(stream, "md5").digest()).decode("ascii")
            client = _client(config)
            try:
                with archive.open("rb") as stream:
                    client.put_object(Bucket=config["bucket"], Key=key, Body=stream,
                        ContentLength=size, ContentType="application/gzip", ContentMD5=md5,
                        Metadata={"sha256": sha256}, IfNoneMatch="*")
                head = client.head_object(Bucket=config["bucket"], Key=key)
                if head["ContentLength"] != size or head.get("Metadata", {}).get("sha256") != sha256:
                    raise BackupUploadError("R2 备份信息校验失败，已取消更新。")
                response = client.get_object(Bucket=config["bucket"], Key=key)
                body = response["Body"]
                try:
                    digest = hashlib.sha256()
                    received = 0
                    for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                        received += len(chunk)
                        digest.update(chunk)
                    if received != size or digest.hexdigest() != sha256:
                        raise BackupUploadError("R2 备份内容校验失败，已取消更新。")
                finally:
                    body.close()
            finally:
                client.close()
        return {"bucket": config["bucket"], "key": key, "sha256": sha256, "size": size, "verified": True}
    except BackupUploadError:
        raise
    except Exception:
        # SDK exceptions can contain request details. Keep credentials out of logs and chat.
        raise BackupUploadError("R2 备份上传或校验失败，请检查凭据、权限和网络；本地备份已保留。") from None
