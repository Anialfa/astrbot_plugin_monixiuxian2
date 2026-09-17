"""Check R2 isolation, archive contents, readback and conditional writes offline."""

import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
from unittest.mock import patch
import sys


def main(root):
    spec = importlib.util.spec_from_file_location("backup_storage", root / "managers/backup_storage.py")
    storage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(storage)
    results = {}
    config = {"enabled": True, "endpoint_url": "https://example.r2.cloudflarestorage.com",
              "bucket": "test-bucket", "access_key_id": "test-key", "secret_access_key": "test-secret"}

    class Body:
        def __init__(self, data):
            self.data = data
            self.closed = False

        def iter_chunks(self, chunk_size):
            yield self.data

        def close(self):
            self.closed = True

    class Client:
        def __init__(self, mode):
            self.mode = mode
            self.objects = {"backups/2026/09/17/other.sql.gz": b"untouched"}
            self.closed = False

        def put_object(self, **kwargs):
            assert kwargs["Bucket"] == "test-bucket"
            assert kwargs["Key"].startswith("xiuxian/2026/09/18/xiuxian_20260918_010000_")
            assert kwargs["IfNoneMatch"] == "*"
            assert kwargs["Key"] not in self.objects
            if self.mode == "collision":
                raise RuntimeError("PreconditionFailed test-secret")
            data = kwargs["Body"].read()
            assert len(data) == kwargs["ContentLength"]
            assert base64.b64encode(hashlib.md5(data).digest()).decode() == kwargs["ContentMD5"]
            self.metadata = kwargs["Metadata"]
            self.objects[kwargs["Key"]] = data

        def head_object(self, Bucket, Key):
            return {"ContentLength": len(self.objects[Key]) + (1 if self.mode == "wrong_size" else 0),
                    "Metadata": self.metadata}

        def get_object(self, Bucket, Key):
            self.body = Body(b"corrupted" if self.mode == "corrupted" else self.objects[Key])
            return {"Body": self.body}

        def close(self):
            self.closed = True

    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory)
        assert storage.load_r2_config(data) is None
        config_path = data / "backup_r2.json"
        for value in ("bad-json", "[]", '{"enabled": "false"}', '{}',
                      json.dumps({**config, "endpoint_url": "http://example.com"})):
            config_path.write_text(value)
            try:
                storage.load_r2_config(data)
                raise AssertionError("Invalid config accepted")
            except storage.BackupUploadError as error:
                assert "test-secret" not in str(error)
        config_path.write_text('{"enabled": false}')
        assert storage.load_r2_config(data) is None
        config_path.write_text(json.dumps(config))
        assert storage.load_r2_config(data) == config
        results["config_missing_disabled_valid_and_invalid"] = {"passed": True}

        backup = data / "backups/update-test"
        (backup / "plugin").mkdir(parents=True)
        (backup / "plugin/code.py").write_text("# test\n")
        (backup / "config").mkdir()
        (backup / "config/custom.json").write_text("{}")
        for name in ("database.db", "plugin-settings.json", "manifest.json"):
            (backup / name).write_text("snapshot")
        (backup / "backup_r2.json").write_text("test-secret")
        (backup / "failed-plugin").mkdir()
        (backup / "failed-plugin/private.txt").write_text("do-not-archive")
        started = storage.datetime(2026, 9, 17, 17, tzinfo=storage.timezone.utc).timestamp()
        for mode in ("success", "collision", "wrong_size", "corrupted"):
            client = Client(mode)
            with patch.object(storage, "_client", return_value=client):
                try:
                    result = storage.upload_backup(backup, config, started)
                    assert mode == "success"
                    assert result["verified"]
                    assert hashlib.sha256(client.objects[result["key"]]).hexdigest() == result["sha256"]
                    with tarfile.open(fileobj=io.BytesIO(client.objects[result["key"]]), mode="r:gz") as bundle:
                        assert set(bundle.getnames()) == {"plugin", "plugin/code.py", "database.db",
                            "plugin-settings.json", "manifest.json", "config", "config/custom.json"}
                except storage.BackupUploadError as error:
                    assert mode != "success"
                    assert "test-secret" not in str(error)
            assert client.objects["backups/2026/09/17/other.sql.gz"] == b"untouched"
            assert client.closed
            if hasattr(client, "body"):
                assert client.body.closed
            assert not list(backup.glob("r2-upload-*"))
            results[mode] = {"passed": True}
        (backup / "database.db").unlink()
        with patch.object(storage, "_client") as client:
            try:
                storage.upload_backup(backup, config, started)
                raise AssertionError("Incomplete snapshot uploaded")
            except storage.BackupUploadError:
                client.assert_not_called()
        results["incomplete_snapshot_not_uploaded"] = {"passed": True}
    print("BACKUP_RESULTS=" + json.dumps(results), flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
