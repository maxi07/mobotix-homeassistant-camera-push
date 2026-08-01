import io
import logging
from unittest.mock import Mock, patch

import requests
from gunicorn.http.body import EOFReader, LengthReader

from app import HealthCheckAccessFilter, create_app
from gunicorn_config import set_compatible_body_reader


JPEG_BYTES = b"\xff\xd8camera-image\xff\xd9"


class FakeUnreader:
    def __init__(self, data):
        self.data = data

    def read(self):
        data, self.data = self.data, b""
        return data

    def unread(self, data):
        self.data = data + self.data


class FakeGunicornRequest:
    version = (1, 0)
    method = "POST"

    def __init__(self, data, user_agent="mxmsg/2.2", connection="close"):
        self.headers = [("USER-AGENT", user_agent)]
        if connection is not None:
            self.headers.append(("CONNECTION", connection))
        self.unreader = FakeUnreader(data)


def test_gunicorn_reads_mobotix_http_10_body_until_eof():
    request = FakeGunicornRequest(JPEG_BYTES)

    set_compatible_body_reader(request)

    assert isinstance(request.body.reader, EOFReader)
    assert request.body.read() == JPEG_BYTES


def test_gunicorn_reads_mobotix_body_without_connection_header():
    request = FakeGunicornRequest(JPEG_BYTES, connection=None)

    set_compatible_body_reader(request)

    assert isinstance(request.body.reader, EOFReader)
    assert request.body.read() == JPEG_BYTES


def test_gunicorn_does_not_read_mobotix_keep_alive_body_to_eof():
    request = FakeGunicornRequest(JPEG_BYTES, connection="keep-alive")

    set_compatible_body_reader(request)

    assert isinstance(request.body.reader, LengthReader)
    assert request.body.read() == b""


def test_gunicorn_keeps_zero_length_for_other_unframed_requests():
    request = FakeGunicornRequest(JPEG_BYTES, user_agent="other-client")

    set_compatible_body_reader(request)

    assert isinstance(request.body.reader, LengthReader)
    assert request.body.read() == b""


def test_healthcheck_access_log_is_filtered():
    access_filter = HealthCheckAccessFilter()
    health_record = logging.LogRecord(
        "gunicorn.access",
        logging.INFO,
        "",
        0,
        '"GET /healthz HTTP/1.1" 200',
        (),
        None,
    )
    event_record = logging.LogRecord(
        "gunicorn.access",
        logging.INFO,
        "",
        0,
        '"POST / HTTP/1.0" 200',
        (),
        None,
    )

    assert access_filter.filter(health_record) is False
    assert access_filter.filter(event_record) is True


def make_app(tmp_path, **config):
    settings = {
        "HA_WEBHOOK_URL": "http://server3:8123/api/webhook/test",
        "PUBLIC_BASE_URL": "http://relay.test:18425",
        "IMAGE_DIR": str(tmp_path / "images"),
        "RETRY_COUNT": 1,
        "TESTING": True,
    }
    settings.update(config)
    return create_app(settings)


def test_multipart_image_is_forwarded_and_available(tmp_path):
    application = make_app(tmp_path)
    home_assistant_response = Mock()
    home_assistant_response.raise_for_status.return_value = None

    with patch(
        "app.requests.post", return_value=home_assistant_response
    ) as post:
        response = application.test_client().post(
            "/",
            data={
                "message": "Doorbell",
                "image": (
                    io.BytesIO(JPEG_BYTES),
                    "doorbell.jpg",
                    "image/jpeg",
                ),
            },
            environ_base={"REMOTE_ADDR": "::ffff:192.168.111.25"},
        )

    assert response.status_code == 200
    forwarded = post.call_args
    assert forwarded.args == ("http://server3:8123/api/webhook/test",)
    assert forwarded.kwargs["data"]["message"] == "Doorbell"
    assert forwarded.kwargs["data"]["source_ip"] == "192.168.111.25"
    image_url = forwarded.kwargs["data"]["image_url"]
    assert image_url.startswith("http://relay.test:18425/images/")
    assert forwarded.kwargs["files"] == {
        "image": ("doorbell.jpg", JPEG_BYTES, "image/jpeg")
    }

    image_path = image_url.removeprefix("http://relay.test:18425")
    image_response = application.test_client().get(image_path)
    assert image_response.status_code == 200
    assert image_response.data == JPEG_BYTES
    assert image_response.content_type == "image/jpeg"


