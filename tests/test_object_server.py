"""Phase 1 tests — Repr-Digest, Content-Digest, and Content-Type headers."""

import base64
import hashlib
import pytest

import simpler_objects.object_server as server
from tests.openapi_validation import ValidatingTestClient

BUCKET = "test-bucket"
TEST_FILE = "test-object.bin"
TEST_CONTENT = b"Hello, World!"


def _expected_digest(content: bytes) -> str:
    """Return RFC 9530 sha-256 digest header value for the given bytes."""
    return f"sha-256=:{base64.b64encode(hashlib.sha256(content).digest()).decode()}:"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OBJECT_DIRECTORY", str(tmp_path))
    (tmp_path / BUCKET).mkdir()
    return ValidatingTestClient(server.app)


@pytest.fixture()
def uploaded(client):
    """Client with TEST_FILE already stored in BUCKET."""
    resp = client.put(f"/{BUCKET}/{TEST_FILE}", content=TEST_CONTENT)
    assert resp.status_code == 201
    return client


def test_put(client):
    resp = client.put(f"/{BUCKET}/{TEST_FILE}", content=TEST_CONTENT)
    assert resp.status_code == 201
    expected = _expected_digest(TEST_CONTENT)
    assert resp.headers["Repr-Digest"] == expected


def test_put_length_mismatch(client, tmp_path):
    """A Content-Length disagreeing with the body returns 400 and leaves no file."""
    resp = client.put(
        f"/{BUCKET}/{TEST_FILE}",
        content=TEST_CONTENT,
        headers={"Content-Length": str(len(TEST_CONTENT) + 100)},
    )
    assert resp.status_code == 400
    assert not (tmp_path / BUCKET / TEST_FILE).exists()


def test_get(uploaded):
    resp = uploaded.get(f"/{BUCKET}/{TEST_FILE}")
    assert resp.status_code == 200
    assert resp.content == TEST_CONTENT
    assert "Content-Type" in resp.headers
    expected = _expected_digest(TEST_CONTENT)
    assert resp.headers["Repr-Digest"] == expected


def test_head(uploaded):
    resp = uploaded.head(f"/{BUCKET}/{TEST_FILE}")
    assert resp.status_code == 200
    assert resp.content == b""
    assert "Content-Type" in resp.headers
    expected = _expected_digest(TEST_CONTENT)
    assert resp.headers["Repr-Digest"] == expected


@pytest.mark.parametrize("filename,expected_mime", [
    ("file.txt", "text/plain"),
    ("image.png", "image/png"),
    ("archive.tar.gz", "application/x-tar"),
    ("file.bin", "application/octet-stream"),
])
def test_mime_type(client, filename, expected_mime):
    client.put(f"/{BUCKET}/{filename}", content=b"test")
    resp = client.get(f"/{BUCKET}/{filename}")
    assert resp.headers["Content-Type"].split(";")[0] == expected_mime
