"""Storage backends for relay images.

Two backends are available and selected with ``STORAGE_BACKEND``:

``local``
    Writes images to a directory and serves them from the relay itself. This
    only works while the receiving device can reach the relay. Images are
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

Note on the rejection of unsigned requests: that happens between the phone and
the object store, not inside this relay. The relay only ever creates the URL.
Errors handled here are the ones raised by the relay's own calls.
"""

import logging
import re
import threading
import time
from pathlib import Path

LOGGER = logging.getLogger("mobotix-relay.storage")

SWEEP_MIN_INTERVAL = 30.0
SWEEP_MAX_INTERVAL = 300.0

# S3 accepts at most 1000 keys per DeleteObjects request.
DELETE_BATCH_SIZE = 1000

# Objects created by this relay are named <uuid4-hex><suffix>. The sweeper
# only ever deletes keys matching this shape, so pointing the relay at a
# bucket that holds other data cannot destroy it.
#
# The suffix is deliberately unconstrained: when an image/* upload carries a
# signature none of the detectors recognise, app.py falls back to the client's
# own filename suffix, which can be any length and contain punctuation. A
# narrower pattern would silently exclude those objects from retention and
# leave them in the bucket forever. The 32-char hex prefix is what identifies
# an object as ours.
RELAY_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}\.[^/]+$")

# A UUID-shaped name is not proof of ownership: an unrelated object of the
# same shape could already sit in a shared bucket. Every upload therefore
# carries an explicit marker in its user metadata, and the sweeper deletes
# only objects that actually carry it.
OWNER_METADATA_KEY = "created-by"
OWNER_METADATA_VALUE = "mobotix-relay"

# Default namespace. Keeping relay objects under their own prefix means the
# sweeper never even lists anything else.
DEFAULT_KEY_PREFIX = "mobotix-relay"


