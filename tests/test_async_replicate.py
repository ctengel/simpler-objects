"""Tests for async_replicate.py — all HTTP calls are mocked with respx.

Issue #36 focus: replicate_object must send Content-Length (not chunked TE)
when piping a streaming GET body into a PUT. The test verifies the outgoing
PUT request carries the correct Content-Length header.
"""

import base64
import hashlib
import sys
from unittest.mock import patch

import httpx
import pytest
import respx

from simpler_objects.async_replicate import (
    auto_replica,
    cli,
    find_space,
    get_bucket_contents,
    get_object_size,
    replicate_object,
)

LOCATOR = "http://locator/"
SERVER_A = "http://server-a/"
SERVER_B = "http://server-b/"
BUCKET = "mybucket"
KEY = "myfile.bin"
SRC = SERVER_A + BUCKET + "/" + KEY
DST = SERVER_B + BUCKET + "/" + KEY

CONTENT = b"Hello, replicated world!"
CKSUM = "sha-256=:" + base64.b64encode(hashlib.sha256(CONTENT).digest()).decode() + ":"


def _health(write=True, available=10 ** 9, percent=50):
    return {
        "write": write,
        "read": True,
        "quota-available-bytes": available,
        "quota-used-bytes": 0,
        "percent": percent,
    }


# ---------------------------------------------------------------------------
# find_space
# ---------------------------------------------------------------------------

@respx.mock
def test_find_space_returns_candidate():
    health = {"servers": {SERVER_A: _health(), SERVER_B: _health()}}
    respx.get(LOCATOR + "health").mock(return_value=httpx.Response(200, json=health))
    respx.head(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200))
    result = find_space(LOCATOR, BUCKET, 1024, current=[SERVER_A], desired=1)
    assert len(result) == 1
    assert result[0] == SERVER_B


@respx.mock
def test_find_space_excludes_current():
    health = {"servers": {SERVER_A: _health(), SERVER_B: _health()}}
    respx.get(LOCATOR + "health").mock(return_value=httpx.Response(200, json=health))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200))
    result = find_space(LOCATOR, BUCKET, 1024, current=[SERVER_A], desired=1)
    assert SERVER_A not in result


@respx.mock
def test_find_space_no_writable_servers():
    no_space = _health(write=False, available=0, percent=0)
    health = {"servers": {SERVER_A: no_space, SERVER_B: no_space}}
    respx.get(LOCATOR + "health").mock(return_value=httpx.Response(200, json=health))
    result = find_space(LOCATOR, BUCKET, 1024, current=[], desired=2)
    assert result == []


@respx.mock
def test_find_space_bucket_missing_excluded():
    """A server where the bucket doesn't exist is dropped from candidates."""
    health = {"servers": {SERVER_A: _health(), SERVER_B: _health()}}
    respx.get(LOCATOR + "health").mock(return_value=httpx.Response(200, json=health))
    respx.head(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(404))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200))
    result = find_space(LOCATOR, BUCKET, 1024, current=[], desired=2)
    assert SERVER_A not in result
    assert SERVER_B in result


# ---------------------------------------------------------------------------
# get_object_size
# ---------------------------------------------------------------------------

@respx.mock
def test_get_object_size_success():
    respx.head(SRC).mock(return_value=httpx.Response(
        200,
        headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM},
    ))
    size, cksum = get_object_size(SRC)
    assert size == len(CONTENT)
    assert cksum == CKSUM


@respx.mock
def test_get_object_size_404_skip():
    respx.head(DST).mock(return_value=httpx.Response(404))
    size, cksum = get_object_size(DST, skip_404=True)
    assert size == 0
    assert cksum is None


@respx.mock
def test_get_object_size_no_digest():
    respx.head(SRC).mock(return_value=httpx.Response(
        200,
        headers={"Content-Length": str(len(CONTENT))},
    ))
    size, cksum = get_object_size(SRC)
    assert size == len(CONTENT)
    assert cksum is None


# ---------------------------------------------------------------------------
# get_bucket_contents
# ---------------------------------------------------------------------------

