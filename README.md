# simpler-objects
A simpler object storage service

## Installation

Requires Python 3.12 or newer. Install from a git tag into a virtualenv:

```
pip install git+https://github.com/ctengel/simpler-objects@v0.4.0
```

The base install pulls in `fastapi`, `uvicorn`, and `httpx`. The Python upload/download client (`simpler_objects.client`) depends on `pycurl`, which is split out into an optional extra so the servers can be installed on hosts without libcurl development headers:

```
pip install 'git+https://github.com/ctengel/simpler-objects@v0.4.0#egg=simpler-objects[client]'
```

`[client]` requires `libcurl` headers — `dnf install libcurl-devel` on Fedora, `apt install libcurl4-openssl-dev` on Debian/Raspberry Pi OS.

For development, clone and `pip install -e '.[test]'` to pull in test dependencies (pytest, respx, jsonschema, etc).

## Start storage servers

For each filesystem/drive

```
OBJECT_DIRECTORY=/path/to/objects fastapi dev --port 29171 simpler_objects/object_server.py
```

Alternatively `uvicorn simpler_objects.object_server:app`.

## Start object locator

Do this once

```
OBJECT_SERVERS="http://localhost:29171/" fastapi dev --port 29164 simpler_objects/locator_api.py
```

## Use

PUT an object

```
curl -L -T /path/to/file http://localhost:29164/object_key
```

GET an object:
```
curl -L http://localhost:29164/object_key
```

## Replication

Start up another object server:

```
OBJECT_DIRECTORY=/path/to/more-objects fastapi dev --port 29172 simpler_objects/object_server.py
```

Restart locator with second object server included:

```
OBJECT_SERVERS="http://localhost:29171/,http://localhost:29172/" fastapi dev --port 29164 simpler_objects/locator_api.py
```

Run an asynchronous replication once (handy for debugging):
```
python -m simpler_objects.async_replicate http://localhost:29164/ bucket 2
```

For periodic scheduling, see [`deploy/systemd/README.md`](deploy/systemd/README.md) (systemd timer, one per bucket) or [`deploy/cron/README.md`](deploy/cron/README.md) (cron equivalent).

## Post-crash scrub

PUTs write the body in place to the final key path. A clean failure (client disconnect, length/digest mismatch, ENOSPC) unlinks the partial file before returning, but a hard crash (`SIGKILL`, power loss, kernel panic) can leave an orphan partial file at a key path — and may also leave a torn fragment in the bucket's `<bucket>.sha256` file.

After a crash, **before restarting the object server**, run the scrub utility against its `OBJECT_DIRECTORY`:

```
# Inspect what would be removed (default — dry-run, prints a report)
python -m simpler_objects.scrub /path/to/objects

# Actually unlink orphan files and rewrite <bucket>.sha256 without garbled/stale lines
python -m simpler_objects.scrub --delete-victims --repair-checksums /path/to/objects
```

A "crash victim" is any file in a bucket directory without a matching valid line in `<bucket>.sha256`. The scrub assumes the server is stopped — it doesn't coordinate with a running server.

## On-disk format

The object server keeps all state as plain files: each bucket is a directory of object files, with a sibling `<bucket>.sha256` checksum file in standard `sha256sum` format. There is no database or index.

On-disk-format simplicity is a deliberate design goal. The format is changed only when genuinely necessary, and any change is kept backwards-compatible — existing bucket directories and checksum files keep working without migration. This is why legacy files can be dropped straight into a bucket directory and "just work" (see Validation, below).

## Validation

```
cd /path/to/bucket
sha256sum -c ../bucket.sha256
```

Note that ObjectIndex has a legacy way to manage this. Simply move your old files into above space and it should "just work."

## Performance test

```
./perf_test.sh http://localhost:29171/ big_file small_file
```

## Setting up storage servers

- Ensure your disks are aligned on 4k (or 1MB?) boundaries etc
- create a seperate user `useradd -m`
- create buckets at top level with date `mkdir /path/to/bucket-20000101`
- run in screen
- change `fastapi dev` to `fastapi run` for prod

## Installing as a service

`deploy/systemd/` ships a template object-server unit (one instance per attached disk), a single-instance locator unit, and example env files. The walkthrough in [`deploy/systemd/README.md`](deploy/systemd/README.md) covers Fedora and Raspberry Pi OS — venv install at `/opt/simpler-objects/venv`, mount-path convention at `/srv/simpler-objects/<instance>`, `systemctl enable --now` per instance.

For a fleet of Raspberry Pis, [`deploy/ansible/`](deploy/ansible/) wraps the same units in an Ansible playbook: one inventory file describes every storage Pi and disk, and `ansible-playbook` handles install and future upgrades (`--tags update`).

## Client guidance

### Redirects (307)

The locator returns `307 Temporary Redirect` for `GET`, `HEAD`, and `PUT` on `/{bucket}/{key}`. Clients must follow redirects and must preserve the original HTTP method — particularly for PUT. Use `curl -L`; verify your HTTP library honours 307 method preservation for non-GET requests.

### `Expect: 100-continue` on PUT

A `PUT` to the locator is answered with a `307` redirect, and the locator never reads the request body. Without coordination a client uploads the entire object to the locator and then again to the object server, transferring the data twice. Sending `Expect: 100-continue` avoids this: the locator returns the `307` in place of `100 Continue`, so a compliant client discards the upload and sends the body only to the object server (which does emit `100 Continue`).

