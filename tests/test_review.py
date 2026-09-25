"""Tests fuer die Punkte aus dem Copilot-Review an PR #3.

Jeder Test haelt genau eine der beanstandeten Eigenschaften fest, damit sie
nicht still wieder verloren geht.
"""
import time

import pytest

from app import create_app, positive_int
from storage import (
    DELETE_BATCH_SIZE,
    R2Storage,
    StorageError,
    is_relay_object,
)


JPEG_BYTES = b"\xff\xd8camera-image\xff\xd9"
RELAY_KEY = "0123456789abcdef0123456789abcdef.jpg"


# --- Review: sweep darf fremde Objekte nicht loeschen ----------------------


def test_relay_keys_are_recognised():
    assert is_relay_object(RELAY_KEY)
    assert is_relay_object("doorbell/" + RELAY_KEY)
    assert is_relay_object("abcdef01234567890abcdef012345678.png")


def test_foreign_keys_are_not_recognised():
    fremde = [
        "backup.tar.gz",
        "invoices/2026-09.pdf",
        "holiday.jpg",
        "0123456789abcdef.jpg",  # zu kurz
        "0123456789abcdef0123456789abcdefXY.jpg",  # kein Hex
        "0123456789abcdef0123456789abcdef",  # ohne Endung
        "0123456789abcdef0123456789abcde.jpg",  # 31 statt 32 Zeichen
    ]
    for key in fremde:
        assert not is_relay_object(key), key


class OwnedObjects:
    """Mischklasse: alle Objekte tragen den Besitzmarker des Relays.

    Die Besitzpruefung im Sweeper fragt pro Kandidat head_object ab. Ohne
    diese Antwort wuerde jeder Testfall als "nicht unseres" gelten.
    """

    def head_object(self, Bucket, Key):
        return {"Metadata": {"created-by": "mobotix-relay"}}

class SpyClient(OwnedObjects):
    """Erfasst, was geloescht wuerde."""

    def __init__(self, contents):
        self.contents = contents
        self.deleted = []
        self.list_calls = []

    def list_objects_v2(self, **kwargs):
        self.list_calls.append(kwargs)
        return {"Contents": self.contents, "IsTruncated": False}

    def delete_objects(self, Bucket, Delete):
        keys = [item["Key"] for item in Delete["Objects"]]
        self.deleted.extend(keys)
        return {"Deleted": [{"Key": key} for key in keys]}


def eintrag(key, alter_sekunden):
    from datetime import datetime, timedelta, timezone

    return {
        "Key": key,
        "LastModified": datetime.now(timezone.utc)
        - timedelta(seconds=alter_sekunden),
    }


def make_storage(client, **kwargs):
    settings = {
        "bucket": "shared",
        "endpoint_url": "https://x.eu.r2.cloudflarestorage.com",
        "access_key_id": "key",
        "access_key_secret": "secret",
        "url_ttl_seconds": 900,
        "client": client,
    }
    settings.update(kwargs)
    return R2Storage(**settings)


def test_sweep_leaves_foreign_objects_alone_in_a_shared_bucket():
    client = SpyClient(
        [
            eintrag(RELAY_KEY, 5000),               # unseres, abgelaufen
            eintrag("important-backup.zip", 99999),  # fremd, uralt
            eintrag("photos/wedding.jpg", 99999),    # fremd, uralt
        ]
    )
    storage = make_storage(client)

    removed = storage.sweep()

    assert removed == 1
    assert client.deleted == [RELAY_KEY]


def test_sweep_without_prefix_does_not_wipe_a_shared_bucket():
    client = SpyClient([eintrag("customer-data.csv", 99999)])
    storage = make_storage(client, key_prefix="")

    assert storage.sweep() == 0
    assert client.deleted == []


# --- Review: DeleteObjects meldet Teilfehler mit HTTP 200 ------------------


class PartialFailureClient(OwnedObjects):
    def __init__(self):
        self.attempted = []

    def list_objects_v2(self, **kwargs):
        return {
            "Contents": [
                eintrag("0123456789abcdef0123456789abcde1.jpg", 5000),
                eintrag("0123456789abcdef0123456789abcde2.jpg", 5000),
            ],
            "IsTruncated": False,
        }

    def delete_objects(self, Bucket, Delete):
        keys = [item["Key"] for item in Delete["Objects"]]
        self.attempted.extend(keys)
        return {
            "Deleted": [{"Key": keys[0]}],
            "Errors": [
                {
                    "Key": keys[1],
                    "Code": "AccessDenied",
                    "Message": "no permission",
                }
            ],
        }


def test_partial_delete_failure_is_not_counted_as_removed(caplog):
    storage = make_storage(PartialFailureClient())

    with caplog.at_level("WARNING"):
        removed = storage.sweep()

    # Zwei Kandidaten, aber nur einer wurde wirklich geloescht.
    assert removed == 1
    assert "Could not delete" in caplog.text
    assert "AccessDenied" in caplog.text


