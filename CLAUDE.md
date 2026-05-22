# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

There is no `pyproject.toml`/`setup.py`. The package runs in place; dependencies (`fastapi`, `uvicorn`, `httpx`, `pycurl`, `pytest`) must already be installed in the environment. `pycurl` also needs the system `libcurl`. The demo scripts in `upload-behavior-demo/` additionally require `aiohttp` and `requests`.

```bash
# Run an object server (storage node) — one per filesystem/drive
OBJECT_DIRECTORY=/path/to/objects fastapi dev --port 29171 simpler_objects/object_server.py

# Run the locator (cluster coordinator) — pointed at one or more object servers
OBJECT_SERVERS="http://localhost:29171/,http://localhost:29172/" fastapi dev --port 29164 simpler_objects/locator_api.py

# Production: swap `fastapi dev` for `fastapi run`

# Run replication once (CLI tool, run periodically e.g. via cron)
python -m simpler_objects.async_replicate http://localhost:29164/ <bucket> <replica-count>

# Tests
python -m pytest                                  # all tests
python -m pytest tests/test_object_server.py::test_put   # single test
```

`OBJECT_SERVERS` URLs are comma-separated and **must** keep their trailing slash — code concatenates paths directly onto them.

## Architecture

Two-tier distributed object store. The big picture spans `simpler_objects/`:

- **`object_server.py`** — a storage node. A "bucket" is a directory under `OBJECT_DIRECTORY`; an "object" is a file inside it. Pure filesystem state, no database. Handles `GET/HEAD/PUT /{bucket}/{key}` and `GET/HEAD /{bucket}/`.
- **`locator_api.py`** — the cluster coordinator. It is **stateless and never proxies object data**: every `GET/HEAD/PUT /{bucket}/{key}` returns a `307` redirect so the client talks directly to an object server. The only configured state is the `OBJECT_SERVERS` env var.
- **`async_replicate.py`** — out-of-band replication CLI. Replication is *not* part of the request path; this tool queries the locator's `/health` and bucket listing, then copies objects server-to-server until each reaches the desired replica count.
- **`client.py`** — a lightweight, synchronous upload/download client library (`simple_upload`/`simple_download`), built on `pycurl`. Sends `Expect: 100-continue` so a PUT through the locator transfers the body once, and verifies SHA-256 on both ends. Not used by the servers — it is for external callers (e.g. objectindex). `pycurl` was chosen over `aiohttp` via the throughput bake-off in `upload-behavior-demo/bench_throughput.py`.

### Key cross-cutting concepts

- **On-disk format is a stable contract.** The object server's entire state is plain files — bucket directories, object files, and sibling `<bucket>.sha256` checksum files; there is no database. On-disk-format simplicity is a design goal: change the format only when genuinely necessary, and keep every change backwards-compatible — existing buckets and checksum files must keep working without migration.
- **Checksums / `Repr-Digest`.** Each bucket has a *sibling* file `<bucket>.sha256` (e.g. `mybucket.sha256` next to the `mybucket/` dir) in standard `sha256sum` format (`<hex>  <name>` lines). The object server appends to it via a single durable `O_APPEND` write at the end of a successful PUT (`append_checksum`) — that append is the **commit marker**: an object only counts as complete once its checksum line exists. Readers (`read_checksum`, `list_directory`) skip any malformed/torn line. The format is intentionally compatible with `sha256sum -c`. There is no checksum record unless one was written — `Repr-Digest` is therefore conditional on GET/HEAD.
- **Redirects.** Clients must follow `307` and **preserve the HTTP method** (notably for PUT). The locator never sees object bytes. For PUT through the locator, clients must send `Expect: 100-continue` to avoid uploading the body twice (locator returns `307` instead of `100`, so a compliant client skips the body on the locator leg). `requests` and `httpx` do not implement this handshake and always double-upload; use `aiohttp` (`expect100=True`) or `pycurl` (sets `Expect: 100-continue` automatically for bodies ≥ 1 MiB). See `upload-behavior-demo/` for measured results.
- **Server selection (`locator_api.add_object`).** Candidate object servers are filtered by health (`write` true, has free space) and weighted by `quota-available-bytes * percent`; one is picked by weighted random choice. Both `locator_api.add_object` and `async_replicate.find_space` use `common.filter_write_candidates` for this logic.
- **Immutability, atomic writes & locking.** PUT writes the body **in place** to the object's final path. The file is opened with `O_CREAT | O_EXCL` (an existing key — or a racing same-key PUT that won the create — gets `409`) and held under an exclusive `flock` for the whole upload. A concurrent GET takes a *non-blocking* shared `flock`; if a PUT holds the file the GET returns `503` + `Retry-After` immediately instead of reading a partial object. Objects are never overwritten. Any non-crash failure (client disconnect, task cancellation, `Content-Length`/body or `Content-Digest`/`Repr-Digest` mismatch → `400`) unlinks the partial file. A hard crash (SIGKILL / power loss) can leave an orphan partial file at a key path — the post-crash scrub utility (issue #64) must be run before restarting the server to clear it.
- **Path traversal.** All filesystem paths go through `safe_path()` in `object_server.py`, which resolves and rejects anything escaping `OBJECT_DIRECTORY`. Use it for any new path construction.
- **Bucket enumeration is intentionally disabled** — `GET /` returns `403` on both servers.
- **`FileResponse` handles Range/MIME/Content-Length automatically.** Starlette's `FileResponse` (used by FastAPI) provides Range request support (`206 Partial Content`, `Content-Range`, `416`), MIME type detection via `mimetypes.guess_type()`, `Content-Length` from file stat, and `Accept-Ranges: bytes` — all without any code in `object_server.py`. Do not add manual handling for these.

### API contract

`openapi.yaml` is the hand-maintained canonical spec. The `openapi-*.json` files at the repo root are exported dumps. When changing endpoint behavior, update `openapi.yaml` to match. The README's "Client guidance" section and `review-v0.3.0.md` document breaking changes between spec versions — follow that convention for new breaking changes.
