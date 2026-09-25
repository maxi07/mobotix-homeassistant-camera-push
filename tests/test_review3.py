"""Tests fuer die dritte Copilot-Review-Runde an PR #3.

Befund: build_r2_storage() gab den Client zurueck, ohne check() aufzurufen.
Ein falscher Endpunkt, Bucket oder Schluessel liess den Dienst also starten
und fiel erst beim ersten Klingeln auf. check() war toter Code.
"""
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from app import create_app, env_flag
from storage import R2Storage, StorageError


REPO = Path(__file__).resolve().parent.parent

BASIS_ENV = {
    "HA_WEBHOOK_URL": "http://server3:8123/api/webhook/test",
    "STORAGE_BACKEND": "r2",
    "R2_BUCKET": "doorbell",
    "R2_ACCESS_KEY_ID": "key",
    "R2_SECRET_ACCESS_KEY": "secret",
    "R2_ENDPOINT_URL": "https://x.eu.r2.cloudflarestorage.com",
}


class FakeClientError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.response = {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class ReachableClient:
    def __init__(self):
        self.list_calls = 0

    def list_objects_v2(self, **kwargs):
        self.list_calls += 1
        return {"Contents": [], "IsTruncated": False}


class RejectingClient:
    def __init__(self, status, code):
        self.status = status
        self.code = code

    def list_objects_v2(self, **kwargs):
        raise FakeClientError(self.status, self.code)


def start(tmp_path, client, **extra_env):
    """create_app() ueber den echten Umgebungspfad, mit gestelltem Client."""
    environment = dict(BASIS_ENV)
    environment["IMAGE_DIR"] = str(tmp_path / "images")
    environment.update(extra_env)

    def fake_build(endpoint_url, access_key_id, access_key_secret, region):
        return client

    with patch.dict(os.environ, environment, clear=False):
        with patch.object(R2Storage, "_build_client", staticmethod(fake_build)):
            return create_app({"TESTING": True})


# --- Der Kern des Befunds -------------------------------------------------


def test_startup_reaches_the_bucket(tmp_path):
    client = ReachableClient()

    start(tmp_path, client)

    assert client.list_calls >= 1, "check() wurde beim Start nicht aufgerufen"


@pytest.mark.parametrize(
    "status,code,erwartet",
    [
        (404, "NoSuchBucket", "R2_BUCKET"),
        (403, "InvalidAccessKeyId", "R2_ACCESS_KEY_ID"),
        (400, "SignatureDoesNotMatch", "R2_SECRET_ACCESS_KEY"),
        (400, "InvalidArgument", "R2_ENDPOINT_URL"),
        (403, "AccessDenied", "Object Read & Write"),
        (301, "PermanentRedirect", ".eu."),
    ],
)
def test_bad_configuration_fails_at_startup(tmp_path, status, code, erwartet):
    """Vorher: Start ok, Fehler erst beim ersten Klingeln."""
    with pytest.raises(StorageError) as caught:
        start(tmp_path, RejectingClient(status, code))

    meldung = str(caught.value)
    assert f"HTTP {status}" in meldung
    assert code in meldung
    assert erwartet in meldung


def test_unreachable_endpoint_fails_at_startup(tmp_path):
    class OfflineClient:
        def list_objects_v2(self, **kwargs):
            raise ConnectionError("name resolution failed")

    with pytest.raises(StorageError, match="name resolution failed"):
        start(tmp_path, OfflineClient())


# --- Der Schalter ---------------------------------------------------------


def test_verification_can_be_disabled(tmp_path):
    """Fuer Umgebungen, die starten muessen, auch wenn R2 gerade weg ist."""
    client = RejectingClient(404, "NoSuchBucket")

    app = start(tmp_path, client, R2_VERIFY_ON_START="false")

    assert app.config["STORAGE"].name == "r2"


def test_verification_is_on_by_default(tmp_path):
    environment = dict(BASIS_ENV)
    environment["IMAGE_DIR"] = str(tmp_path / "images")
    environment.pop("R2_VERIFY_ON_START", None)

    with patch.dict(os.environ, environment, clear=False):
        with patch.object(
            R2Storage,
            "_build_client",
            staticmethod(lambda *a: RejectingClient(404, "NoSuchBucket")),
        ):
            with pytest.raises(StorageError):
                create_app({"TESTING": True})


@pytest.mark.parametrize("wert", ["false", "False", "0", "no", "off", ""])
def test_flag_recognises_falsy_values(wert, monkeypatch):
    monkeypatch.setenv("SOME_FLAG", wert)
    assert env_flag("SOME_FLAG") is False


@pytest.mark.parametrize("wert", ["true", "True", "1", "yes", "on"])
def test_flag_recognises_truthy_values(wert, monkeypatch):
    monkeypatch.setenv("SOME_FLAG", wert)
    assert env_flag("SOME_FLAG") is True


def test_flag_defaults_to_true(monkeypatch):
    monkeypatch.delenv("SOME_FLAG", raising=False)
    assert env_flag("SOME_FLAG") is True


# --- Das local-Backend darf davon unberuehrt bleiben ----------------------


def test_local_backend_needs_no_r2_settings(tmp_path):
    environment = {
        "HA_WEBHOOK_URL": "http://server3:8123/api/webhook/test",
        "STORAGE_BACKEND": "local",
        "IMAGE_DIR": str(tmp_path / "images"),
    }
    with patch.dict(os.environ, environment, clear=False):
        app = create_app({"TESTING": True})

    assert app.config["STORAGE"].name == "local"


# --- Die neue Variable muss ueberall ankommen -----------------------------


def test_verify_flag_reaches_the_container():
    compose = (REPO / "compose.yaml").read_text(encoding="utf-8")
    assert "R2_VERIFY_ON_START" in compose


def test_verify_flag_is_documented():
    env_example = (REPO / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^R2_VERIFY_ON_START=", env_example, re.M)