Not all clients send `Expect: 100-continue`. `curl` adds it automatically only for request bodies of 1 MiB or larger; for smaller bodies — and for clients such as Python `requests` and `httpx`, which have no `Expect: 100-continue` support — the client streams the whole object to the locator, receives the `307`, then re-uploads it to the object server, transferring the data twice (confirmed here with 8 MiB `requests` and `httpx` uploads). Send `Expect: 100-continue` explicitly on every PUT to avoid this; Python uploaders should use the bundled `simpler_objects.client` library (see below). Even then the client's expect-continue timeout applies: if the locator is slow to select a server (for example when an object server is down) the client may begin uploading before the `307` arrives.

### Python clients: use `simpler_objects.client`

`requests` and `httpx` have no `Expect: 100-continue` handshake, so an upload helper built on either always transfers the body twice. The repository ships a lightweight client library, [`simpler_objects/client.py`](simpler_objects/client.py), that handles the handshake, the `307` redirect, `Content-Length`, and SHA-256 verification on both ends:

```python
from simpler_objects.client import simple_upload, simple_download

# PUT a local file through the locator — body uploaded once, not twice
simple_upload("photo.jpg", "http://localhost:29164/mybucket/photo.jpg")

# GET it back — streamed to disk, SHA-256 verified against Repr-Digest
digest, mime, suggested_name, mtime = simple_download(
    "http://localhost:29164/mybucket/photo.jpg", "out.jpg")
```

`simple_upload` computes the file's SHA-256, sends it as `Content-Digest`, and checks it against the object server's `Repr-Digest` reply; `simple_download` verifies the downloaded bytes the same way. Both raise `simpler_objects.client.ClientError` on an HTTP error or a digest mismatch.

The library is synchronous and built on `pycurl`, which (with its system `libcurl`) must be installed. `pycurl` was chosen over `aiohttp` after a throughput bake-off — see [`upload-behavior-demo/`](upload-behavior-demo/) for the measurements and the investigation notes behind this guidance.

### Async clients: `aiohttp`

`simpler_objects.client` is synchronous — `pycurl` has no asyncio integration. Code that needs an async client should use `aiohttp` directly with `expect100=True`. It is pure Python and async-native, and still wraps cleanly in a synchronous helper:

```python
import asyncio
import base64
import aiohttp

def simple_upload(filename, url, file_mime, checksum_val=None):
    """PUT a file to a locator URL; body uploaded once via Expect: 100-continue."""
    headers = {'Content-Type': file_mime}
    if checksum_val:  # raw 32-byte SHA-256 digest
        headers['Content-Digest'] = (
            f"sha-256=:{base64.b64encode(checksum_val).decode()}:")

    async def _put():
        async with aiohttp.ClientSession() as session:
            with open(filename, 'rb') as f:
                async with session.put(url, data=f, headers=headers,
                                       expect100=True) as response:
                    response.raise_for_status()

    asyncio.run(_put())
```

`expect100=True` makes aiohttp wait for `100 Continue` before sending the body; the locator replies `307` instead, so the body skips the locator entirely. Verified: only ~240 bytes (headers) reach the locator.

- Do not call `asyncio.run()` from code already inside an event loop — there, make the helper `async def` and `await` the upload directly.
- Uploading many files? Create one `ClientSession` and reuse it instead of one per call.
- `aiohttp` uploads are slower than `pycurl` for large files (see the throughput bake-off in [`upload-behavior-demo/`](upload-behavior-demo/)); the trade-off buys native asyncio support.

### `Content-Length` on PUT

`Content-Length` is required on PUT. The locator uses it to select a server with sufficient free space; without it the request is rejected with `411 Length Required`. The object server uses it to verify the upload was received intact. Always send `Content-Length` — use `aiohttp` with `expect100=True` (see above) to ensure it is sent correctly when uploading through the locator.

### Digest headers on PUT

Clients may send `Content-Digest` or `Repr-Digest` with a SHA-256 value (`sha-256=:base64:` format) for integrity verification. The object server returns `400` on mismatch. The `Repr-Digest` header on GET/HEAD responses is present only when a checksum record exists for the object — do not assume it is always included.

### Changes in spec v0.2.0

- **`HEAD /{bucket}/` on the locator** is now supported (previously returned `405`). Returns `200` if the bucket exists on any server, `404` if none have it, `503` for server errors.
- **Directory entries in `GET /{bucket}/`** now return `"size": null` instead of `"size": 0`. Use the `"directory": true` field to identify directories rather than testing `size == 0`.

### Changes in spec v0.4.0

- **`Content-Length` is now required on `PUT /{bucket}/{key}`** (locator only). Previously the locator assumed 1 GiB when `Content-Length` was absent; it now returns `411 Length Required`. The object server already required it.

### Changes in spec v0.3.0

- **`/health` response renamed `available` to `quota-available-bytes`** (RFC 4331 alignment) and added a `quota-used-bytes` field. Clients reading the old `available` key will break and must switch to `quota-available-bytes`. This applies to the object server's `/health` body and to each per-server entry under `servers` in the locator's `/health` body.
- **`GET /`** now returns `403` on both the object server and the locator (previously `404`, as no route existed). Bucket enumeration is intentionally not offered.

## CORS

CORS headers are not configured by default. Web browsers making cross-origin requests will be blocked. To enable browser access from a different origin, add FastAPI's `CORSMiddleware` or place the service behind a reverse proxy that adds the appropriate `Access-Control-Allow-*` headers. Note that `Repr-Digest` is a non-standard response header and would need to be explicitly listed in `Access-Control-Expose-Headers` for JavaScript clients to read it.

## ObjectIndex

Optionally on ports 46569 (API) and 46567 (GUI).  Will be lowered/changed in future.
