"""Tests for locator_api.py — all object-server calls are mocked with respx.

Every test that exercises a redirect-returning endpoint passes
``follow_redirects=False``. This is required, not optional: Starlette's
TestClient dispatches every request to the wrapped ASGI app regardless of the
URL host, so a followed 307 (whose Location points at an object server) is
routed straight back into the locator app — which 307s again, looping until
httpx raises TooManyRedirects. That loop is an artifact of the in-process test
transport, not a production bug; a real client follows the redirect once to a
genuinely different host. Assert the 307 and its Location directly instead.
"""

import httpx
import pytest
import respx

import simpler_objects.locator_api as locator
from tests.openapi_validation import ValidatingTestClient

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
    # Context manager so the lifespan handler runs and sets app.state.client.
    with ValidatingTestClient(locator.app) as test_client:
        yield test_client


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


@respx.mock
def test_find_object_busy_returns_503(client):
    """An explicit 503 (object mid-upload) must propagate, not become a 404."""
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(503))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    resp = client.get(f"/{OBJ_PATH}", follow_redirects=False)
    assert resp.status_code == 503
    assert "Retry-After" in resp.headers


@respx.mock
def test_find_object_retry_after_forwarded(client):
    """The upstream Retry-After is echoed on the locator's 503."""
    respx.head(SERVER_A + OBJ_PATH).mock(
        return_value=httpx.Response(503, headers={"Retry-After": "64"}))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    resp = client.get(f"/{OBJ_PATH}", follow_redirects=False)
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "64"


@respx.mock
def test_find_object_timeout_is_not_found(client):
    """A timeout is the absence of an answer, not evidence the object exists."""
    respx.head(SERVER_A + OBJ_PATH).mock(side_effect=httpx.ReadTimeout("slow"))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    resp = client.get(f"/{OBJ_PATH}", follow_redirects=False)
    assert resp.status_code == 404


@respx.mock
def test_find_object_unreachable_is_not_found(client):
    """An unreachable server does not turn a genuine 404 into a 503."""
    respx.head(SERVER_A + OBJ_PATH).mock(side_effect=httpx.ConnectError("down"))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    resp = client.get(f"/{OBJ_PATH}", follow_redirects=False)
    assert resp.status_code == 404


@respx.mock
def test_find_object_busy_beats_unreachable(client):
    """An explicit 503 wins over a server that simply could not be reached."""
    respx.head(SERVER_A + OBJ_PATH).mock(side_effect=httpx.ConnectError("down"))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(503))
    resp = client.get(f"/{OBJ_PATH}", follow_redirects=False)
    assert resp.status_code == 503


@respx.mock
def test_find_object_busy_then_found(client):
    """A live replica still wins even when another replica is mid-upload."""
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(503))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(200))
    resp = client.get(f"/{OBJ_PATH}", follow_redirects=False)
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
def test_add_object_mid_write_returns_409(client):
    """A 503 on the existence check (key being written/replicated) must be treated as
    a conflict — the key is in use and cannot be claimed by a new PUT."""
    respx.get(SERVER_A + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(503))
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
def test_add_object_broken_server_excluded_not_conflict(client):
    """A 500 on the existence check must drop the server, not raise 409."""
    respx.get(SERVER_A + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(500))
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


@respx.mock
def test_add_object_bucket_probe_redirect_excluded(client):
    """A bucket probe answering 307 must not count as "bucket exists" (#39).

    httpx's raise_for_status() rejects any 3xx, so check_bucket drops the
    server (this is the behaviour the requests->httpx migration brought, and
    the real fix for #39). With the bucket on no server the only correct
    answer is 507 — a recurrence that read the 307 as "exists" would instead
    307 the PUT to SERVER_A.
    """
    respx.get(SERVER_A + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(404))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    respx.head(SERVER_A + BUCKET + "/").mock(
        return_value=httpx.Response(307, headers={"Location": SERVER_A + "elsewhere/"}))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(404))
    resp = client.put(f"/{OBJ_PATH}", headers={"Content-Length": "100"}, follow_redirects=False)
    assert resp.status_code == 507


def test_add_object_content_type_mismatch(client):
    """Locator returns 415 when Content-Type disagrees with the key's extension."""
    resp = client.put(f"/{BUCKET}/photo.jpg",
                      headers={"Content-Length": "100", "Content-Type": "text/plain"},
                      follow_redirects=False)
    assert resp.status_code == 415