@respx.mock
def test_get_bucket_contents():
    bucket_url = SERVER_A + BUCKET + "/"
    payload = {
        "objects": {
            "file.txt": {"size": 42, "directory": False, "checksum": "sha256:abc"},
            "subdir/": {"size": 0, "directory": True, "checksum": None},
        }
    }
    respx.get(bucket_url).mock(return_value=httpx.Response(200, json=payload))
    result = get_bucket_contents(bucket_url)
    assert "file.txt" in result
    assert "subdir/" not in result  # directories are excluded
    assert result["file.txt"] == (42, "sha256:abc")


# ---------------------------------------------------------------------------
# replicate_object — issue #36 focus
# ---------------------------------------------------------------------------

def _dst_head_sequence(*responses):
    """Return a side_effect callable that yields responses in order for HEAD DST.

    replicate_object calls HEAD DST twice: once to assert it is empty (expects
    404), and once after the PUT to verify the object landed (expects 200).
    respx re-uses the same route for the same URL, so a plain .mock() would
    overwrite the first response. A callable side_effect consumes the iterator.
    """
    it = iter(responses)
    return lambda _req: next(it)


@respx.mock
def test_replicate_object_content_length_header():
    """PUT must carry Content-Length, not Transfer-Encoding: chunked (issue #36)."""
    respx.head(SRC).mock(return_value=httpx.Response(
        200,
        headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM},
    ))
    respx.head(DST).mock(side_effect=_dst_head_sequence(
        httpx.Response(404),
        httpx.Response(200, headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM}),
    ))
    respx.get(SRC).mock(return_value=httpx.Response(
        200,
        content=CONTENT,
        headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM},
    ))
    put_route = respx.put(DST).mock(return_value=httpx.Response(201))

    result = replicate_object(SRC, DST)

    assert result == len(CONTENT)
    assert put_route.called
    put_req = put_route.calls[0].request
    assert put_req.headers["content-length"] == str(len(CONTENT))
    assert "transfer-encoding" not in put_req.headers
    assert put_req.headers["content-digest"] == CKSUM


@respx.mock
def test_replicate_object_returns_size():
    respx.head(SRC).mock(return_value=httpx.Response(
        200,
        headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM},
    ))
    respx.head(DST).mock(side_effect=_dst_head_sequence(
        httpx.Response(404),
        httpx.Response(200, headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM}),
    ))
    respx.get(SRC).mock(return_value=httpx.Response(
        200,
        content=CONTENT,
        headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM},
    ))
    respx.put(DST).mock(return_value=httpx.Response(201))

    assert replicate_object(SRC, DST) == len(CONTENT)


@respx.mock
def test_replicate_object_aborts_if_dest_exists():
    """replicate_object asserts the destination is empty before transferring."""
    respx.head(SRC).mock(return_value=httpx.Response(
        200,
        headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM},
    ))
    # Destination already has the object
    respx.head(DST).mock(return_value=httpx.Response(
        200,
        headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM},
    ))

    with pytest.raises(AssertionError):
        replicate_object(SRC, DST)


# ---------------------------------------------------------------------------
# auto_replica — parallel-operation safety (PR #67 comment)
# ---------------------------------------------------------------------------

NEEDLE_KEY = "needs_replica.bin"
NEEDLE_SRC = SERVER_A + BUCKET + "/" + NEEDLE_KEY
NEEDLE_DST = SERVER_B + BUCKET + "/" + NEEDLE_KEY


@respx.mock
def test_auto_replica_skips_no_checksum_object():
    """Objects with no checksum (mid-PUT) are warned about and skipped; job continues."""
    contents = {"objects": {
        "partial.bin": {"size": 50, "directory": False, "checksum": None,
                        "locations": [SERVER_A], "error": False},
        "done.bin": {"size": len(CONTENT), "directory": False, "checksum": CKSUM,
                     "locations": [SERVER_A, SERVER_B], "error": False},
    }}
    respx.get(LOCATOR + BUCKET + "/").mock(return_value=httpx.Response(200, json=contents))

    with pytest.warns(UserWarning, match="partial.bin"):
        result = auto_replica(LOCATOR, BUCKET, 2)

    # done.bin already has 2 replicas so no replication work was needed, but
    # the partial object must still flip the error flag.
    assert result is False


