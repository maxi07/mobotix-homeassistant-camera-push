"""Tests fuer die Uebersetzung von Backend-Fehlern in StorageError."""
import io
from unittest.mock import Mock, patch

import pytest

from app import create_app
from storage import R2Storage, StorageError, describe_error, guard


JPEG_BYTES = b"\xff\xd8camera-image\xff\xd9"


class FakeClientError(Exception):
    """Nachbildung von botocore.exceptions.ClientError."""

    def __init__(self, status, code, message="rejected"):
        super().__init__(message)
        self.response = {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


def test_invalid_argument_400_names_the_endpoint_setting():
    error = describe_error("upload", FakeClientError(400, "InvalidArgument"))

    assert isinstance(error, StorageError)
    assert error.status == 400
    assert error.code == "InvalidArgument"
    assert "HTTP 400" in str(error)
    assert "R2_ENDPOINT_URL" in str(error)


def test_signature_mismatch_400_names_the_secret():
    error = describe_error(
        "upload", FakeClientError(400, "SignatureDoesNotMatch")
    )

    assert error.status == 400
    assert "R2_SECRET_ACCESS_KEY" in str(error)


def test_invalid_access_key_403_names_the_key_id():
    error = describe_error(
        "upload", FakeClientError(403, "InvalidAccessKeyId")
    )

    assert error.status == 403
    assert "R2_ACCESS_KEY_ID" in str(error)


def test_access_denied_names_the_token_permission():
    error = describe_error("upload", FakeClientError(403, "AccessDenied"))

    assert "Object Read & Write" in str(error)


def test_no_such_bucket_404_names_the_bucket_setting():
    error = describe_error("upload", FakeClientError(404, "NoSuchBucket"))

    assert error.status == 404
    assert "R2_BUCKET" in str(error)


def test_permanent_redirect_points_at_the_eu_endpoint():
    error = describe_error(
        "upload", FakeClientError(301, "PermanentRedirect")
    )

    assert ".eu." in str(error)


def test_unknown_error_code_still_reports_status():
    error = describe_error("upload", FakeClientError(400, "SomethingNew"))

    assert error.status == 400
    assert "HTTP 400" in str(error)
    assert "SomethingNew" in str(error)


def test_non_client_error_is_wrapped_without_status():
    error = describe_error("upload", ConnectionError("no route to host"))

    assert isinstance(error, StorageError)
    assert error.status is None
    assert "no route to host" in str(error)


def test_guard_passes_through_successful_calls():
    assert guard("listing", lambda **kw: {"ok": True}) == {"ok": True}


def test_guard_does_not_rewrap_storage_error():
    def boom(**kwargs):
        raise StorageError("already translated", status=400, code="X")

    with pytest.raises(StorageError) as caught:
        guard("upload", boom)

    assert str(caught.value) == "already translated"
    assert caught.value.code == "X"


# --- the failing call surfaces through the relay ---------------------------


class RejectingClient:
    """S3-Client, der jeden Upload mit einem echten 400 abweist."""

    def put_object(self, **kwargs):
        raise FakeClientError(400, "InvalidArgument")

    def list_objects_v2(self, **kwargs):
        return {"Contents": []}


def test_relay_returns_actionable_message_on_400(tmp_path):
    storage = R2Storage(
        bucket="doorbell",
        endpoint_url="https://account.eu.r2.cloudflarestorage.com",
        access_key_id="key",
        access_key_secret="secret",
        client=RejectingClient(),
    )
    app = create_app(
        {
            "HA_WEBHOOK_URL": "http://server3:8123/api/webhook/test",
            "IMAGE_DIR": str(tmp_path / "images"),
            "STORAGE_BACKEND": "r2",
            "STORAGE": storage,
            "RETRY_COUNT": 1,
            "URL_TTL_SECONDS": 900,
            "IMAGE_RETENTION_MINUTES": 60,
            "TESTING": True,
        }
    )

    with patch("app.requests.post") as post:
        response = app.test_client().post(
            "/",
            data={"image": (io.BytesIO(JPEG_BYTES), "d.jpg", "image/jpeg")},
        )

    assert response.status_code == 502
    # The caller learns which setting to fix, not just "failed".
    assert "R2_ENDPOINT_URL" in response.get_json()["error"]
    # Home Assistant is never contacted when the image could not be stored.
    assert post.call_count == 0


def test_sweeper_survives_a_rejected_listing():
    class BrokenClient:
        def list_objects_v2(self, **kwargs):
            raise FakeClientError(403, "AccessDenied")

    storage = R2Storage(
        bucket="doorbell",
        endpoint_url="https://account.eu.r2.cloudflarestorage.com",
        access_key_id="key",
        access_key_secret="secret",
        client=BrokenClient(),
    )

    from storage import _run_sweep

    # Must not raise: a failing sweep may never kill the daemon thread.
    _run_sweep(storage)