def test_add_object_content_type_octet_stream_accepted(client):
    """application/octet-stream is accepted for any extension, passes the 415 guard."""
    # No upstream mocks — the request proceeds past 415 and hits 507 (no servers).
    resp = client.put(f"/{BUCKET}/photo.jpg",
                      headers={"Content-Length": "100", "Content-Type": "application/octet-stream"},
                      follow_redirects=False)
    assert resp.status_code != 415


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
def test_list_bucket_server_down(client):
    """An unreachable object server yields 503, not an uncaught-exception 500."""
    obj_a = {"objects": {"obj1": {"size": 5, "directory": False, "checksum": "sha256:abc"}}}
    respx.get(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200, json=obj_a))
    respx.get(SERVER_B + BUCKET + "/").mock(side_effect=httpx.ConnectError("down"))
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


# ---------------------------------------------------------------------------
# Signed URLs (CLUSTER_SECRET) — probes and Locations carry exp/sig
# ---------------------------------------------------------------------------

from simpler_objects import auth  # noqa: E402

SECRET = "test-cluster-secret"


@pytest.fixture()
def secured_client(monkeypatch):
    monkeypatch.setattr(locator, "OBJECT_SERVERS", f"{SERVER_A},{SERVER_B}")
    monkeypatch.setattr(locator, "CLUSTER_SECRET", SECRET)
    with ValidatingTestClient(locator.app) as test_client:
        yield test_client


def _assert_signed(url, operation, bucket=BUCKET, key=""):
    """Assert a URL (httpx.URL or str) carries a valid signature."""
    params = httpx.URL(str(url)).params
    assert auth.verify(SECRET, operation, bucket, key,
                       params.get("exp"), params.get("sig"))


@respx.mock
def test_find_object_location_and_probe_signed(secured_client):
    route_a = respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(200))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(200))
    resp = secured_client.get(f"/{OBJ_PATH}", follow_redirects=False)
    assert resp.status_code == 307
    location = resp.headers["location"]
    # The Location must open the object for the client (read op) …
    _assert_signed(location, auth.OP_READ, key=KEY)
    # … and the locator's own probe carried the same suffix.
    if route_a.called:
        probe_url = route_a.calls[0].request.url
        assert str(probe_url).endswith(str(httpx.URL(location)).split("?", 1)[1])


@respx.mock
def test_add_object_signs_probes_and_location(secured_client):
    respx.get(SERVER_A + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=_health()))
    exists_a = respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(404))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    bucket_a = respx.head(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200))
    resp = secured_client.put(f"/{OBJ_PATH}", headers={"Content-Length": "100"},
                              follow_redirects=False)
    assert resp.status_code == 307
    # Redirect authorizes the client's PUT.
    _assert_signed(resp.headers["location"], auth.OP_WRITE, key=KEY)
    # Existence probe is signed as read; bucket probe as list.
    _assert_signed(exists_a.calls[0].request.url, auth.OP_READ, key=KEY)
    _assert_signed(bucket_a.calls[0].request.url, auth.OP_LIST)


@respx.mock
def test_head_bucket_probe_signed(secured_client):
    route = respx.head(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(404))
    assert secured_client.head(f"/{BUCKET}/").status_code == 200
    _assert_signed(route.calls[0].request.url, auth.OP_LIST)


@respx.mock
def test_list_bucket_fanout_signed(secured_client):
    payload = {"objects": {}}
    route = respx.get(SERVER_A + BUCKET + "/").mock(
        return_value=httpx.Response(200, json=payload))
    respx.get(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200, json=payload))
    assert secured_client.get(f"/{BUCKET}/").status_code == 200
    _assert_signed(route.calls[0].request.url, auth.OP_LIST)


@respx.mock
def test_no_secret_means_bare_urls(client):
    """Without CLUSTER_SECRET the Location has no query — today's contract."""
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(200))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(200))
    resp = client.get(f"/{OBJ_PATH}", follow_redirects=False)
    assert resp.status_code == 307
    assert "?" not in resp.headers["location"]


# ---------------------------------------------------------------------------
# Client authn/authz (AUTH_CONFIG) — API keys, Bearer + Basic
# ---------------------------------------------------------------------------

import base64  # noqa: E402

OI_KEY = "oi-secret-key"
RO_KEY = "ro-secret-key"
AUTH_TOML = f"""
[clients.oi]
key = "{OI_KEY}"
[clients.oi.buckets]
{BUCKET} = ["read", "write", "list"]

[clients.ro]
key = "{RO_KEY}"
[clients.ro.buckets]
{BUCKET} = ["read"]
"""