@respx.mock
def test_auto_replica_continues_past_partial_object():
    """Replication of complete objects proceeds even when a partial one is skipped."""
    contents = {"objects": {
        "partial.bin": {"size": 50, "directory": False, "checksum": None,
                        "locations": [SERVER_A], "error": False},
        NEEDLE_KEY: {"size": len(CONTENT), "directory": False, "checksum": CKSUM,
                     "locations": [SERVER_A], "error": False},
    }}
    respx.get(LOCATOR + BUCKET + "/").mock(return_value=httpx.Response(200, json=contents))

    # find_space internals: health check + bucket existence on candidate
    health = {"servers": {SERVER_A: _health(), SERVER_B: _health()}}
    respx.get(LOCATOR + "health").mock(return_value=httpx.Response(200, json=health))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200))

    # replicate_object internals for NEEDLE_KEY
    respx.head(NEEDLE_SRC).mock(return_value=httpx.Response(
        200, headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM},
    ))
    respx.head(NEEDLE_DST).mock(side_effect=_dst_head_sequence(
        httpx.Response(404),
        httpx.Response(200, headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM}),
    ))
    respx.get(NEEDLE_SRC).mock(return_value=httpx.Response(
        200, content=CONTENT,
        headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM},
    ))
    put_route = respx.put(NEEDLE_DST).mock(return_value=httpx.Response(201))

    with pytest.warns(UserWarning, match="partial.bin"):
        result = auto_replica(LOCATOR, BUCKET, 2)

    assert result is False          # error flag set for the partial object
    assert put_route.called         # replication of the complete object ran


# ---------------------------------------------------------------------------
# auto_replica --evac (issue #69)
# ---------------------------------------------------------------------------

SERVER_C = "http://server-c/"


def _mock_replication(src_server, dst_server, key):
    """Mock the replicate_object internals for one src => dst copy; return PUT route."""
    src = src_server + BUCKET + "/" + key
    dst = dst_server + BUCKET + "/" + key
    respx.head(src).mock(return_value=httpx.Response(
        200, headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM},
    ))
    respx.head(dst).mock(side_effect=_dst_head_sequence(
        httpx.Response(404),
        httpx.Response(200, headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM}),
    ))
    respx.get(src).mock(return_value=httpx.Response(
        200, content=CONTENT,
        headers={"Content-Length": str(len(CONTENT)), "Repr-Digest": CKSUM},
    ))
    return respx.put(dst).mock(return_value=httpx.Response(201))


@respx.mock
def test_auto_replica_evac_replica_does_not_count():
    """A replica on the evacuating node doesn't count: a new copy is made elsewhere."""
    contents = {"objects": {
        KEY: {"size": len(CONTENT), "directory": False, "checksum": CKSUM,
              "locations": [SERVER_A, SERVER_B], "error": False},
    }}
    respx.get(LOCATOR + BUCKET + "/").mock(return_value=httpx.Response(200, json=contents))
    health = {"servers": {SERVER_A: _health(), SERVER_B: _health(), SERVER_C: _health()}}
    respx.get(LOCATOR + "health").mock(return_value=httpx.Response(200, json=health))
    respx.head(SERVER_C + BUCKET + "/").mock(return_value=httpx.Response(200))
    put_route = _mock_replication(SERVER_B, SERVER_C, KEY)

    assert auto_replica(LOCATOR, BUCKET, 2, evacuate=[SERVER_A]) is True
    assert put_route.called
    # source must be the surviving replica, not the evacuating node
    get_calls = [c for c in respx.calls if c.request.method == "GET"
                 and str(c.request.url).endswith(KEY)]
    assert all(str(c.request.url).startswith(SERVER_B) for c in get_calls)


