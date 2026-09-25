"""Tests fuer die vierte Copilot-Review-Runde an PR #3.

Befund: Ein UUID-foermiger Name ist KEIN Eigentumsnachweis. In einem
geteilten Bucket konnte ein fremdes Objekt namens
`0123456789abcdef0123456789abcdef.jpg` zufaellig passen und wurde dann
dauerhaft geloescht. Die README-Zusage "shared bucket is safe" war damit
nicht gedeckt.

Jetzt gilt Besitz nur bei nachgewiesenem Marker aus den Objekt-Metadaten.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app import create_app
from storage import (
    DEFAULT_KEY_PREFIX,
    OWNER_METADATA_KEY,
    OWNER_METADATA_VALUE,
    R2Storage,
)


REPO = Path(__file__).resolve().parent.parent
UUID = "0123456789abcdef0123456789abcdef"


def eintrag(key, alter_sekunden):
    return {
        "Key": key,
        "LastModified": datetime.now(timezone.utc)
        - timedelta(seconds=alter_sekunden),
    }


class Bucket:
    """S3-Attrappe, bei der jedes Objekt eigene Metadaten tragen kann."""

    def __init__(self, objekte):
        # objekte: {key: metadata-dict oder None}
        self.objekte = objekte
        self.deleted = []
        self.head_calls = []

    def list_objects_v2(self, **kwargs):
        prefix = kwargs.get("Prefix")
        contents = [
            eintrag(key, 5000)
            for key in self.objekte
            if not prefix or key.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    def head_object(self, Bucket, Key):
        self.head_calls.append(Key)
        metadata = self.objekte.get(Key)
        if metadata is None:
            return {"Metadata": {}}
        return {"Metadata": metadata}

    def delete_objects(self, Bucket, Delete):
        keys = [item["Key"] for item in Delete["Objects"]]
        self.deleted.extend(keys)
        return {"Deleted": [{"Key": key} for key in keys]}


MARKER = {OWNER_METADATA_KEY: OWNER_METADATA_VALUE}


def storage_fuer(client, **kwargs):
    settings = {
        "bucket": "shared",
        "endpoint_url": "https://x.eu.r2.cloudflarestorage.com",
        "access_key_id": "key",
        "access_key_secret": "secret",
        "url_ttl_seconds": 900,
        "key_prefix": "",
        "client": client,
    }
    settings.update(kwargs)
    return R2Storage(**settings)


# --- Der Kern des Befunds -------------------------------------------------


def test_foreign_object_with_uuid_shaped_name_survives():
    """Genau der Fall aus dem Review: fremdes Objekt, passender Name."""
    client = Bucket({f"{UUID}.jpg": None})  # kein Marker = nicht unseres
    storage = storage_fuer(client)

    removed = storage.sweep()

    assert removed == 0
    assert client.deleted == []


def test_our_own_object_is_still_deleted():
    client = Bucket({f"{UUID}.jpg": dict(MARKER)})
    storage = storage_fuer(client)

    removed = storage.sweep()

    assert removed == 1
    assert client.deleted == [f"{UUID}.jpg"]


def test_mixed_bucket_deletes_only_marked_objects():
    fremd = "fedcba9876543210fedcba9876543210.jpg"
    client = Bucket(
        {
            f"{UUID}.jpg": dict(MARKER),  # unseres
            fremd: None,                   # fremd, gleiche Namensform
            "abcdef00112233445566778899aabbcc.png": {"created-by": "andere-app"},
        }
    )
    storage = storage_fuer(client)

    removed = storage.sweep()

    assert removed == 1
    assert client.deleted == [f"{UUID}.jpg"]
    assert fremd not in client.deleted


def test_marker_of_another_application_is_not_ours():
    client = Bucket({f"{UUID}.jpg": {"created-by": "some-other-tool"}})
    storage = storage_fuer(client)

    assert storage.sweep() == 0
    assert client.deleted == []


# --- Im Zweifel niemals loeschen ------------------------------------------


def test_failed_metadata_lookup_keeps_the_object(caplog):
    class BrokenHead(Bucket):
        def head_object(self, Bucket, Key):
            raise ConnectionError("timeout")

    client = BrokenHead({f"{UUID}.jpg": dict(MARKER)})
    storage = storage_fuer(client)

    with caplog.at_level("WARNING"):
        removed = storage.sweep()

    assert removed == 0
    assert client.deleted == []
    assert "leaving it alone" in caplog.text


def test_objects_predating_the_marker_are_kept():
    """Bestandsobjekte aus einer aelteren Version tragen keinen Marker."""
    client = Bucket({f"{UUID}.jpg": {}})
    storage = storage_fuer(client)

    assert storage.sweep() == 0


def test_skipped_objects_are_reported(caplog):
    client = Bucket({f"{UUID}.jpg": None})
    storage = storage_fuer(client)

    with caplog.at_level("INFO"):
        storage.sweep()

    assert "no created-by=mobotix-relay marker" in caplog.text


# --- Der Marker wird beim Upload gesetzt ----------------------------------


class RecordingBucket(Bucket):
    def __init__(self):
        super().__init__({})
        self.put_metadata = None

    def put_object(self, Bucket, Key, Body, ContentType, Metadata=None):
        self.put_metadata = Metadata


def test_upload_writes_the_ownership_marker():
    client = RecordingBucket()
    storage = storage_fuer(client)

    storage.store("abc.jpg", b"\xff\xd8x\xff\xd9", "image/jpeg")

    assert client.put_metadata == {OWNER_METADATA_KEY: OWNER_METADATA_VALUE}


def test_metadata_key_underscore_variant_is_accepted():
    """Manche S3-Dienste liefern Metadatenschluessel mit Unterstrich."""
    client = Bucket({f"{UUID}.jpg": {"created_by": OWNER_METADATA_VALUE}})
    storage = storage_fuer(client)

    assert storage.sweep() == 1


# --- Zweite Verteidigungslinie: eigener Namensraum ------------------------


def test_default_prefix_namespaces_relay_objects(tmp_path):
    environment = {
        "HA_WEBHOOK_URL": "http://server3:8123/api/webhook/test",
        "STORAGE_BACKEND": "r2",
        "R2_BUCKET": "doorbell",
        "R2_ACCESS_KEY_ID": "key",
        "R2_SECRET_ACCESS_KEY": "secret",
        "R2_ENDPOINT_URL": "https://x.eu.r2.cloudflarestorage.com",
        "R2_VERIFY_ON_START": "false",
        "IMAGE_DIR": str(tmp_path / "images"),
    }
    environment.pop("R2_KEY_PREFIX", None)

    with patch.dict(os.environ, environment, clear=False):
        with patch.object(
            R2Storage, "_build_client", staticmethod(lambda *a: Bucket({}))
        ):
            app = create_app({"TESTING": True})

    assert app.config["R2_KEY_PREFIX"] == DEFAULT_KEY_PREFIX
    assert app.config["STORAGE"].key_prefix == DEFAULT_KEY_PREFIX


def test_prefix_limits_what_is_listed_at_all():
    client = Bucket(
        {
            f"{DEFAULT_KEY_PREFIX}/{UUID}.jpg": dict(MARKER),
            "customer-data.csv": None,
        }
    )
    storage = storage_fuer(client, key_prefix=DEFAULT_KEY_PREFIX)

    storage.sweep()

    # Fremde Objekte werden nicht einmal auf Besitz geprueft.
    assert "customer-data.csv" not in client.head_calls
    assert client.deleted == [f"{DEFAULT_KEY_PREFIX}/{UUID}.jpg"]


# --- Die README darf nichts versprechen, was nicht gilt -------------------


def test_readme_does_not_claim_name_shape_is_proof():
    text = (REPO / "README.md").read_text(encoding="utf-8")

    assert "32-character hex UUID plus a file extension" not in text
