"""Tests for simpler_objects.client.

Unit tests cover the pure header/digest helpers. Integration tests launch a real
object server + locator as uvicorn subprocesses (the in-process TestClient used
elsewhere cannot exercise real sockets, Expect: 100-continue, or 307 redirects).
If the subprocesses cannot start, the integration tests skip.
"""

import asyncio
import datetime
import hashlib
import os
import pathlib
import socket
import ssl
import subprocess
import sys
import threading
import time

import httpx
import pycurl
import pytest

from simpler_objects import client

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BUCKET = "mybucket"


# ---------------------------------------------------------------------------
# Unit tests — header / digest helpers
# ---------------------------------------------------------------------------

def test_encode_parse_digest_roundtrip():
    raw = hashlib.sha256(b"abc").digest()
    header = client.encode_digest_header(raw)
    assert header.startswith("sha-256=:") and header.endswith(":")
    assert client.parse_digest_header(header) == raw


def test_parse_digest_header_picks_sha256_among_many():
    raw = hashlib.sha256(b"xyz").digest()
    value = f"sha-512=:AAAA:, {client.encode_digest_header(raw)}"
    assert client.parse_digest_header(value) == raw


def test_parse_digest_header_none_and_missing():
    assert client.parse_digest_header(None) is None
    assert client.parse_digest_header("") is None
    assert client.parse_digest_header("md5=:AAAA:") is None


def test_read_content_disposition():
    assert client.read_content_disposition('attachment; filename="hi.txt"') == "hi.txt"
    assert client.read_content_disposition("inline") is None
    assert client.read_content_disposition(None) is None


def test_read_http_datetime():
    parsed = client.read_http_datetime("Sun, 04 Jan 2026 18:19:00 GMT")
    assert parsed == datetime.datetime(2026, 1, 4, 18, 19, 0)
    assert client.read_http_datetime(None) is None


def test_file_checksum(tmp_path):
    target = tmp_path / "data.bin"
    payload = os.urandom(5 * 1024 * 1024 + 17)  # spans several BLOCK_SIZE reads
    target.write_bytes(payload)
    assert client.file_checksum(target) == hashlib.sha256(payload).digest()


class _RewindingCurl:
    """A fake pycurl.Curl that simulates the locator's 307 body replay.

    perform() streams the body once (as if to the locator), then rewinds via the
    registered SEEKFUNCTION and streams it again (the replay to the object
    server). It records both reads and the seek result so the test can prove the
    rewind actually worked — the path that fails with CURLE_SEND_FAIL_REWIND
    (error 65) when no seek callback is registered.
    """

    def __init__(self):
        self.opts = {}
        self.first_read = b""
        self.replay_read = b""
        self.seek_result = None
        self.probe_seek_result = None

    def setopt(self, opt, value):
        self.opts[opt] = value

    def _drain(self):
        body = self.opts[pycurl.READDATA]
        return b"".join(iter(lambda: body.read(64 * 1024), b""))

    def perform(self):
        seek = self.opts.get(pycurl.SEEKFUNCTION)
        if seek is None:
            # Mirror libcurl: a redirect with an unrewindable body errors here.
            raise pycurl.error(pycurl.E_SEND_FAIL_REWIND,
                               "necessary data rewind was not possible")
        self.first_read = self._drain()
        self.seek_result = seek(0, os.SEEK_SET)
        self.replay_read = self._drain()
        # Probe a non-zero offset while the body is still open: a bare body.seek
        # would return 7 here, not SEEKFUNC_OK, so this guards the wrapper.
        self.probe_seek_result = seek(7, os.SEEK_SET)

    def getinfo(self, _info):
        return 201

    def close(self):
        pass


def test_upload_rewinds_body_on_redirect(tmp_path, monkeypatch):
    """Regression for error 65: the body must be replayable after the 307.

    If the Expect: 100-continue handshake times out the body streams to the
    locator and libcurl must rewind it to replay to the object server. Drive the
    upload through a fake Curl that exercises exactly that read/seek path.
    """
    data = os.urandom(3 * 1024 * 1024 + 7)
    src = tmp_path / "rewind.bin"
    src.write_bytes(data)

    captured = {}

    def _factory():
        captured["curl"] = _RewindingCurl()
        return captured["curl"]

    monkeypatch.setattr(pycurl, "Curl", _factory)

    digest = client.simple_upload(str(src), "http://locator.test/mybucket/key")

    fake = captured["curl"]
    assert digest == hashlib.sha256(data).digest()
    # The body survived the rewind: the replay leg read the whole file again.
    assert fake.replay_read == data
    assert fake.first_read == data
    # The wrapper reports success to libcurl, not the new file offset that a bare
    # body.seek would return (0 here by luck, but non-zero offsets would break).
    assert fake.seek_result == pycurl.SEEKFUNC_OK
    assert fake.probe_seek_result == pycurl.SEEKFUNC_OK
    # The handshake is given well over libcurl's 1 s default to land first.
    assert fake.opts[pycurl.EXPECT_100_TIMEOUT_MS] >= 5000


