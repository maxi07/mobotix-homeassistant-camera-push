import io
import time
from unittest.mock import Mock, patch

import pytest

from app import build_storage, create_app, derive_r2_endpoint
from storage import LocalStorage, R2Storage, _sweep_interval


JPEG_BYTES = b"\xff\xd8camera-image\xff\xd9"


class FakeR2Client:
    """Minimal stand-in for the boto3 S3 client."""

    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.put_calls = []

    def put_object(self, Bucket, Key, Body, ContentType):
        self.put_calls.append((Bucket, Key, ContentType))
        self.objects[Key] = {"body": Body, "modified": time.time()}

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
        key = Params["Key"]
        return (
            f"https://account.eu.r2.cloudflarestorage.com/{key}"
            f"?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=testsignature"
        )

    def list_objects_v2(self, Bucket, MaxKeys, Prefix=None):
        from datetime import datetime, timezone

        contents = []
        for key, value in self.objects.items():
            if Prefix and not key.startswith(Prefix):
                continue
            contents.append(
                {
                    "Key": key,
                    "LastModified": datetime.fromtimestamp(
                        value["modified"], tz=timezone.utc
                    ),
                }
            )
        return {"Contents": contents}

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.deleted.append(item["Key"])
            self.objects.pop(item["Key"], None)


def make_r2(**kwargs):
    client = FakeR2Client()
    settings = {
        "bucket": "doorbell",
        "endpoint_url": "https://account.eu.r2.cloudflarestorage.com",
        "access_key_id": "key",
        "secret_access_key": "secret",
        "url_ttl_seconds": 900,
        "client": client,
    }
    settings.update(kwargs)
    return R2Storage(**settings), client


# --- endpoint derivation ---------------------------------------------------


def test_eu_jurisdiction_uses_eu_endpoint():
    assert derive_r2_endpoint("abc123", "eu") == (
        "https://abc123.eu.r2.cloudflarestorage.com"
    )


def test_default_jurisdiction_uses_plain_endpoint():
    assert derive_r2_endpoint("abc123", "") == (
        "https://abc123.r2.cloudflarestorage.com"
    )


def test_jurisdiction_is_case_insensitive():
    assert derive_r2_endpoint("abc123", "EU") == (
        "https://abc123.eu.r2.cloudflarestorage.com"
    )


def test_missing_account_id_yields_no_endpoint():
    assert derive_r2_endpoint("", "eu") == ""


# --- R2 storage ------------------------------------------------------------


def test_r2_upload_and_presigned_url():
    storage, client = make_r2()

    storage.store("abc.jpg", JPEG_BYTES, "image/jpeg")
    url = storage.url_for("abc.jpg", "http://ignored")

    assert client.put_calls == [("doorbell", "abc.jpg", "image/jpeg")]
    assert url.startswith("https://account.eu.r2.cloudflarestorage.com/abc.jpg")
    assert "X-Amz-Signature=" in url
    assert "X-Amz-Expires=900" in url


def test_r2_key_prefix_is_applied():
    storage, client = make_r2(key_prefix="doorbell/")

    storage.store("abc.jpg", JPEG_BYTES, "image/jpeg")

    assert client.put_calls == [
        ("doorbell", "doorbell/abc.jpg", "image/jpeg")
    ]


def test_r2_sweep_removes_only_expired_objects():
    storage, client = make_r2(url_ttl_seconds=900)
    storage.store("fresh.jpg", JPEG_BYTES, "image/jpeg")
    storage.store("stale.jpg", JPEG_BYTES, "image/jpeg")
    client.objects["stale.jpg"]["modified"] = time.time() - 1000

    removed = storage.sweep()

    assert removed == 1
    assert client.deleted == ["stale.jpg"]
    assert "fresh.jpg" in client.objects


def test_r2_sweep_without_expired_objects_does_nothing():
    storage, client = make_r2()
    storage.store("fresh.jpg", JPEG_BYTES, "image/jpeg")

    assert storage.sweep() == 0
    assert client.deleted == []


# --- local storage retention (issue #1) ------------------------------------


