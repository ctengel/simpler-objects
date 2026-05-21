# Upload behavior: the locator's 2x-upload problem

This directory records an investigation into how HTTP clients upload through
`locator_api`, and the demo scripts that prove the behavior. The client-facing
conclusions live in the main [README](../README.md) under *Client guidance*
(`Expect: 100-continue on PUT` and `Python clients: aiohttp instead of
requests`); this document is the reasoning, the measurements, and the
trial-and-error behind them.

## TL;DR

- The locator answers `PUT /{bucket}/{key}` with a `307` redirect and **never
  reads the request body** — it only reads the `Content-Length` header to pick
  an object server.
- A client that streams the whole body before reading the response uploads the
  object **twice**: once to the locator (wasted), then again to the object
  server (where it is actually stored).
- `Expect: 100-continue` prevents the waste. **`requests` and `httpx` do not
  support it → 2x. `aiohttp` supports it via `expect100=True` → 1x.**

## Why it happens

`PUT` to the locator runs `add_object()`, which reads only the `Content-Length`
header, picks an object server, and returns `307` with a `Location`. It never
touches the body. The client must then follow the redirect and re-PUT to the
object server, which *does* read and store the body.

The trap: a client commits to sending a body before it learns it will be
redirected. Unless it pauses for the server's go-ahead, the body is streamed to
the locator (and discarded) and then again to the object server.

`Expect: 100-continue` is the fix: the client sends headers first and waits for
a `100 Continue` before sending the body. The locator replies `307` *instead
of* `100`, so a client that honors the handshake never sends the body to the
locator.

## What we measured (8 MiB file)

| Client | Body reaching the locator | Result |
|---|---|---|
| `requests` | full 8 MiB | **2x** |
| `httpx` (default) | full 8 MiB | **2x** |
| `httpx` + manual `Expect:` header | full 8 MiB | **2x** (header ignored) |
| `curl`, body < 1 MiB | full body | 2x |
| `curl`, body ≥ 1 MiB | headers only | 1x (curl auto-sends `Expect`) |
| `aiohttp` (default) | ~1.8 MiB (timing-dependent) | partial — aborts mid-stream |
| `aiohttp`, `expect100=True` | ~240 bytes (headers only) | **1x** |

`requests` and `httpx` have no `Expect: 100-continue` handshake — adding the
header by hand does nothing, because they never wait for the `100`. `aiohttp`
without `expect100` is better than nothing (it interleaves reading the response
with writing the body, so it aborts partway when the `307` arrives) but still
wastes whatever was already in flight. Only `aiohttp` with `expect100=True` is
clean.

## Trial and error — measurement pitfalls

Measuring this correctly took several wrong turns. They are recorded here
because the obvious measurements are misleading.

1. **`file.tell()` after the PUT cannot measure this.** The obvious test —
   read `file.tell()` after `requests.put(...)` and treat it as the bytes
   uploaded — does not work. `requests` rewinds seekable bodies after a request
   (so they can be replayed on a redirect), so the position always reads back
   `0` no matter how much was sent. We were briefly fooled into concluding "no
   body sent"; an isolated test then showed the locator leg actually read all
   8,388,608 bytes while `tell()` still reported `0`. Use one of the methods
   below instead.

2. **A duck-typed file wrapper breaks the transfer.** A plain class exposing
   `read`/`seek`/`tell` (not an `io` subclass) made `requests` hang and the
   object server raise `ClientDisconnect` on the redirect — `requests`/`urllib3`
   treat genuine `io` objects differently from arbitrary look-alikes.

3. **A real `io.BufferedReader` subclass works.** Counting every `.read()` on
   such a subclass — the count survives the redirect rewind — gave the honest
   total: 16,777,216 bytes = **2.00x**. This is what `requests_double_upload_demo.py`
   does.

4. **A TCP proxy is the ground truth.** A transparent, byte-counting proxy in
   front of the locator does not depend on the file object at all. It confirmed
   8,388,843 bytes (8 MiB body + headers) reach the locator — independent of
   `tell()`, `sendfile`, or wrapper quirks. This is what `test_expect100.py`
   uses.

**Lesson:** to measure client upload volume, count `.read()` on a real `io`
subclass, or — better — put a TCP proxy in the path. Never trust `file.tell()`.

## Reproducing the demos

### Prerequisites

From the repo root:

```sh
# test file + bucket directory (paths are hardcoded in the scripts)
mkdir -p /tmp/so-demo/objects/mybucket
head -c 8388608 /dev/urandom > /tmp/so-demo/file8m.bin

# client libraries
pip install requests aiohttp httpx

# terminal 1 — object server
OBJECT_DIRECTORY=/tmp/so-demo/objects fastapi dev --port 29171 simpler_objects/object_server.py

# terminal 2 — locator
OBJECT_SERVERS="http://localhost:29171/" fastapi dev --port 29164 simpler_objects/locator_api.py
```

Objects are immutable, so re-running a demo against an existing key returns
`409`. Reset the bucket between runs:

```sh
rm -rf /tmp/so-demo/objects/mybucket && mkdir -p /tmp/so-demo/objects/mybucket
```

Both scripts hardcode `/tmp/so-demo/file8m.bin` and the locator at
`localhost:29164` — edit the constants at the top of each file if your setup
differs.

### Demo 1 — `requests` uploads twice

```sh
python upload-behavior-demo/requests_double_upload_demo.py
```

Expected output — `requests` reads (and therefore uploads) the file twice:

```
file size                   : 8,388,608 bytes
redirect chain              : 307 -> 201
total bytes read by requests: 16,777,216 bytes
upload multiplier           : 2.00x the file
```

### Demo 2 — `aiohttp` works, `httpx` does not

```sh
python upload-behavior-demo/test_expect100.py
```

Expected output — a TCP proxy counts bytes reaching the locator for four client
configurations:

```
aiohttp  (expect100=True)
    bytes client -> locator  : 239  (0.00x file)
    => 1x  -- body skipped the locator (clean)

aiohttp  (default, no expect100)
    bytes client -> locator  : ~1,800,000  (~0.2x file)
    => ~1x  -- partial: ... reached the locator before the client noticed the 307

httpx    (default)
    bytes client -> locator  : 8,388,795  (1.00x file)
    => 2x  -- whole body uploaded to the locator AND object server

httpx    (manual 'Expect: 100-continue' header)
    bytes client -> locator  : 8,388,817  (1.00x file)
    => 2x  -- whole body uploaded to the locator AND object server
```

The aiohttp-default figure is timing-dependent — it is whatever was in flight
when the client noticed the `307`.

## File index

| File | Demonstrates |
|---|---|
| `requests_double_upload_demo.py` | `requests` uploads the body 2x. Counts bytes via an `io.BufferedReader` subclass. |
| `test_expect100.py` | `aiohttp` (`expect100=True`) uploads 1x; `httpx` uploads 2x. Counts bytes with a transparent TCP proxy. |
