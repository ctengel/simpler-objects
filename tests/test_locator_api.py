"""Tests for locator_api.py — all object-server calls are mocked with respx."""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import simpler_objects.locator_api as locator

SERVER_A = "http://server-a/"
SERVER_B = "http://server-b/"

BUCKET = "mybucket"
KEY = "mykey"
OBJ_PATH = f"{BUCKET}/{KEY}"


def _health(write=True, available=10 ** 9, percent=50):
    return {
        "write": write,
        "read": True,
        "quota-available-bytes": available,
        "quota-used-bytes": 0,
        "percent": percent,
    }


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(locator, "OBJECT_SERVERS", f"{SERVER_A},{SERVER_B}")
    return TestClient(locator.app)


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------

def test_list_buckets_forbidden(client):
    assert client.get("/").status_code == 403


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@respx.mock
def test_health_both_up(client):
    respx.get(SERVER_A + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=_health()))
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["servers"][SERVER_A]["write"] is True
    assert data["servers"][SERVER_B]["write"] is True


@respx.mock
def test_health_server_down(client):
    respx.get(SERVER_A + "health").mock(side_effect=httpx.ConnectError("down"))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=_health()))
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["servers"][SERVER_A]["write"] is False
    assert data["servers"][SERVER_B]["write"] is True


# ---------------------------------------------------------------------------
# GET/HEAD /{bucket}/{key}
# ---------------------------------------------------------------------------

@respx.mock
def test_find_object_found(client):
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(200))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    resp = client.get(f"/{OBJ_PATH}", follow_redirects=False)
    assert resp.status_code == 307
    assert OBJ_PATH in resp.headers["location"]


@respx.mock
def test_find_object_not_found(client):
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(404))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    resp = client.get(f"/{OBJ_PATH}", follow_redirects=False)
    assert resp.status_code == 404


@respx.mock
def test_find_object_head_method(client):
    """HEAD /{bucket}/{key} should also redirect."""
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(200))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(200))
    resp = client.head(f"/{OBJ_PATH}", follow_redirects=False)
    assert resp.status_code == 307


# ---------------------------------------------------------------------------
# PUT /{bucket}/{key}
# ---------------------------------------------------------------------------

def test_add_object_no_content_length(client):
    # TestClient (httpx) sends Content-Length: 0 for empty PUT, so the header
    # is never absent from normal clients. Test the guard via direct invocation.
    import asyncio
    from fastapi import HTTPException as FHTTPException
    with pytest.raises(FHTTPException) as exc_info:
        asyncio.run(locator.add_object(BUCKET, KEY, content_length=None))
    assert exc_info.value.status_code == 411


@respx.mock
def test_add_object_conflict(client):
    respx.get(SERVER_A + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(200))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    resp = client.put(f"/{OBJ_PATH}", headers={"Content-Length": "100"}, follow_redirects=False)
    assert resp.status_code == 409


@respx.mock
def test_add_object_no_writable_servers(client):
    no_space = _health(write=False, available=0, percent=0)
    respx.get(SERVER_A + "health").mock(return_value=httpx.Response(200, json=no_space))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=no_space))
    # Phase 2 checks ALL servers for existence regardless of candidate set.
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(404))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    resp = client.put(f"/{OBJ_PATH}", headers={"Content-Length": "100"}, follow_redirects=False)
    assert resp.status_code == 507


@respx.mock
def test_add_object_success(client):
    respx.get(SERVER_A + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(404))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    respx.head(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200))
    resp = client.put(f"/{OBJ_PATH}", headers={"Content-Length": "100"}, follow_redirects=False)
    assert resp.status_code == 307
    assert OBJ_PATH in resp.headers["location"]