@pytest.fixture()
def authed_client(monkeypatch, tmp_path):
    config = tmp_path / "auth.toml"
    config.write_text(AUTH_TOML)
    config.chmod(0o600)
    monkeypatch.setattr(locator, "OBJECT_SERVERS", f"{SERVER_A},{SERVER_B}")
    monkeypatch.setattr(locator, "CLUSTER_SECRET", SECRET)
    monkeypatch.setattr(locator, "AUTH_CONFIG", str(config))
    with ValidatingTestClient(locator.app) as test_client:
        yield test_client


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def _basic(name, key):
    return {"Authorization": "Basic " + base64.b64encode(f"{name}:{key}".encode()).decode()}


def _mock_object_found():
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(200))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(200))


@respx.mock
def test_no_credentials_401_with_challenge(authed_client):
    resp = authed_client.get(f"/{OBJ_PATH}", follow_redirects=False)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Basic")


@respx.mock
def test_bad_key_401(authed_client):
    resp = authed_client.get(f"/{OBJ_PATH}", headers=_bearer("wrong-key"),
                             follow_redirects=False)
    assert resp.status_code == 401


@respx.mock
def test_basic_wrong_username_401(authed_client):
    resp = authed_client.get(f"/{OBJ_PATH}", headers=_basic("ro", OI_KEY),
                             follow_redirects=False)
    assert resp.status_code == 401


@respx.mock
def test_bearer_read_redirects(authed_client):
    _mock_object_found()
    resp = authed_client.get(f"/{OBJ_PATH}", headers=_bearer(OI_KEY),
                             follow_redirects=False)
    assert resp.status_code == 307
    _assert_signed(resp.headers["location"], auth.OP_READ, key=KEY)


@respx.mock
def test_basic_read_redirects(authed_client):
    """Browser-style Basic auth works and yields the same signed redirect."""
    _mock_object_found()
    resp = authed_client.get(f"/{OBJ_PATH}", headers=_basic("oi", OI_KEY),
                             follow_redirects=False)
    assert resp.status_code == 307
    _assert_signed(resp.headers["location"], auth.OP_READ, key=KEY)


@respx.mock
def test_wrong_bucket_403(authed_client):
    resp = authed_client.get(f"/otherbucket/{KEY}", headers=_bearer(OI_KEY),
                             follow_redirects=False)
    assert resp.status_code == 403


@respx.mock
def test_read_only_client_cannot_put(authed_client):
    resp = authed_client.put(f"/{OBJ_PATH}", headers={**_bearer(RO_KEY),
                                                      "Content-Length": "100"},
                             follow_redirects=False)
    assert resp.status_code == 403


@respx.mock
def test_read_only_client_cannot_list(authed_client):
    resp = authed_client.get(f"/{BUCKET}/", headers=_bearer(RO_KEY))
    assert resp.status_code == 403


@respx.mock
def test_list_with_permission(authed_client):
    payload = {"objects": {}}
    respx.get(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200, json=payload))
    respx.get(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200, json=payload))
    resp = authed_client.get(f"/{BUCKET}/", headers=_bearer(OI_KEY))
    assert resp.status_code == 200


@respx.mock
def test_authorized_put_redirects_signed(authed_client):
    respx.get(SERVER_A + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.head(SERVER_A + OBJ_PATH).mock(return_value=httpx.Response(404))
    respx.head(SERVER_B + OBJ_PATH).mock(return_value=httpx.Response(404))
    respx.head(SERVER_A + BUCKET + "/").mock(return_value=httpx.Response(200))
    respx.head(SERVER_B + BUCKET + "/").mock(return_value=httpx.Response(200))
    resp = authed_client.put(f"/{OBJ_PATH}", headers={**_bearer(OI_KEY),
                                                      "Content-Length": "100"},
                             follow_redirects=False)
    assert resp.status_code == 307
    _assert_signed(resp.headers["location"], auth.OP_WRITE, key=KEY)


@respx.mock
def test_health_stays_open(authed_client):
    respx.get(SERVER_A + "health").mock(return_value=httpx.Response(200, json=_health()))
    respx.get(SERVER_B + "health").mock(return_value=httpx.Response(200, json=_health()))
    assert authed_client.get("/health").status_code == 200


def test_root_stays_403(authed_client):
    assert authed_client.get("/").status_code == 403


def test_auth_config_without_cluster_secret_refuses_startup(monkeypatch, tmp_path):
    """AUTH_CONFIG without CLUSTER_SECRET would hand out forgeable URLs."""
    config = tmp_path / "auth.toml"
    config.write_text(AUTH_TOML)
    monkeypatch.setattr(locator, "OBJECT_SERVERS", f"{SERVER_A},{SERVER_B}")
    monkeypatch.setattr(locator, "CLUSTER_SECRET", "")
    monkeypatch.setattr(locator, "AUTH_CONFIG", str(config))
    with pytest.raises(RuntimeError, match="CLUSTER_SECRET"):
        with ValidatingTestClient(locator.app):
            pass
