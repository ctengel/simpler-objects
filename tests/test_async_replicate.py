"""Tests for async_replicate.py — all HTTP calls are mocked with respx.

Issue #36 focus: replicate_object must send Content-Length (not chunked TE)
when piping a streaming GET body into a PUT. The test verifies the outgoing
PUT request carries the correct Content-Length header.
"""

import base64
import hashlib

import httpx
import pytest
import respx

from simpler_objects.async_replicate import (
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