@respx.mock
def test_add_object_unreachable_server_excluded(client):
    """A server that fails the existence check should be dropped from candidates."""
    respx.get(SERVER_A + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=_health()))
    # SERVER_A is unreachable for the existence check → removed from candidates.
    respx.head(SERVER_A + OBJ_PATH).mock(side_effect=httpx.ConnectError("down"))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200))
    resp = client.put(f"/{OBJ_PATH}", headers={"Content-Length": "100"}, follow_redirects=False)
    assert resp.status_code == 307
    assert SERVER_B in resp.headers["location"]


@respx.mock
def test_add_object_bucket_missing(client):
    """If the bucket doesn't exist on any candidate, return 507."""
    respx.get(SERVER_A + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(404))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    respx.head(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(404))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(404))
    resp = client.put(f"/{OBJ_PATH}", headers={"Content-Length": "100"}, follow_redirects=False)
    assert resp.status_code == 507


# ---------------------------------------------------------------------------
# HEAD /{bucket}/
# ---------------------------------------------------------------------------

@respx.mock
def test_head_bucket_exists(client):
    respx.head(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(404))
    resp = client.head(f"/{BUCKET}/")
    assert resp.status_code == 200


@respx.mock
def test_head_bucket_not_found(client):
    respx.head(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(404))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(404))
    resp = client.head(f"/{BUCKET}/")
    assert resp.status_code == 404


@respx.mock
def test_head_bucket_server_error(client):
    respx.head(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(500))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(404))
    resp = client.head(f"/{BUCKET}/")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /{bucket}/
# ---------------------------------------------------------------------------

@respx.mock
def test_list_bucket_merged(client):
    obj_a = {"objects": {"obj1": {"size": 13, "directory": False, "checksum": "sha256:abc"}}}
    obj_b = {"objects": {"obj2": {"size": 42, "directory": False, "checksum": "sha256:def"}}}
    respx.get(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200, json=obj_a))
    respx.get(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200, json=obj_b))
    resp = client.get(f"/{BUCKET}/")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data["objects"]) == {"obj1", "obj2"}
    assert data["objects"]["obj1"]["locations"] == [SERVER_A]
    assert data["objects"]["obj2"]["locations"] == [SERVER_B]


@respx.mock
def test_list_bucket_replicated(client):
    obj = {"size": 13, "directory": False, "checksum": "sha256:abc"}
    both = {"objects": {"obj1": obj}}
    respx.get(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200, json=both))
    respx.get(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200, json=both))
    resp = client.get(f"/{BUCKET}/")
    assert resp.status_code == 200
    data = resp.json()
    locs = data["objects"]["obj1"]["locations"]
    assert len(locs) == 2
    assert SERVER_A in locs
    assert SERVER_B in locs
    assert data["objects"]["obj1"]["error"] is False


@respx.mock
def test_list_bucket_checksum_mismatch(client):
    """An object with different checksums on two servers sets error=True."""
    obj_a = {"objects": {"obj1": {"size": 13, "directory": False, "checksum": "sha256:aaa"}}}
    obj_b = {"objects": {"obj1": {"size": 13, "directory": False, "checksum": "sha256:bbb"}}}
    respx.get(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200, json=obj_a))
    respx.get(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200, json=obj_b))
    resp = client.get(f"/{BUCKET}/")
    assert resp.status_code == 200
    assert resp.json()["objects"]["obj1"]["error"] is True


@respx.mock
def test_list_bucket_server_error(client):
    empty = {"objects": {}}
    respx.get(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200, json=empty))
    respx.get(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(500))
    resp = client.get(f"/{BUCKET}/")
    assert resp.status_code == 503


@respx.mock
def test_list_bucket_missing_on_one_server(client):
    """A 404 from one server is not an error — bucket just isn't there."""
    obj_a = {"objects": {"obj1": {"size": 5, "directory": False, "checksum": "sha256:abc"}}}
    respx.get(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200, json=obj_a))
    respx.get(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(404))
    resp = client.get(f"/{BUCKET}/")
    assert resp.status_code == 200
    assert "obj1" in resp.json()["objects"]