@respx.mock
def test_auto_replica_evac_never_a_destination():
    """Even a writable evac node is never chosen as a copy target."""
    contents = {"objects": {
        KEY: {"size": len(CONTENT), "directory": False, "checksum": CKSUM,
              "locations": [SERVER_B], "error": False},
    }}
    respx.get(LOCATOR + BUCKET + "/").mock(return_value=httpx.Response(200, json=contents))
    # evac node reports write=True (operator forgot read-only) yet must be skipped
    health = {"servers": {SERVER_A: _health(), SERVER_B: _health(), SERVER_C: _health()}}
    respx.get(LOCATOR + "health").mock(return_value=httpx.Response(200, json=health))
    respx.head(SERVER_C + BUCKET + "/").mock(return_value=httpx.Response(200))
    put_route = _mock_replication(SERVER_B, SERVER_C, KEY)

    assert auto_replica(LOCATOR, BUCKET, 2, evacuate=[SERVER_A]) is True
    assert put_route.called
    assert not any(c.request.method == "PUT" and str(c.request.url).startswith(SERVER_A)
                   for c in respx.calls)


@respx.mock
def test_auto_replica_evac_sole_copy_read_from_evac():
    """When the evac node holds the only copy it is still used as the source."""
    contents = {"objects": {
        KEY: {"size": len(CONTENT), "directory": False, "checksum": CKSUM,
              "locations": [SERVER_A], "error": False},
    }}
    respx.get(LOCATOR + BUCKET + "/").mock(return_value=httpx.Response(200, json=contents))
    health = {"servers": {SERVER_A: _health(), SERVER_B: _health()}}
    respx.get(LOCATOR + "health").mock(return_value=httpx.Response(200, json=health))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200))
    put_route = _mock_replication(SERVER_A, SERVER_B, KEY)

    # replicas=1: the evac copy doesn't count, so one new copy is made from it
    assert auto_replica(LOCATOR, BUCKET, 1, evacuate=[SERVER_A]) is True
    assert put_route.called


@respx.mock
def test_auto_replica_no_evac_unchanged():
    """Without evacuate, a fully-replicated object triggers no traffic at all."""
    contents = {"objects": {
        KEY: {"size": len(CONTENT), "directory": False, "checksum": CKSUM,
              "locations": [SERVER_A, SERVER_B], "error": False},
    }}
    respx.get(LOCATOR + BUCKET + "/").mock(return_value=httpx.Response(200, json=contents))

    assert auto_replica(LOCATOR, BUCKET, 2) is True
    assert all(c.request.method == "GET" for c in respx.calls)


# ---------------------------------------------------------------------------
# cli env-var override
# ---------------------------------------------------------------------------

def test_cli_dash_bucket_uses_underscore_env_var(monkeypatch):
    """Dashes in bucket names become underscores in the REPLICAS_ env var (issue #71)."""
    monkeypatch.setenv("REPLICAS_MY_BACKUPS", "5")
    calls = []

    def fake_auto_replica(locator, bucket, replicas, evacuate=()):
        calls.append((bucket, replicas))
        return True

    with patch("simpler_objects.async_replicate.auto_replica", fake_auto_replica), \
         patch("sys.argv", ["prog", "http://locator/", "my-backups"]):
        with pytest.raises(SystemExit) as exc:
            cli()

    assert exc.value.code == 0
    assert calls == [("my-backups", 5)]


def test_cli_evac_normalizes_trailing_slash():
    """--evac URLs get a trailing slash appended and are passed to auto_replica."""
    calls = []

    def fake_auto_replica(locator, bucket, replicas, evacuate=()):
        calls.append((bucket, replicas, evacuate))
        return True

    with patch("simpler_objects.async_replicate.auto_replica", fake_auto_replica), \
         patch("sys.argv", ["prog", "http://locator/", "mybucket", "--replicas", "2",
                            "--evac", "http://server-a", "--evac", "http://server-b/"]):
        with pytest.raises(SystemExit) as exc:
            cli()

    assert exc.value.code == 0
    assert calls == [("mybucket", 2, ["http://server-a/", "http://server-b/"])]
