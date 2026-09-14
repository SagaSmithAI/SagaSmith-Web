import hashlib
import io
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from sagasmith_service.storage import PrivateStorageError, S3PrivateStorage


def error(code):
    return ClientError({"Error": {"Code": code}}, "test")


def storage(tmp_path, monkeypatch, client, **kwargs):
    monkeypatch.setattr("sagasmith_service.storage.boto3.client", lambda *a, **k: client)
    return S3PrivateStorage(
        endpoint="https://objects.example.test", bucket="private", access_key="test",
        secret_key="test", exchange_root=str(tmp_path), **kwargs,
    )


@pytest.mark.parametrize("code", ["403", "AccessDenied", "500", "404"])
def test_startup_never_creates_bucket_by_default(tmp_path, monkeypatch, code):
    client = Mock()
    client.head_bucket.side_effect = error(code)
    with pytest.raises(PrivateStorageError):
        storage(tmp_path, monkeypatch, client)
    client.create_bucket.assert_not_called()


def test_development_bucket_creation_only_on_missing(tmp_path, monkeypatch):
    client = Mock()
    client.head_bucket.side_effect = error("404")
    storage(tmp_path, monkeypatch, client, create_bucket=True)
    client.create_bucket.assert_called_once_with(Bucket="private")


def test_conditional_put_and_identical_retry(tmp_path, monkeypatch):
    client = Mock()
    backend = storage(tmp_path, monkeypatch, client)
    payload = b"private content"
    expected = (hashlib.sha256(payload).hexdigest(), len(payload))
    assert backend.put_bytes_idempotent(
        "key", payload, max_bytes=100, content_type="text/plain"
    ) == expected
    assert client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    client.put_object.side_effect = error("PreconditionFailed")
    body = io.BytesIO(payload)
    client.get_object.return_value = {"ContentLength": len(payload), "Body": body}
    assert backend.put_bytes_idempotent(
        "key", payload, max_bytes=100, content_type="text/plain"
    ) == expected
    assert body.closed


def test_conflicting_retry_does_not_overwrite(tmp_path, monkeypatch):
    client = Mock()
    backend = storage(tmp_path, monkeypatch, client)
    client.put_object.side_effect = error("412")
    client.get_object.return_value = {"ContentLength": 3, "Body": io.BytesIO(b"old")}
    with pytest.raises(PrivateStorageError, match="different bytes"):
        backend.put_bytes_idempotent("key", b"new", max_bytes=100, content_type="text/plain")
    assert client.put_object.call_count == 1


def test_unsupported_conditional_write_fails_closed(tmp_path, monkeypatch):
    client = Mock()
    backend = storage(tmp_path, monkeypatch, client)
    client.put_object.side_effect = error("NotImplemented")
    with pytest.raises(PrivateStorageError, match="conditional"):
        backend.put_bytes_idempotent("key", b"new", max_bytes=100, content_type="text/plain")
    client.get_object.assert_not_called()
