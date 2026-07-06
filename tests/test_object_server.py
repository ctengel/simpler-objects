"""Phase 1 tests — Repr-Digest, Content-Digest, and Content-Type headers."""

import base64
import errno as errno_mod
import fcntl
import hashlib
import os
import pytest

from fastapi.testclient import TestClient

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


def test_head_bucket_exists(client):
    """HEAD /{bucket}/ on an existing bucket returns 200."""
    resp = client.head(f"/{BUCKET}/")
    assert resp.status_code == 200


def test_head_bucket_missing(client):
    """HEAD /{bucket}/ on a missing bucket returns 404."""
    resp = client.head("/no-such-bucket/")
    assert resp.status_code == 404


def test_bucket_no_slash_redirects():
    """HEAD /{bucket} (no trailing slash) returns a 307 to /{bucket}/.

    External clients may rely on this Starlette redirect_slashes behaviour, so
    it is locked in here. A plain TestClient is used because /{bucket} is
    intentionally undocumented and ValidatingTestClient asserts a matching
    openapi.yaml path template exists.
    """
    plain = TestClient(server.app)
    resp = plain.head(f"/{BUCKET}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"].endswith(f"/{BUCKET}/")


@pytest.fixture()
def readonly_client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OBJECT_DIRECTORY", str(tmp_path))
    monkeypatch.setattr(server, "READ_ONLY", True)
    (tmp_path / BUCKET).mkdir()
    return ValidatingTestClient(server.app)


def test_readonly_put_rejected(readonly_client):
    resp = readonly_client.put(f"/{BUCKET}/{TEST_FILE}", content=TEST_CONTENT)
    assert resp.status_code == 405


def test_readonly_health_write_false(readonly_client):
    resp = readonly_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["write"] is False
    assert data["read"] is True


def test_readonly_get_allowed(readonly_client, tmp_path):
    """Read-only mode does not block GET on existing objects."""
    obj_path = tmp_path / BUCKET / TEST_FILE
    obj_path.write_bytes(TEST_CONTENT)
    resp = readonly_client.get(f"/{BUCKET}/{TEST_FILE}")
    assert resp.status_code == 200
    assert resp.content == TEST_CONTENT


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


def test_put_existing_key_conflict(uploaded):
    """PUT to a key that already exists returns 409 (O_EXCL)."""
    resp = uploaded.put(f"/{BUCKET}/{TEST_FILE}", content=TEST_CONTENT)
    assert resp.status_code == 409


def test_get_missing_object(client):
    """GET of a key that was never stored returns 404."""
    resp = client.get(f"/{BUCKET}/never-stored.bin")
    assert resp.status_code == 404