def test_image_url_uses_request_host_without_override(tmp_path):
    application = make_app(tmp_path, PUBLIC_BASE_URL="")
    home_assistant_response = Mock()
    home_assistant_response.raise_for_status.return_value = None

    with patch(
        "app.requests.post", return_value=home_assistant_response
    ) as post:
        response = application.test_client().post(
            "/",
            data={
                "image": (
                    io.BytesIO(JPEG_BYTES),
                    "doorbell.jpg",
                    "image/jpeg",
                ),
            },
            headers={"Host": "relay.local:18425"},
        )

    assert response.status_code == 200
    image_url = post.call_args.kwargs["data"]["image_url"]
    assert image_url.startswith("http://relay.local:18425/images/")


def test_mobotix_octet_stream_image_is_forwarded_as_jpeg(tmp_path):
    application = make_app(tmp_path)
    home_assistant_response = Mock()
    home_assistant_response.raise_for_status.return_value = None

    with patch(
        "app.requests.post", return_value=home_assistant_response
    ) as post:
        response = application.test_client().post(
            "/",
            data={
                "image": (
                    io.BytesIO(JPEG_BYTES),
                    "mx-image.bin",
                    "application/octet-stream",
                ),
            },
            headers={"User-Agent": "mxmsg/2.2"},
        )

    assert response.status_code == 200
    assert post.call_args.kwargs["files"] == {
        "image": ("mx-image.jpg", JPEG_BYTES, "image/jpeg")
    }


def test_mobotix_image_is_found_after_metadata_file(tmp_path):
    application = make_app(tmp_path)
    home_assistant_response = Mock()
    home_assistant_response.raise_for_status.return_value = None

    with patch(
        "app.requests.post", return_value=home_assistant_response
    ) as post:
        response = application.test_client().post(
            "/",
            data={
                "metadata": (
                    io.BytesIO(b"event=doorbell"),
                    "event.txt",
                    "text/plain",
                ),
                "image": (
                    io.BytesIO(JPEG_BYTES),
                    "mx-image.bin",
                    "application/octet-stream",
                ),
            },
        )

    assert response.status_code == 200
    assert post.call_args.kwargs["files"] == {
        "image": ("mx-image.jpg", JPEG_BYTES, "image/jpeg")
    }


def test_mobotix_mime_parts_without_form_disposition_are_forwarded(tmp_path):
    application = make_app(tmp_path)
    home_assistant_response = Mock()
    home_assistant_response.raise_for_status.return_value = None
    boundary = "mxmsg_qswteervteuliaodpj9a7b5i3_20050405"
    body = (
        f"--{boundary}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Doorbell\r\n"
        f"--{boundary}\r\n"
        "Content-Type: image/jpeg\r\n"
        "Content-Transfer-Encoding: binary\r\n"
        "\r\n"
    ).encode() + JPEG_BYTES + f"\r\n--{boundary}--\r\n".encode()

    with patch(
        "app.requests.post", return_value=home_assistant_response
    ) as post:
        response = application.test_client().post(
            "/",
            data=body,
            content_type=f'multipart/form-data; boundary="{boundary}"',
            headers={"User-Agent": "mxmsg/2.2"},
        )

    assert response.status_code == 200
    assert post.call_args.kwargs["data"]["message"] == "Doorbell"
    assert post.call_args.kwargs["files"] == {
        "image": ("mobotix.jpg", JPEG_BYTES, "image/jpeg")
    }


def test_request_without_image_is_rejected(tmp_path):
    application = make_app(tmp_path)

    response = application.test_client().post(
        "/", data={"message": "Doorbell"}
    )

    assert response.status_code == 415


def test_home_assistant_failure_is_retried_and_logged(
    tmp_path, caplog
):
    application = make_app(tmp_path, RETRY_COUNT=2)
    error = requests.ConnectionError("offline")

    with patch("app.requests.post", side_effect=error) as post, patch(
        "app.time.sleep"
    ):
        response = application.test_client().post(
            "/",
            data={
                "image": (
                    io.BytesIO(JPEG_BYTES),
                    "doorbell.jpg",
                    "image/jpeg",
                )
            },
        )

    assert response.status_code == 502
    assert post.call_count == 2
    assert "Delivery failed" in caplog.text


def test_healthcheck(tmp_path):
    application = make_app(tmp_path)

    response = application.test_client().get("/healthz")

    assert response.status_code == 200
    assert response.json == {"status": "ok"}
