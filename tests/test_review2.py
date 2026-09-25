"""Tests fuer die zweite Copilot-Review-Runde an PR #3.

Jeder Test haelt genau einen der drei Befunde fest:
  1. compose.yaml muss jede dokumentierte Variable durchreichen
  2. der Sweeper muss JEDEN Schluessel erkennen, den das Relay erzeugen kann
  3. URL_TTL_SECONDS muss auf dem ECHTEN .env-Pfad validiert werden
"""
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from app import create_app
from storage import is_relay_object


REPO = Path(__file__).resolve().parent.parent


# --- Befund 1: compose.yaml reicht R2_SIGNING_REGION nicht durch -----------


def documented_variables() -> set:
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z0-9_]+)=", text, re.M))


def compose_variables() -> set:
    text = (REPO / "compose.yaml").read_text(encoding="utf-8")
    return set(re.findall(r"^\s+([A-Z0-9_]+):\s", text, re.M))


def test_signing_region_reaches_the_container():
    assert "R2_SIGNING_REGION" in compose_variables()


def test_every_documented_variable_is_passed_through():
    # RELAY_PORT maps the published port and is consumed by Compose itself,
    # not handed to the application.
    missing = documented_variables() - compose_variables() - {"RELAY_PORT"}

    assert not missing, f"not passed into the container: {sorted(missing)}"


# --- Befund 2: Muster war enger als die erzeugbaren Schluessel -------------


UUID = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "suffix",
    [
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".jpeg2000",       # laenger als fuenf Zeichen
        ".JPG",            # Grossschreibung
        ".jpg~1",          # Satzzeichen
        ".image-final",    # Bindestrich
        ".tar.gz",         # mehrteilig
    ],
)
def test_every_suffix_the_relay_can_produce_is_swept(suffix):
    """app.py faellt auf den Dateinamen des Clients zurueck, wenn die
    Signatur unbekannt ist. Solche Objekte muessen aufgeraeumt werden."""
    assert is_relay_object(UUID + suffix)


@pytest.mark.parametrize(
    "key",
    [
        "backup.tar.gz",
        "photos/wedding.jpg",
        "0123456789abcdef.jpg",                    # zu kurz
        "0123456789abcdef0123456789abcdeXY.jpg",   # kein Hex
        UUID,                                      # ohne Endung
        UUID + ".jpg/evil",                        # Pfadtrenner in der Endung
    ],
)
def test_foreign_keys_stay_untouched(key):
    assert not is_relay_object(key)


def test_fallback_suffix_from_app_is_recognised():
    """Gegenprobe gegen den echten Code-Pfad in app.py."""
    from pathlib import Path as P
    from uuid import uuid4

    # So baut app.py den Schluessel, wenn detect_image_type nichts erkennt.
    filename = "var_www_record_current.unusual-extension"
    suffix = P(filename).suffix.lower()
    stored_name = f"{uuid4().hex}{suffix}"

    assert is_relay_object(stored_name)


# --- Befund 3: TTL-Validierung griff nicht auf dem .env-Pfad ---------------


R2_ENV = {
    "HA_WEBHOOK_URL": "http://server3:8123/api/webhook/test",
    "STORAGE_BACKEND": "r2",
    "R2_BUCKET": "doorbell",
    "R2_ACCESS_KEY_ID": "key",
    "R2_SECRET_ACCESS_KEY": "secret",
    "R2_ENDPOINT_URL": "https://x.eu.r2.cloudflarestorage.com",
}


def env_app(tmp_path, ttl):
    """create_app() ohne config-Override - genau wie im Container."""
    environment = dict(R2_ENV)
    environment["IMAGE_DIR"] = str(tmp_path / "images")
    environment["URL_TTL_SECONDS"] = ttl
    with patch.dict(os.environ, environment, clear=False):
        return create_app({"TESTING": True})


@pytest.mark.parametrize("ttl", ["0", "-1", "-900"])
def test_non_positive_ttl_from_environment_is_rejected(tmp_path, ttl):
    with pytest.raises(RuntimeError, match="URL_TTL_SECONDS must be greater"):
        env_app(tmp_path, ttl)


@pytest.mark.parametrize("ttl", ["bald", "", "900s", "15 minutes"])
def test_non_numeric_ttl_from_environment_reports_clearly(tmp_path, ttl):
    """Vorher: roher ValueError beim Aufbau des Mappings."""
    with pytest.raises(RuntimeError, match="URL_TTL_SECONDS must be a whole"):
        env_app(tmp_path, ttl)


def test_valid_ttl_from_environment_starts(tmp_path):
    app = env_app(tmp_path, "900")

    assert app.config["STORAGE"].url_ttl_seconds == 900


def test_ttl_error_is_a_runtime_error_not_a_value_error(tmp_path):
    """Der Betreiber soll eine Klartextmeldung sehen, keinen int()-Fehler."""
    with pytest.raises(RuntimeError) as caught:
        env_app(tmp_path, "bald")

    assert not isinstance(caught.value, ValueError)
    assert "URL_TTL_SECONDS" in str(caught.value)