# ---------------------------------------------------------------------------
# Integration tests — live object server + locator
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for(url: str, timeout: float = 20.0, verify=True) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1, verify=verify).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.15)
    return False


def _spawn(module: str, port: int, env_extra: dict, args=()) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", f"simpler_objects.{module}:app",
         "--port", str(port), "--log-level", "warning", *args],
        cwd=REPO_ROOT, env={**os.environ, **env_extra},
    )


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    """Run a real object server + locator; yield connection info."""
    obj_dir = tmp_path_factory.mktemp("objects")
    (obj_dir / BUCKET).mkdir()
    obj_port, loc_port = _free_port(), _free_port()

    obj_proc = _spawn("object_server", obj_port,
                      {"OBJECT_DIRECTORY": str(obj_dir)})
    loc_proc = _spawn("locator_api", loc_port,
                      {"OBJECT_SERVERS": f"http://127.0.0.1:{obj_port}/"})
    try:
        if not (_wait_for(f"http://127.0.0.1:{obj_port}/health")
                and _wait_for(f"http://127.0.0.1:{loc_port}/health")):
            pytest.skip("could not start object server / locator subprocesses")
        yield {"locator": f"http://127.0.0.1:{loc_port}", "obj_dir": obj_dir}
    finally:
        for proc in (loc_proc, obj_proc):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _object_url(servers, prefix: str) -> str:
    return f"{servers['locator']}/{BUCKET}/{prefix}-{os.urandom(4).hex()}"


def test_upload_download_roundtrip(servers, tmp_path):
    data = os.urandom(2 * 1024 * 1024 + 5)
    src = tmp_path / "src.bin"
    src.write_bytes(data)
    url = _object_url(servers, "rt")

    assert client.simple_upload(str(src), url) == hashlib.sha256(data).digest()

    dst = tmp_path / "dst.bin"
    digest, mime, sugg, mtime = client.simple_download(url, str(dst))
    assert digest == hashlib.sha256(data).digest()
    assert dst.read_bytes() == data
    assert mime is not None
    assert isinstance(mtime, datetime.datetime)


def test_download_missing_raises(servers, tmp_path):
    dst = tmp_path / "missing"
    with pytest.raises(client.ClientError) as excinfo:
        client.simple_download(_object_url(servers, "nope"), str(dst))
    assert excinfo.value.status == 404
    assert not dst.exists()


def test_upload_conflict_raises(servers, tmp_path):
    src = tmp_path / "dup.bin"
    src.write_bytes(b"conflict")
    url = _object_url(servers, "dup")
    client.simple_upload(str(src), url)
    with pytest.raises(client.ClientError) as excinfo:
        client.simple_upload(str(src), url)
    assert excinfo.value.status == 409


def test_download_digest_mismatch_raises(servers, tmp_path):
    """A stored object tampered after upload fails the Repr-Digest check."""
    src = tmp_path / "tamper.bin"
    src.write_bytes(os.urandom(64 * 1024))
    url = _object_url(servers, "corrupt")
    client.simple_upload(str(src), url)

    stored = servers["obj_dir"] / BUCKET / url.rsplit("/", 1)[-1]
    stored.write_bytes(b"tampered")  # checksum record still claims the old hash

    dst = tmp_path / "tamper-dl"
    with pytest.raises(client.ClientError, match="digest mismatch"):
        client.simple_download(url, str(dst))
    assert not dst.exists()


