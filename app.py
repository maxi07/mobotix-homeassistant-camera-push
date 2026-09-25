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

from storage import LocalStorage, R2Storage, start_sweeper


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


def build_storage(app: Flask):
    """Create the storage backend selected by STORAGE_BACKEND."""
    backend = (app.config["STORAGE_BACKEND"] or "local").strip().lower()
    if backend == "local":
        return LocalStorage(
            app.config["IMAGE_DIR"],
            retention_seconds=app.config["IMAGE_RETENTION_MINUTES"] * 60,
        )
    if backend == "r2":
        return build_r2_storage(app)
    raise RuntimeError(
        f"STORAGE_BACKEND must be 'local' or 'r2', got {backend!r}"
    )


def build_r2_storage(app: Flask) -> R2Storage:
    """Create the R2 backend, failing loudly on incomplete configuration."""
    missing = [
        name
        for name in (
            "R2_BUCKET",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
        )
        if not app.config.get(name)
    ]
    endpoint = app.config["R2_ENDPOINT_URL"] or derive_r2_endpoint(
        app.config["R2_ACCOUNT_ID"], app.config["R2_JURISDICTION"]
    )
    if not endpoint:
        missing.append("R2_ACCOUNT_ID (or R2_ENDPOINT_URL)")
    if missing:
        raise RuntimeError(
            "STORAGE_BACKEND=r2 requires: " + ", ".join(missing)
        )
    return R2Storage(
        bucket=app.config["R2_BUCKET"],
        endpoint_url=endpoint,
        access_key_id=app.config["R2_ACCESS_KEY_ID"],
        secret_access_key=app.config["R2_SECRET_ACCESS_KEY"],
        url_ttl_seconds=app.config["URL_TTL_SECONDS"],
        key_prefix=app.config["R2_KEY_PREFIX"],
    )


def derive_r2_endpoint(account_id: str, jurisdiction: str) -> str:
    """Build the R2 S3 endpoint. EU buckets use a separate hostname."""
    if not account_id:
        return ""
    region = (jurisdiction or "").strip().lower()
    if region in ("eu", "fedramp"):
        return f"https://{account_id}.{region}.r2.cloudflarestorage.com"
    return f"https://{account_id}.r2.cloudflarestorage.com"


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        HA_WEBHOOK_URL=os.getenv("HA_WEBHOOK_URL", ""),
        PUBLIC_BASE_URL=os.getenv("PUBLIC_BASE_URL", ""),
        IMAGE_DIR=os.getenv("IMAGE_DIR", "/data/images"),
        REQUEST_TIMEOUT=float(os.getenv("REQUEST_TIMEOUT", "10")),
        RETRY_COUNT=int(os.getenv("RETRY_COUNT", "3")),
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_CONTENT_LENGTH", "20971520")),
        STORAGE_BACKEND=os.getenv("STORAGE_BACKEND", "local"),
        IMAGE_RETENTION_MINUTES=float(
            os.getenv("IMAGE_RETENTION_MINUTES", "60")
        ),
        URL_TTL_SECONDS=int(os.getenv("URL_TTL_SECONDS", "900")),
        R2_ACCOUNT_ID=os.getenv("R2_ACCOUNT_ID", ""),
        R2_BUCKET=os.getenv("R2_BUCKET", ""),
        R2_ACCESS_KEY_ID=os.getenv("R2_ACCESS_KEY_ID", ""),
        R2_SECRET_ACCESS_KEY=os.getenv("R2_SECRET_ACCESS_KEY", ""),
        R2_ENDPOINT_URL=os.getenv("R2_ENDPOINT_URL", ""),
        R2_JURISDICTION=os.getenv("R2_JURISDICTION", ""),
        R2_KEY_PREFIX=os.getenv("R2_KEY_PREFIX", ""),
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

    storage = app.config.get("STORAGE") or build_storage(app)
    app.config["STORAGE"] = storage

    if storage.name == "r2":
        retention_seconds = app.config["URL_TTL_SECONDS"]
    else:
        retention_seconds = app.config["IMAGE_RETENTION_MINUTES"] * 60
    if not app.config.get("TESTING"):
        start_sweeper(storage, retention_seconds)

    LOGGER.info(
        "Storage backend=%s retention=%ss",
        storage.name,
        int(retention_seconds),
    )

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

        try:
            storage.store(stored_name, image, image_content_type)
        except Exception as error:  # noqa: BLE001 - surface any backend fault
            LOGGER.error(
                "Storing image failed backend=%s error=%s",
                storage.name,
                error,
            )
            return jsonify(error="storing the image failed"), 502

        source_ip = request.remote_addr or "unknown"
        if source_ip.startswith("::ffff:"):
            source_ip = source_ip.removeprefix("::ffff:")

        fields.setdefault("message", "Mobotix event")
        fields["source_ip"] = source_ip
        fields["received_at"] = datetime.now(timezone.utc).isoformat()
        public_base_url = app.config["PUBLIC_BASE_URL"] or request.host_url
        fields["image_url"] = storage.url_for(stored_name, public_base_url)
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
