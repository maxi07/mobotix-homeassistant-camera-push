import logging
import os
import time
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from uuid import uuid4

import requests
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename


LOGGER = logging.getLogger("mobotix-relay")


class HealthCheckAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


logging.getLogger("gunicorn.access").addFilter(HealthCheckAccessFilter())


def detect_image_type(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def parse_mime_parts(
    body: bytes, content_type: str
) -> list[tuple[str, str, str, bytes]]:
    message = BytesParser(policy=policy.default).parsebytes(
        b"MIME-Version: 1.0\r\nContent-Type: "
        + content_type.encode("latin-1")
        + b"\r\n\r\n"
        + body
    )
    parts = []
    for part in message.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        field_name = (
            part.get_param("name", header="content-disposition") or ""
        )
        parts.append(
            (
                field_name,
                part.get_filename() or "",
                part.get_content_type(),
                payload,
            )
        )
    return parts


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        HA_WEBHOOK_URL=os.getenv("HA_WEBHOOK_URL", ""),
        PUBLIC_BASE_URL=os.getenv("PUBLIC_BASE_URL", ""),
        IMAGE_DIR=os.getenv("IMAGE_DIR", "/data/images"),
        REQUEST_TIMEOUT=float(os.getenv("REQUEST_TIMEOUT", "10")),
        RETRY_COUNT=int(os.getenv("RETRY_COUNT", "3")),
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_CONTENT_LENGTH", "20971520")),
    )
    if config:
        app.config.update(config)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not app.config["HA_WEBHOOK_URL"]:
        raise RuntimeError("HA_WEBHOOK_URL must be configured")

    image_dir = Path(app.config["IMAGE_DIR"])
    image_dir.mkdir(parents=True, exist_ok=True)

    @app.post("/")
    def receive_event():
        if request.mimetype != "multipart/form-data":
            return jsonify(error="multipart/form-data is required"), 415

        raw_body = request.get_data(cache=True)
        uploads = list(request.files.items(multi=True))
        parts = [
            (
                field_name,
                uploaded.filename or "",
                uploaded.mimetype,
                uploaded.read(),
            )
            for field_name, uploaded in uploads
        ]
        if not parts:
            parts = parse_mime_parts(raw_body, request.content_type or "")

        fields = request.form.to_dict(flat=True)
        if not uploads:
            for field_name, filename, content_type, payload in parts:
                if filename or not content_type.startswith("text/"):
                    continue
                value = payload.decode("utf-8", errors="replace").strip()
                if field_name:
                    fields.setdefault(field_name, value)
                elif value:
                    fields.setdefault("message", value)

        image_field = None
        candidates = sorted(
            parts,
            key=lambda item: not item[2].startswith("image/"),
        )
        for field_name, filename, content_type, image in candidates:
            detected_type = detect_image_type(image)
            if content_type.startswith("image/"):
                image_content_type = content_type
                suffix = (
                    detected_type[1]
                    if detected_type
                    else Path(filename).suffix.lower()
                )
            elif detected_type is not None:
                image_content_type, suffix = detected_type
            else:
                continue
            image_field = (
                field_name or "image",
                filename or "mobotix.jpg",
                image,
                image_content_type,
                suffix,
            )
            break

        if image_field is None:
            LOGGER.warning(
                "No image part content_type=%s files=%s form_fields=%s "
                "body_length=%s user_agent=%s",
                request.content_type,
                [
                    (field, filename, content_type)
                    for field, filename, content_type, _ in parts
                ],
                list(fields.keys()),
                len(raw_body),
                request.user_agent.string,
            )
            return jsonify(error="multipart image is required"), 400

        field_name, filename, image, image_content_type, suffix = image_field
        if not image:
            return jsonify(error="image is empty"), 400

        original_name = secure_filename(filename)
        suffix = suffix or ".jpg"
        original_name = f"{Path(original_name).stem}{suffix}"
        stored_name = f"{uuid4().hex}{suffix}"
        (image_dir / stored_name).write_bytes(image)

        source_ip = request.remote_addr or "unknown"
        if source_ip.startswith("::ffff:"):
            source_ip = source_ip.removeprefix("::ffff:")

        fields.setdefault("message", "Mobotix event")
        fields["source_ip"] = source_ip
        fields["received_at"] = datetime.now(timezone.utc).isoformat()
        public_base_url = app.config["PUBLIC_BASE_URL"] or request.host_url
        fields["image_url"] = (
            f"{public_base_url.rstrip('/')}/images/{stored_name}"
        )
        files = {
            field_name: (
                original_name,
                image,
                image_content_type,
            )
        }

        for attempt in range(1, app.config["RETRY_COUNT"] + 1):
            try:
                response = requests.post(
                    app.config["HA_WEBHOOK_URL"],
                    data=fields,
                    files=files,
                    timeout=app.config["REQUEST_TIMEOUT"],
                )
                response.raise_for_status()
                LOGGER.info(
                    "Event delivered source_ip=%s image=%s attempt=%s",
                    source_ip,
                    stored_name,
                    attempt,
                )
                return jsonify(
                    status="delivered", image_url=fields["image_url"]
                )
            except requests.RequestException as error:
                LOGGER.warning(
                    "Delivery failed source_ip=%s attempt=%s error=%s",
                    source_ip,
                    attempt,
                    error,
                )
                if attempt < app.config["RETRY_COUNT"]:
                    time.sleep(attempt)

        return jsonify(error="Home Assistant delivery failed"), 502

    @app.get("/images/<path:filename>")
    def image(filename: str):
        return send_from_directory(image_dir, filename)

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    return app
