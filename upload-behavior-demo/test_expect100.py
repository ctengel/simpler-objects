"""Verify Expect: 100-continue behaviour across Python HTTP clients, against locator_api.

A transparent TCP proxy fronts the locator and counts the bytes the client sends
toward it. The locator answers PUT with a 307; the client then contacts the
object server directly (bypassing the proxy). So the proxy's count ~= bytes
uploaded to the locator:

    ~0          -> body skipped the locator          -> 1x  (correct)
    ~file size  -> body was uploaded to the locator  -> 2x  (wasteful)
"""

import asyncio
import io
import os
import socket
import threading
import time

import aiohttp
import httpx
import pycurl
import requests

LOCATOR = ("127.0.0.1", 29164)
PROXY_PORT = 29166
FILE = "/tmp/so-demo/file8m.bin"
SIZE = os.path.getsize(FILE)

counts = {"up": 0}


async def _pump(src, dst, count):
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            if count:
                counts["up"] += len(chunk)
            dst.write(chunk)
            await dst.drain()
    except OSError:
        pass
    finally:
        if not dst.is_closing():
            dst.close()


async def _handle(client_reader, client_writer):
    up_reader, up_writer = await asyncio.open_connection(*LOCATOR)
    await asyncio.gather(
        _pump(client_reader, up_writer, count=True),
        _pump(up_reader, client_writer, count=False),
    )


def _serve():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    server = loop.run_until_complete(
        asyncio.start_server(_handle, "127.0.0.1", PROXY_PORT))
    loop.run_until_complete(server.serve_forever())


def _url(key):
    return f"http://localhost:{PROXY_PORT}/mybucket/{key}"


def _requests_put(key, expect_header):
    headers = {"Content-Length": str(SIZE)}
    if expect_header:
        headers["Expect"] = "100-continue"
    with open(FILE, "rb") as f:
        r = requests.put(_url(key), data=f, headers=headers, timeout=90)
    return r.status_code, [h.status_code for h in r.history]


async def _aiohttp_put(key, expect100):
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with open(FILE, "rb") as body:
            async with session.put(_url(key), data=body,
                                   expect100=expect100) as resp:
                return resp.status, [h.status for h in resp.history]


def _httpx_put(key, expect_header):
    headers = {"Expect": "100-continue"} if expect_header else {}
    with open(FILE, "rb") as f:
        content = f.read()
    r = httpx.put(_url(key), content=content, headers=headers,
                  follow_redirects=True, timeout=90)
    return r.status_code, [h.status_code for h in r.history]


def _pycurl_put(key, expect_mode):
    """expect_mode: 'auto' | 'explicit' | 'suppress'

    auto     — no HTTPHEADER override; libcurl adds Expect: 100-continue for bodies >= 1 MiB
    explicit — manually set Expect: 100-continue header
    suppress — set Expect: (empty) to prevent libcurl's automatic header
    """
    statuses = []

    def _header_fn(line):
        # Capture HTTP status codes from all responses (including intermediates).
        decoded = line.decode('iso-8859-1').rstrip()
        if decoded.upper().startswith('HTTP/'):
            try:
                statuses.append(int(decoded.split()[1]))
            except (IndexError, ValueError):
                pass

    c = pycurl.Curl()
    c.setopt(pycurl.URL, _url(key))
    c.setopt(pycurl.UPLOAD, 1)
    c.setopt(pycurl.FOLLOWLOCATION, 1)
    if expect_mode == 'explicit':
        c.setopt(pycurl.HTTPHEADER, ['Expect: 100-continue'])
    elif expect_mode == 'suppress':
        # Empty value overrides libcurl's automatic Expect: 100-continue.
        c.setopt(pycurl.HTTPHEADER, ['Expect:'])
    # 'auto': libcurl adds Expect: 100-continue automatically for bodies >= 1 MiB.
    c.setopt(pycurl.HEADERFUNCTION, _header_fn)
    c.setopt(pycurl.WRITEDATA, io.BytesIO())   # discard response body
    with open(FILE, 'rb') as f:
        c.setopt(pycurl.READDATA, f)
        c.setopt(pycurl.INFILESIZE, SIZE)
        c.perform()
    c.close()

    redirects = [s for s in statuses if 300 <= s < 400]
    final = statuses[-1] if statuses else 0
    return final, redirects


def run(label, fn):
    counts["up"] = 0
    try:
        status, history = fn()
    except Exception as exc:  # noqa: BLE001 - report, don't abort the matrix
        status, history = f"ERROR {type(exc).__name__}: {exc}", []
    time.sleep(0.4)  # let the proxy drain any final buffered bytes
    up = counts["up"]
    if up < SIZE * 0.05:
        verdict = "1x  -- body skipped the locator (clean)"
    elif up < SIZE * 0.5:
        verdict = (f"~1x  -- partial: {up:,} bytes reached the locator before "
                   "the client noticed the 307 and stopped")
    else:
        verdict = "2x  -- whole body uploaded to the locator AND object server"
    print(label)
    print(f"    redirects + final status : {history} -> {status}")
    print(f"    bytes client -> locator  : {up:,}  ({up / SIZE:.2f}x file)")
    print(f"    => {verdict}\n")


def main():
    threading.Thread(target=_serve, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", PROXY_PORT), 0.1).close()
            break
        except OSError:
            time.sleep(0.05)

    print(f"file size: {SIZE:,} bytes\n")
    run("requests (default)",
        lambda: _requests_put("rq_def", expect_header=False))
    run("requests (manual 'Expect: 100-continue' header — ignored)",
        lambda: _requests_put("rq_hdr", expect_header=True))
    run("aiohttp  (expect100=True)",
        lambda: asyncio.run(_aiohttp_put("ai_e100", expect100=True)))
    run("aiohttp  (default, no expect100)",
        lambda: asyncio.run(_aiohttp_put("ai_def", expect100=False)))
    run("httpx    (default)",
        lambda: _httpx_put("hx_def", expect_header=False))
    run("httpx    (manual 'Expect: 100-continue' header)",
        lambda: _httpx_put("hx_hdr", expect_header=True))
    run("pycurl   (default — libcurl auto-adds Expect for bodies >= 1 MiB)",
        lambda: _pycurl_put("pc_def", expect_mode='auto'))
    run("pycurl   (explicit 'Expect: 100-continue')",
        lambda: _pycurl_put("pc_e100", expect_mode='explicit'))
    run("pycurl   (Expect: suppressed — confirms 2x without handshake)",
        lambda: _pycurl_put("pc_noe", expect_mode='suppress'))


if __name__ == "__main__":
    main()
