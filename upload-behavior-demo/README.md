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
| `pycurl` (default, body ≥ 1 MiB) | ~290 bytes (headers only) | **1x** (libcurl auto-sends `Expect`) |
| `pycurl`, explicit `Expect: 100-continue` | ~290 bytes (headers only) | **1x** |
| `pycurl`, `Expect:` suppressed | ~4.8 MiB, then **error** | broken — `CURLE_SEND_FAIL_REWIND` |

`requests` and `httpx` have no `Expect: 100-continue` handshake — adding the
header by hand does nothing, because they never wait for the `100`. `aiohttp`
without `expect100` is better than nothing (it interleaves reading the response
with writing the body, so it aborts partway when the `307` arrives) but still
wastes whatever was already in flight. `aiohttp` with `expect100=True` and
`pycurl` (with `Expect: 100-continue` present) are both clean.

`pycurl` without `Expect: 100-continue` fails more severely than `requests`/`httpx`:
libcurl streams ~half the body to the locator before the `307` arrives, then tries
to rewind the file handle to replay the body on the redirect — and fails with
`CURLE_SEND_FAIL_REWIND` (error 65) because no `CURLOPT_SEEKFUNCTION` was registered.
The upload is incomplete and the object is never stored. For bodies < 1 MiB, where
libcurl does not auto-add `Expect: 100-continue`, the explicit header is required.

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
   total: 16,777,216 bytes = **2.00x**. This was the technique used before the
   TCP proxy approach was developed.

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
pip install requests aiohttp httpx pycurl

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

The script hardcodes `/tmp/so-demo/file8m.bin` and the locator at
`localhost:29164` — edit the constants at the top if your setup differs.

### Running the demo

```sh
python upload-behavior-demo/test_expect100.py
```

Expected output — a TCP proxy counts bytes reaching the locator for each client:

```
requests (default)
    bytes client -> locator  : 8,388,804  (1.00x file)
    => 2x  -- whole body uploaded to the locator AND object server

requests (manual 'Expect: 100-continue' header — ignored)
    bytes client -> locator  : 8,388,826  (1.00x file)
    => 2x  -- whole body uploaded to the locator AND object server

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

pycurl   (default — libcurl auto-adds Expect for bodies >= 1 MiB)
    bytes client -> locator  : 290  (0.00x file)
    => 1x  -- body skipped the locator (clean)

pycurl   (explicit 'Expect: 100-continue')
    bytes client -> locator  : 291  (0.00x file)
    => 1x  -- body skipped the locator (clean)

pycurl   (Expect: suppressed — confirms 2x without handshake)
    redirects + final status : [] -> ERROR error: (65, 'necessary data rewind was not possible')
    bytes client -> locator  : 4,784,128  (0.57x file)
    => 2x  -- whole body uploaded to the locator AND object server
```

The aiohttp-default figure is timing-dependent — it is whatever was in flight
when the client noticed the `307`. The pycurl-suppressed case errors with
`CURLE_SEND_FAIL_REWIND` (error 65): after streaming ~half the body to the locator
and receiving the `307`, libcurl cannot replay the body on the redirect without a
registered `CURLOPT_SEEKFUNCTION`. The upload fails entirely.

## File index

| File | Demonstrates |
|---|---|
| `test_expect100.py` | Full client matrix: `requests` and `httpx` upload 2x; `aiohttp` (`expect100=True`) and `pycurl` upload 1x; `pycurl` with `Expect:` suppressed errors. Counts bytes with a transparent TCP proxy. |