# --- Review: Listing muss paginieren --------------------------------------


class PagingClient(OwnedObjects):
    """Liefert zwei Seiten und erwartet, dass beide gelesen werden."""

    def __init__(self):
        self.pages = 0
        self.deleted = []

    def list_objects_v2(self, **kwargs):
        self.pages += 1
        if "ContinuationToken" not in kwargs:
            return {
                "Contents": [eintrag("0123456789abcdef0123456789abcde1.jpg", 5000)],
                "IsTruncated": True,
                "NextContinuationToken": "seite2",
            }
        return {
            "Contents": [eintrag("0123456789abcdef0123456789abcde2.jpg", 5000)],
            "IsTruncated": False,
        }

    def delete_objects(self, Bucket, Delete):
        keys = [item["Key"] for item in Delete["Objects"]]
        self.deleted.extend(keys)
        return {"Deleted": [{"Key": key} for key in keys]}


def test_sweep_follows_pagination_beyond_the_first_page():
    client = PagingClient()
    storage = make_storage(client)

    removed = storage.sweep()

    assert client.pages == 2
    assert removed == 2
    assert len(client.deleted) == 2


class BatchClient(OwnedObjects):
    """Prueft, dass Loeschungen in 1000er-Bloecken gehen."""

    def __init__(self, anzahl):
        self.contents = [
            eintrag(f"{i:032x}.jpg", 5000) for i in range(anzahl)
        ]
        self.batch_sizes = []

    def list_objects_v2(self, **kwargs):
        return {"Contents": self.contents, "IsTruncated": False}

    def delete_objects(self, Bucket, Delete):
        keys = [item["Key"] for item in Delete["Objects"]]
        self.batch_sizes.append(len(keys))
        return {"Deleted": [{"Key": key} for key in keys]}


def test_deletes_are_split_into_batches_of_1000():
    client = BatchClient(2300)
    storage = make_storage(client)

    removed = storage.sweep()

    assert removed == 2300
    assert client.batch_sizes == [DELETE_BATCH_SIZE, DELETE_BATCH_SIZE, 300]
    assert max(client.batch_sizes) <= DELETE_BATCH_SIZE


# --- Review: URL_TTL_SECONDS muss validiert werden -------------------------


def basis(tmp_path, **extra):
    settings = {
        "HA_WEBHOOK_URL": "http://server3:8123/api/webhook/test",
        "IMAGE_DIR": str(tmp_path / "images"),
        "STORAGE_BACKEND": "r2",
        "R2_BUCKET": "doorbell",
        "R2_ACCESS_KEY_ID": "key",
        "R2_SECRET_ACCESS_KEY": "secret",
        "R2_ENDPOINT_URL": "https://x.eu.r2.cloudflarestorage.com",
        "R2_ACCOUNT_ID": "",
        "R2_JURISDICTION": "",
        "R2_KEY_PREFIX": "",
        "R2_SIGNING_REGION": "auto",
        "IMAGE_RETENTION_MINUTES": 60,
        "URL_TTL_SECONDS": 900,
        "TESTING": True,
    }
    settings.update(extra)
    return settings


@pytest.mark.parametrize("ttl", [0, -1, -900])
def test_non_positive_ttl_is_rejected_at_startup(tmp_path, ttl):
    with pytest.raises(RuntimeError, match="URL_TTL_SECONDS must be greater"):
        create_app(basis(tmp_path, URL_TTL_SECONDS=ttl))


def test_non_numeric_ttl_is_rejected_at_startup(tmp_path):
    with pytest.raises(RuntimeError, match="whole number"):
        create_app(basis(tmp_path, URL_TTL_SECONDS="bald"))


def test_positive_int_accepts_a_valid_value():
    class FakeApp:
        config = {"URL_TTL_SECONDS": "900"}

    assert positive_int(FakeApp(), "URL_TTL_SECONDS") == 900


# --- Review: Signing-Region muss konfigurierbar sein -----------------------


def test_signing_region_defaults_to_auto(tmp_path, monkeypatch):
    erfasst = {}

    def fake_client(endpoint_url, access_key_id, access_key_secret, region):
        erfasst["region"] = region
        return SpyClient([])

    monkeypatch.setattr(R2Storage, "_build_client", staticmethod(fake_client))
    create_app(basis(tmp_path))

    assert erfasst["region"] == "auto"


def test_signing_region_can_be_overridden_for_aws(tmp_path, monkeypatch):
    erfasst = {}

    def fake_client(endpoint_url, access_key_id, access_key_secret, region):
        erfasst["region"] = region
        return SpyClient([])

    monkeypatch.setattr(R2Storage, "_build_client", staticmethod(fake_client))
    create_app(basis(tmp_path, R2_SIGNING_REGION="eu-central-1"))

    assert erfasst["region"] == "eu-central-1"