def test_local_sweep_removes_old_images(tmp_path):
    storage = LocalStorage(str(tmp_path), retention_seconds=900)
    storage.store("fresh.jpg", JPEG_BYTES, "image/jpeg")
    storage.store("stale.jpg", JPEG_BYTES, "image/jpeg")
    stale = tmp_path / "stale.jpg"
    old = time.time() - 1000
    import os

    os.utime(stale, (old, old))

    removed = storage.sweep()

    assert removed == 1
    assert not stale.exists()
    assert (tmp_path / "fresh.jpg").exists()


def test_local_sweep_disabled_keeps_everything(tmp_path):
    storage = LocalStorage(str(tmp_path), retention_seconds=0)
    storage.store("old.jpg", JPEG_BYTES, "image/jpeg")
    old = time.time() - 100000
    import os

    os.utime(tmp_path / "old.jpg", (old, old))

    assert storage.sweep() == 0
    assert (tmp_path / "old.jpg").exists()


def test_sweep_interval_is_bounded():
    # Half the retention window, clamped to [30s, 300s].
    assert _sweep_interval(60) == 30.0
    assert _sweep_interval(300) == 150.0
    assert _sweep_interval(900) == 300.0
    assert _sweep_interval(100000) == 300.0


# --- backend selection -----------------------------------------------------


def base_settings(tmp_path, **extra):
    settings = {
        "HA_WEBHOOK_URL": "http://server3:8123/api/webhook/test",
        "IMAGE_DIR": str(tmp_path / "images"),
        "TESTING": True,
    }
    settings.update(extra)
    return settings


def test_unknown_backend_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="must be 'local' or 'r2'"):
        create_app(base_settings(tmp_path, STORAGE_BACKEND="azure"))


def test_r2_backend_without_credentials_fails_loudly(tmp_path):
    with pytest.raises(RuntimeError, match="R2_BUCKET"):
        create_app(
            base_settings(
                tmp_path,
                STORAGE_BACKEND="r2",
                R2_BUCKET="",
                R2_ACCESS_KEY_ID="",
                R2_SECRET_ACCESS_KEY="",
                R2_ACCOUNT_ID="",
                R2_ENDPOINT_URL="",
                R2_JURISDICTION="",
                R2_KEY_PREFIX="",
                URL_TTL_SECONDS=900,
                IMAGE_RETENTION_MINUTES=60,
            )
        )


def test_local_backend_is_the_default(tmp_path):
    app = create_app(
        base_settings(
            tmp_path,
            PUBLIC_BASE_URL="http://relay.test:18425",
            IMAGE_RETENTION_MINUTES=60,
            URL_TTL_SECONDS=900,
            STORAGE_BACKEND="local",
        )
    )
    assert app.config["STORAGE"].name == "local"


# --- end-to-end through the relay ------------------------------------------


def test_event_with_r2_backend_returns_presigned_url(tmp_path):
    storage, client = make_r2()
    app = create_app(
        base_settings(
            tmp_path,
            STORAGE_BACKEND="r2",
            STORAGE=storage,
            RETRY_COUNT=1,
            PUBLIC_BASE_URL="",
            IMAGE_RETENTION_MINUTES=60,
            URL_TTL_SECONDS=900,
        )
    )
    ha_response = Mock()
    ha_response.raise_for_status.return_value = None

    with patch("app.requests.post", return_value=ha_response) as post:
        response = app.test_client().post(
            "/",
            data={
                "message": "Doorbell",
                "image": (io.BytesIO(JPEG_BYTES), "door.jpg", "image/jpeg"),
            },
        )

    assert response.status_code == 200
    image_url = post.call_args.kwargs["data"]["image_url"]
    assert image_url.startswith("https://account.eu.r2.cloudflarestorage.com/")
    assert "X-Amz-Signature=" in image_url
    assert len(client.put_calls) == 1


def test_storage_failure_returns_502(tmp_path):
    storage, client = make_r2()
    storage.store = Mock(side_effect=RuntimeError("bucket unreachable"))
    app = create_app(
        base_settings(
            tmp_path,
            STORAGE_BACKEND="r2",
            STORAGE=storage,
            RETRY_COUNT=1,
            IMAGE_RETENTION_MINUTES=60,
            URL_TTL_SECONDS=900,
        )
    )

    with patch("app.requests.post") as post:
        response = app.test_client().post(
            "/",
            data={
                "image": (io.BytesIO(JPEG_BYTES), "door.jpg", "image/jpeg"),
            },
        )

    assert response.status_code == 502
    assert post.call_count == 0