class StorageError(RuntimeError):
    """A storage operation was rejected by the backend.

    Carries the HTTP status and the provider error code so callers can log
    something more useful than a stack trace.
    """

    def __init__(self, message: str, status=None, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# Cloudflare R2 answers with standard S3 error codes. These are the ones a
# misconfigured relay actually runs into; the hint names the setting to check.
ERROR_HINTS = {
    "InvalidAccessKeyId": (
        "R2_ACCESS_KEY_ID is unknown - the token may have been revoked"
    ),
    "SignatureDoesNotMatch": (
        "R2_SECRET_ACCESS_KEY does not match R2_ACCESS_KEY_ID"
    ),
    "InvalidArgument": (
        "malformed request - check R2_ENDPOINT_URL, it must not contain the "
        "bucket name"
    ),
    "AccessDenied": (
        "the API token lacks Object Read & Write permission on this bucket"
    ),
    "NoSuchBucket": (
        "bucket not found - check R2_BUCKET and that the token is scoped to it"
    ),
    "PermanentRedirect": (
        "wrong endpoint for this bucket - an EU bucket needs the .eu. endpoint"
    ),
    "AuthorizationHeaderMalformed": (
        "wrong signing region - set R2_SIGNING_REGION for non-R2 providers"
    ),
    "EntityTooLarge": "the image exceeds the size the store accepts per PUT",
}


def describe_error(operation: str, error: Exception) -> StorageError:
    """Turn a botocore exception into a StorageError with a usable message."""
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return StorageError(f"{operation} failed: {error}")
    metadata = response.get("ResponseMetadata") or {}
    status = metadata.get("HTTPStatusCode")
    code = (response.get("Error") or {}).get("Code", "")
    hint = ERROR_HINTS.get(code, "")
    message = f"{operation} failed with HTTP {status}"
    if code:
        message += f" ({code})"
    if hint:
        message += f" - {hint}"
    return StorageError(message, status=status, code=code)


def guard(operation: str, call, **kwargs):
    """Run a backend call and translate provider failures into StorageError."""
    try:
        return call(**kwargs)
    except StorageError:
        raise
    except Exception as error:  # noqa: BLE001 - translated below
        raise describe_error(operation, error) from error


def _sweep_interval(retention_seconds: float) -> float:
    """Sweep often enough to honour the retention window, but not excessively."""
    if retention_seconds <= 0:
        return SWEEP_MAX_INTERVAL
    return max(SWEEP_MIN_INTERVAL, min(SWEEP_MAX_INTERVAL, retention_seconds / 2))


def is_relay_object(key: str) -> bool:
    """True when the key looks like one this relay created."""
    return bool(RELAY_KEY_PATTERN.match(key.rsplit("/", 1)[-1]))


class LocalStorage:
    """Stores images on disk and serves them from the relay."""

    name = "local"

    def __init__(self, image_dir: str, retention_seconds: float = 0.0) -> None:
        self.image_dir = Path(image_dir)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.retention_seconds = retention_seconds

    def store(self, key: str, data: bytes, content_type: str) -> None:
        try:
            (self.image_dir / key).write_bytes(data)
        except OSError as error:
            raise StorageError(f"writing {key} failed: {error}") from error

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
    """Uploads images to an S3-compatible store and returns presigned URLs."""

    name = "r2"

    def __init__(
        self,
        bucket: str,
        endpoint_url: str,
        access_key_id: str,
        access_key_secret: str,
        url_ttl_seconds: int = 900,
        key_prefix: str = "",
        signing_region: str = "auto",
        client=None,
    ) -> None:
        self.bucket = bucket
        self.url_ttl_seconds = url_ttl_seconds
        self.key_prefix = key_prefix.strip("/")
        self.client = client or self._build_client(
            endpoint_url, access_key_id, access_key_secret, signing_region
        )

    @staticmethod
    def _build_client(endpoint_url, access_key_id, access_key_secret, region):
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=access_key_secret,
            region_name=region,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    def _full_key(self, key: str) -> str:
        if not self.key_prefix:
            return key
        return f"{self.key_prefix}/{key}"

    def store(self, key: str, data: bytes, content_type: str) -> None:
        guard(
            "upload",
            self.client.put_object,
            Bucket=self.bucket,
            Key=self._full_key(key),
            Body=data,
            ContentType=content_type,
            Metadata={OWNER_METADATA_KEY: OWNER_METADATA_VALUE},
        )

    def url_for(self, key: str, base_url: str) -> str:
        return guard(
            "signing",
            self.client.generate_presigned_url,
            ClientMethod="get_object",
            Params={"Bucket": self.bucket, "Key": self._full_key(key)},
            ExpiresIn=self.url_ttl_seconds,
        )

    def sweep(self) -> int:
        """Delete relay objects whose presigned URLs have expired.

        Ownership is established in three steps, each narrowing further:
        the configured prefix limits what is listed at all, the key shape
        filters obvious strangers, and the ownership marker written at
        upload time is verified per object before anything is deleted.
        A name alone is never taken as proof.
        """
        cutoff = time.time() - self.url_ttl_seconds
        candidates = [
            item["Key"]
            for item in self._list_objects()
            if self._is_expired(item, cutoff)
        ]
        if not candidates:
            return 0
        owned = [key for key in candidates if self._is_owned(key)]
        skipped = len(candidates) - len(owned)
        if skipped:
            LOGGER.info(
                "Left %s expired object(s) alone: no %s=%s marker",
                skipped,
                OWNER_METADATA_KEY,
                OWNER_METADATA_VALUE,
            )
        if not owned:
            return 0
        removed = self._delete_in_batches(owned)
        if removed:
            LOGGER.info("Expired objects removed=%s", removed)
        return removed

    @staticmethod
    def _is_expired(item: dict, cutoff: float) -> bool:
        if not is_relay_object(item["Key"]):
            return False
        return item["LastModified"].timestamp() < cutoff

    def _is_owned(self, key: str) -> bool:
        """Verify the ownership marker written by store().

        Objects that predate this check, or that belong to someone else,
        have no marker and are therefore never deleted. Any doubt -- a
        failed lookup included -- means the object stays.
        """
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as error:  # noqa: BLE001 - never delete on doubt
            LOGGER.warning(
                "Could not verify ownership of %s, leaving it alone: %s",
                key,
                error,
            )
            return False
        metadata = (head or {}).get("Metadata") or {}
        value = metadata.get(OWNER_METADATA_KEY)
        if value is None:
            value = metadata.get(OWNER_METADATA_KEY.replace("-", "_"))
        return value == OWNER_METADATA_VALUE

    def _delete_in_batches(self, keys: list) -> int:
        removed = 0
        for start in range(0, len(keys), DELETE_BATCH_SIZE):
            batch = keys[start : start + DELETE_BATCH_SIZE]
            removed += self._delete_batch(batch)
        return removed

    def _delete_batch(self, keys: list) -> int:
        response = guard(
            "delete",
            self.client.delete_objects,
            Bucket=self.bucket,
            Delete={"Objects": [{"Key": key} for key in keys]},
        )
        # DeleteObjects answers 200 even when individual keys failed.
        errors = (response or {}).get("Errors") or []
        for failure in errors:
            LOGGER.warning(
                "Could not delete %s code=%s message=%s",
                failure.get("Key"),
                failure.get("Code"),
                failure.get("Message"),
            )
        deleted = (response or {}).get("Deleted")
        if deleted is not None:
            return len(deleted)
        return len(keys) - len(errors)

    def _list_objects(self) -> list:
        """List every page, not just the first 1000 keys."""
        items = []
        token = None
        while True:
            response = guard(
                "listing", self.client.list_objects_v2, **self._list_params(token)
            )
            items.extend(response.get("Contents", []))
            token = response.get("NextContinuationToken")
            if not response.get("IsTruncated") or not token:
                return items

    def _list_params(self, token) -> dict:
        params = {"Bucket": self.bucket, "MaxKeys": DELETE_BATCH_SIZE}
        if self.key_prefix:
            params["Prefix"] = f"{self.key_prefix}/"
        if token:
            params["ContinuationToken"] = token
        return params

    def check(self) -> None:
        """Verify credentials and bucket access. Raises StorageError."""
        guard("listing", self.client.list_objects_v2, **self._list_params(None))


def start_sweeper(backend, retention_seconds: float):
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
    except StorageError as error:
        LOGGER.warning("Expiry sweep rejected: %s", error)
    except Exception as error:  # noqa: BLE001 - sweeper must never die
        LOGGER.warning("Expiry sweep failed error=%s", error)