def test_get_locked_object_returns_503(uploaded, tmp_path):
    """A GET while a PUT holds the exclusive lock returns 503 + Retry-After."""
    fd = os.open(tmp_path / BUCKET / TEST_FILE, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        resp = uploaded.get(f"/{BUCKET}/{TEST_FILE}")
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "64"
    finally:
        os.close(fd)


def test_put_no_space_returns_507(client, tmp_path, monkeypatch):
    """PUT returns 507 and leaves no partial file when disk is full."""
    def fsync_enospc(fd):
        raise OSError(errno_mod.ENOSPC, "No space left on device")
    monkeypatch.setattr(os, "fsync", fsync_enospc)
    resp = client.put(f"/{BUCKET}/{TEST_FILE}", content=TEST_CONTENT)
    assert resp.status_code == 507
    assert not (tmp_path / BUCKET / TEST_FILE).exists()


def test_path_traversal_returns_404(tmp_path, monkeypatch):
    """safe_path raises 404 for path traversal attempts."""
    from fastapi import HTTPException
    monkeypatch.setattr(server, "OBJECT_DIRECTORY", str(tmp_path))
    with pytest.raises(HTTPException) as exc_info:
        server.safe_path(tmp_path, "..", "etc", "passwd")
    assert exc_info.value.status_code == 404


@pytest.mark.parametrize("filename,content_type,expected_status", [
    ("photo.jpg",  "image/jpeg",                201),  # exact match
    ("readme.txt", "text/plain; charset=utf-8", 201),  # params stripped
    ("photo.jpg",  "application/octet-stream",  201),  # generic binary wildcard
    ("photo.jpg",  None,                        201),  # no header → no validation
    ("datafile",   "text/plain",                201),  # no extension → no validation
    ("movie.mkv",  "video/x-matroska",          201),  # issue #80: cross-distro mkv label
    ("photo.jpg",  "text/plain",                415),  # mismatch
    ("readme.txt", "image/jpeg",                415),  # mismatch
    ("movie.mkv",  "video/mp4",                 415),  # recognized type, wrong extension
])
def test_put_content_type_extension_validation(client, tmp_path, filename, content_type, expected_status):
    headers = {}
    if content_type is not None:
        headers["Content-Type"] = content_type
    resp = client.put(f"/{BUCKET}/{filename}", content=b"data", headers=headers)
    assert resp.status_code == expected_status
    if expected_status == 415:
        assert not (tmp_path / BUCKET / filename).exists()


def test_get_skips_malformed_checksum_line(uploaded, tmp_path):
    """A torn line in the bucket checksum file does not break GET."""
    cksum_file = tmp_path / f"{BUCKET}.sha256"
    cksum_file.write_text("corrupt-single-field\n" + cksum_file.read_text())
    resp = uploaded.get(f"/{BUCKET}/{TEST_FILE}")
    assert resp.status_code == 200
    assert resp.content == TEST_CONTENT
    assert resp.headers["Repr-Digest"] == _expected_digest(TEST_CONTENT)


# ---------------------------------------------------------------------------
# Signed-URL enforcement (CLUSTER_SECRET) — see simpler_objects/auth.py
# ---------------------------------------------------------------------------

from simpler_objects import auth  # noqa: E402

SECRET = "test-cluster-secret"


@pytest.fixture()
def secured(client, monkeypatch):
    """Client against a server with signature enforcement enabled."""
    monkeypatch.setattr(server, "CLUSTER_SECRET", SECRET)
    return client


def _sq(operation, bucket=BUCKET, key=TEST_FILE, secret=SECRET, ttl=900):
    """Query suffix like the locator's signed_suffix(), with knobs for tests."""
    return "?" + auth.signed_query(secret, operation, bucket, key, ttl)


def test_signed_put_and_get_round_trip(secured):
    resp = secured.put(f"/{BUCKET}/{TEST_FILE}" + _sq(auth.OP_WRITE), content=TEST_CONTENT)
    assert resp.status_code == 201
    resp = secured.get(f"/{BUCKET}/{TEST_FILE}" + _sq(auth.OP_READ))
    assert resp.status_code == 200
    assert resp.content == TEST_CONTENT


def test_signed_head_shares_read_signature(secured):
    """HEAD and GET both map to 'read': one signature serves both."""
    assert secured.put(f"/{BUCKET}/{TEST_FILE}" + _sq(auth.OP_WRITE),
                       content=TEST_CONTENT).status_code == 201
    suffix = _sq(auth.OP_READ)
    assert secured.head(f"/{BUCKET}/{TEST_FILE}" + suffix).status_code == 200
    assert secured.get(f"/{BUCKET}/{TEST_FILE}" + suffix).status_code == 200


def test_unsigned_requests_rejected_401(secured, tmp_path):
    assert secured.put(f"/{BUCKET}/{TEST_FILE}", content=TEST_CONTENT).status_code == 401
    assert not (tmp_path / BUCKET / TEST_FILE).exists()
    assert secured.get(f"/{BUCKET}/{TEST_FILE}").status_code == 401
    assert secured.get(f"/{BUCKET}/").status_code == 401
    assert secured.head(f"/{BUCKET}/").status_code == 401


def test_tampered_signature_rejected_403(secured, tmp_path):
    suffix = _sq(auth.OP_WRITE)
    bad = suffix[:-1] + ("0" if suffix[-1] != "0" else "1")
    resp = secured.put(f"/{BUCKET}/{TEST_FILE}" + bad, content=TEST_CONTENT)
    assert resp.status_code == 403
    assert not (tmp_path / BUCKET / TEST_FILE).exists()


def test_wrong_operation_signature_rejected_403(secured, tmp_path):
    """A read signature must not authorize a PUT."""
    resp = secured.put(f"/{BUCKET}/{TEST_FILE}" + _sq(auth.OP_READ), content=TEST_CONTENT)
    assert resp.status_code == 403
    assert not (tmp_path / BUCKET / TEST_FILE).exists()


def test_wrong_key_signature_rejected_403(secured):
    """A signature minted for one key must not open another."""
    assert secured.put(f"/{BUCKET}/{TEST_FILE}" + _sq(auth.OP_WRITE),
                       content=TEST_CONTENT).status_code == 201
    stolen = _sq(auth.OP_READ, key="other.bin")
    assert secured.get(f"/{BUCKET}/{TEST_FILE}" + stolen).status_code == 403


def test_expired_signature_rejected_403(secured):
    expired = _sq(auth.OP_READ, ttl=-(auth.CLOCK_SKEW + 5))
    assert secured.get(f"/{BUCKET}/{TEST_FILE}" + expired).status_code == 403


def test_expiry_within_clock_skew_accepted(secured):
    assert secured.put(f"/{BUCKET}/{TEST_FILE}" + _sq(auth.OP_WRITE),
                       content=TEST_CONTENT).status_code == 201
    just_expired = _sq(auth.OP_READ, ttl=-(auth.CLOCK_SKEW - 30))
    assert secured.get(f"/{BUCKET}/{TEST_FILE}" + just_expired).status_code == 200


def test_signed_bucket_listing(secured):
    suffix = "?" + auth.signed_query(SECRET, auth.OP_LIST, BUCKET)
    assert secured.head(f"/{BUCKET}/" + suffix).status_code == 200
    resp = secured.get(f"/{BUCKET}/" + suffix)
    assert resp.status_code == 200
    assert resp.json()["bucket"] == BUCKET


def test_health_and_root_stay_open(secured):
    """Health stays credential-less; bucket enumeration stays 403."""
    assert secured.get("/health").status_code == 200
    assert secured.get("/").status_code == 403


def test_no_secret_means_no_enforcement(client):
    """Without CLUSTER_SECRET, unsigned requests work exactly as before."""
    assert client.put(f"/{BUCKET}/{TEST_FILE}", content=TEST_CONTENT).status_code == 201
    assert client.get(f"/{BUCKET}/{TEST_FILE}").status_code == 200