def test_upload_does_not_send_body_to_locator(servers, tmp_path):
    """Issue #26: Expect: 100-continue must keep the body off the locator leg."""
    loc = httpx.URL(servers["locator"])
    counted = {"up": 0}

    async def _handle(client_reader, client_writer):
        up_reader, up_writer = await asyncio.open_connection(loc.host, loc.port)

        async def _pump(src, dst, count):
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    if count:
                        counted["up"] += len(chunk)
                    dst.write(chunk)
                    await dst.drain()
            except OSError:
                pass
            finally:
                if not dst.is_closing():
                    dst.close()

        await asyncio.gather(_pump(client_reader, up_writer, True),
                             _pump(up_reader, client_writer, False))

    proxy = {}
    ready = threading.Event()

    def _serve():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server = loop.run_until_complete(
            asyncio.start_server(_handle, "127.0.0.1", 0))
        proxy["port"] = server.sockets[0].getsockname()[1]
        ready.set()
        loop.run_until_complete(server.serve_forever())

    threading.Thread(target=_serve, daemon=True).start()
    assert ready.wait(5)

    size = 4 * 1024 * 1024
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(size))
    proxy_url = (f"http://127.0.0.1:{proxy['port']}/{BUCKET}/"
                 f"skip-{os.urandom(4).hex()}")
    client.simple_upload(str(src), proxy_url)
    time.sleep(0.4)  # let the proxy drain any buffered bytes

    assert counted["up"] < size * 0.05, (
        f"{counted['up']:,} bytes reached the locator — body was not skipped")


# ---------------------------------------------------------------------------
# Integration tests — secured cluster (CLUSTER_SECRET + AUTH_CONFIG)
# ---------------------------------------------------------------------------

SECRET = "e2e-cluster-secret"
OI_KEY = "e2e-oi-key"


@pytest.fixture(scope="module")
def secured_servers(tmp_path_factory):
    """Run an object server + locator with signing and client auth enabled."""
    obj_dir = tmp_path_factory.mktemp("objects-secured")
    (obj_dir / BUCKET).mkdir()
    auth_toml = tmp_path_factory.mktemp("config") / "auth.toml"
    auth_toml.write_text(f"""
[clients.oi]
key = "{OI_KEY}"
[clients.oi.buckets]
{BUCKET} = ["read", "write", "list"]
""")
    auth_toml.chmod(0o600)
    obj_port, loc_port = _free_port(), _free_port()

    obj_proc = _spawn("object_server", obj_port,
                      {"OBJECT_DIRECTORY": str(obj_dir),
                       "CLUSTER_SECRET": SECRET})
    loc_proc = _spawn("locator_api", loc_port,
                      {"OBJECT_SERVERS": f"http://127.0.0.1:{obj_port}/",
                       "CLUSTER_SECRET": SECRET,
                       "AUTH_CONFIG": str(auth_toml)})
    try:
        if not (_wait_for(f"http://127.0.0.1:{obj_port}/health")
                and _wait_for(f"http://127.0.0.1:{loc_port}/health")):
            pytest.skip("could not start secured object server / locator subprocesses")
        yield {"locator": f"http://127.0.0.1:{loc_port}",
               "object_server": f"http://127.0.0.1:{obj_port}",
               "obj_dir": obj_dir}
    finally:
        for proc in (loc_proc, obj_proc):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_secured_upload_download_roundtrip(secured_servers, tmp_path):
    """Full flow: Bearer key at the locator, signed URL through the 307."""
    data = os.urandom(2 * 1024 * 1024 + 5)
    src = tmp_path / "src.bin"
    src.write_bytes(data)
    url = _object_url(secured_servers, "sec-rt")

    assert client.simple_upload(str(src), url,
                                api_key=OI_KEY) == hashlib.sha256(data).digest()

    dst = tmp_path / "dst.bin"
    digest, _, _, _ = client.simple_download(url, str(dst), api_key=OI_KEY)
    assert digest == hashlib.sha256(data).digest()
    assert dst.read_bytes() == data


def test_secured_upload_without_key_401(secured_servers, tmp_path):
    src = tmp_path / "nokey.bin"
    src.write_bytes(b"data")
    with pytest.raises(client.ClientError) as excinfo:
        client.simple_upload(str(src), _object_url(secured_servers, "nokey"))
    assert excinfo.value.status == 401


def test_secured_download_without_key_401(secured_servers, tmp_path):
    with pytest.raises(client.ClientError) as excinfo:
        client.simple_download(_object_url(secured_servers, "nokey"),
                               str(tmp_path / "nokey-dl"))
    assert excinfo.value.status == 401


def test_secured_basic_auth_browser_flow(secured_servers, tmp_path):
    """A browser-style GET: Basic creds at the locator, bare signed URL after."""
    data = os.urandom(64 * 1024)
    src = tmp_path / "basic.bin"
    src.write_bytes(data)
    url = _object_url(secured_servers, "basic")
    client.simple_upload(str(src), url, api_key=OI_KEY)

    resp = httpx.get(url, auth=("oi", OI_KEY), follow_redirects=True)
    assert resp.status_code == 200
    assert resp.content == data


