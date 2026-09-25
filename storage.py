"""Storage backends for relay images.

Two backends are available and selected with ``STORAGE_BACKEND``:

``local``
    Writes images to a directory and serves them from the relay itself. This
    only works while the receiving device is on the same network. Images are
    removed after ``IMAGE_RETENTION_MINUTES``.

``r2``
    Uploads images to Cloudflare R2 (or any S3-compatible service) and returns
    a presigned URL that expires after ``URL_TTL_SECONDS``. The bucket stays
    private: an unsigned request is rejected. This makes the image reachable
    from outside the local network without exposing the relay or Home
    Assistant to the internet.

Both backends expire images. Expiry is best effort for deletion but exact for
access: once a presigned URL is past its TTL it stops working regardless of
whether the object still exists.
"""

import logging
import threading
import time
from pathlib import Path

LOGGER = logging.getLogger("mobotix-relay.storage")

SWEEP_MIN_INTERVAL = 30.0
SWEEP_MAX_INTERVAL = 300.0


def _sweep_interval(retention_seconds: float) -> float:
    """Sweep often enough to honour the retention window, but not excessively."""
    if retention_seconds <= 0:
        return SWEEP_MAX_INTERVAL
    return max(SWEEP_MIN_INTERVAL, min(SWEEP_MAX_INTERVAL, retention_seconds / 2))


def _log_sweep_error(error: Exception) -> None:
    LOGGER.warning("Expiry sweep failed error=%s", error)


class LocalStorage:
    """Stores images on disk and serves them from the relay."""

    name = "local"

    def __init__(self, image_dir: str, retention_seconds: float = 0.0) -> None:
        self.image_dir = Path(image_dir)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.retention_seconds = retention_seconds

    def store(self, key: str, data: bytes, content_type: str) -> None:
        (self.image_dir / key).write_bytes(data)

    def url_for(self, key: str, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/images/{key}"

    def sweep(self) -> int:
        """Delete images older than the retention window. Returns count."""
        if self.retention_seconds <= 0:
            return 0
        cutoff = time.time() - self.retention_seconds
        removed = 0
        for path in self.image_dir.iterdir():
            removed += self._remove_if_expired(path, cutoff)
        if removed:
            LOGGER.info("Expired local images removed=%s", removed)
        return removed

    def _remove_if_expired(self, path: Path, cutoff: float) -> int:
        try:
            return self._unlink_expired(path, cutoff)
        except OSError as error:
            LOGGER.warning("Could not remove %s error=%s", path.name, error)
            return 0

    def _unlink_expired(self, path: Path, cutoff: float) -> int:
        if not path.is_file():
            return 0
        if path.stat().st_mtime >= cutoff:
            return 0
        path.unlink()
        return 1


class R2Storage:
    """Uploads images to Cloudflare R2 and returns presigned URLs."""

    name = "r2"

    def __init__(
        self,
        bucket: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        url_ttl_seconds: int = 900,
        key_prefix: str = "",
        client=None,
    ) -> None:
        self.bucket = bucket
        self.url_ttl_seconds = url_ttl_seconds
        self.key_prefix = key_prefix.strip("/")
        self.client = client or self._build_client(
            endpoint_url, access_key_id, secret_access_key
        )

    @staticmethod
    def _build_client(endpoint_url: str, access_key_id: str, secret: str):
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    def _full_key(self, key: str) -> str:
        if not self.key_prefix:
            return key
        return f"{self.key_prefix}/{key}"

    def store(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._full_key(key),
            Body=data,
            ContentType=content_type,
        )

    def url_for(self, key: str, base_url: str) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._full_key(key)},
            ExpiresIn=self.url_ttl_seconds,
        )

    def sweep(self) -> int:
        """Delete objects whose presigned URLs have expired. Returns count."""
        cutoff = time.time() - self.url_ttl_seconds
        expired = [
            {"Key": item["Key"]}
            for item in self._list_objects()
            if item["LastModified"].timestamp() < cutoff
        ]
        if not expired:
            return 0
        self.client.delete_objects(
            Bucket=self.bucket, Delete={"Objects": expired}
        )
        LOGGER.info("Expired R2 objects removed=%s", len(expired))
        return len(expired)

    def _list_objects(self) -> list:
        params = {"Bucket": self.bucket, "MaxKeys": 1000}
        if self.key_prefix:
            params["Prefix"] = f"{self.key_prefix}/"
        response = self.client.list_objects_v2(**params)
        return response.get("Contents", [])


def start_sweeper(backend, retention_seconds: float) -> threading.Thread | None:
    """Run ``backend.sweep()`` periodically in a daemon thread."""
    if retention_seconds <= 0:
        return None
    interval = _sweep_interval(retention_seconds)

    def loop() -> None:
        while True:
            time.sleep(interval)
            _run_sweep(backend)

    thread = threading.Thread(target=loop, daemon=True, name="image-sweeper")
    thread.start()
    LOGGER.info(
        "Expiry sweeper started backend=%s interval=%ss retention=%ss",
        backend.name,
        int(interval),
        int(retention_seconds),
    )
    return thread


def _run_sweep(backend) -> None:
    try:
        backend.sweep()
    except Exception as error:  # noqa: BLE001 - sweeper must never die
        _log_sweep_error(error)