def test_secured_direct_object_server_needs_signature(secured_servers, tmp_path):
    """Hitting an object server directly without exp/sig is rejected."""
    data = os.urandom(1024)
    src = tmp_path / "direct.bin"
    src.write_bytes(data)
    url = _object_url(secured_servers, "direct")
    client.simple_upload(str(src), url, api_key=OI_KEY)
    key = url.rsplit("/", 1)[-1]

    direct = f"{secured_servers['object_server']}/{BUCKET}/{key}"
    assert httpx.get(direct).status_code == 401
    assert httpx.get(direct + "?exp=9999999999&sig=" + "0" * 64).status_code == 403


# ---------------------------------------------------------------------------
# Integration tests — TLS (private CA) on top of the secured cluster
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tls_servers(tmp_path_factory):
    """Run the full stack over HTTPS: private CA, signing, and client auth."""
    trustme = pytest.importorskip("trustme")
    tls_dir = tmp_path_factory.mktemp("tls")
    ca = trustme.CA()
    ca_pem = tls_dir / "ca.pem"
    ca.cert_pem.write_to_path(ca_pem)
    host_cert = ca.issue_cert("127.0.0.1", "localhost")
    cert_pem, key_pem = tls_dir / "host.crt", tls_dir / "host.key"
    with cert_pem.open("wb") as f:
        for blob in host_cert.cert_chain_pems:
            f.write(blob.bytes())
    host_cert.private_key_pem.write_to_path(key_pem)
    tls_args = ("--ssl-certfile", str(cert_pem), "--ssl-keyfile", str(key_pem))

    obj_dir = tmp_path_factory.mktemp("objects-tls")
    (obj_dir / BUCKET).mkdir()
    auth_toml = tmp_path_factory.mktemp("config-tls") / "auth.toml"
    auth_toml.write_text(f"""
[clients.oi]
key = "{OI_KEY}"
[clients.oi.buckets]
{BUCKET} = ["read", "write", "list"]
""")
    auth_toml.chmod(0o600)
    obj_port, loc_port = _free_port(), _free_port()

    obj_proc = _spawn("object_server", obj_port,
                      {"OBJECT_DIRECTORY": str(obj_dir),
                       "CLUSTER_SECRET": SECRET},
                      args=tls_args)
    loc_proc = _spawn("locator_api", loc_port,
                      {"OBJECT_SERVERS": f"https://127.0.0.1:{obj_port}/",
                       "CLUSTER_SECRET": SECRET,
                       "AUTH_CONFIG": str(auth_toml),
                       "CA_BUNDLE": str(ca_pem)},
                      args=tls_args)
    ca_ctx = ssl.create_default_context(cafile=str(ca_pem))
    try:
        if not (_wait_for(f"https://127.0.0.1:{obj_port}/health", verify=ca_ctx)
                and _wait_for(f"https://127.0.0.1:{loc_port}/health", verify=ca_ctx)):
            pytest.skip("could not start TLS object server / locator subprocesses")
        yield {"locator": f"https://127.0.0.1:{loc_port}", "ca": str(ca_pem)}
    finally:
        for proc in (loc_proc, obj_proc):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_tls_upload_download_roundtrip(tls_servers, tmp_path):
    """HTTPS end to end: locator leg, redirect leg, and the locator's own
    probes to the object server all verify against the private CA."""
    data = os.urandom(1024 * 1024 + 3)
    src = tmp_path / "src.bin"
    src.write_bytes(data)
    url = _object_url(tls_servers, "tls-rt")

    assert client.simple_upload(str(src), url, api_key=OI_KEY,
                                ca_bundle=tls_servers["ca"]) == hashlib.sha256(data).digest()

    dst = tmp_path / "dst.bin"
    digest, _, _, _ = client.simple_download(url, str(dst), api_key=OI_KEY,
                                             ca_bundle=tls_servers["ca"])
    assert digest == hashlib.sha256(data).digest()
    assert dst.read_bytes() == data


def test_tls_untrusted_ca_rejected(tls_servers, tmp_path):
    """Without the CA bundle, certificate verification fails — proof that
    the client actually verifies rather than silently accepting any cert."""
    with pytest.raises(client.ClientError):
        client.simple_download(_object_url(tls_servers, "untrusted"),
                               str(tmp_path / "untrusted-dl"), api_key=OI_KEY)
