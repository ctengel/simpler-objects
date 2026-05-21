"""Throughput bake-off: aiohttp vs pycurl, for upload and download via the locator.

The 2x-upload *correctness* bake-off lives in ``test_expect100.py`` (both clients
already pass it). This script measures raw wall-clock throughput on a large file
so issue #21 can pick a client library. Both candidates send
``Expect: 100-continue`` and follow the locator's 307, exactly as the real
client library will.

Prerequisites (see README.md):
  - object server on :29171, OBJECT_DIRECTORY set, bucket 'mybucket' created
  - locator on :29164 pointed at that object server
  - OBJECT_DIRECTORY exported here too, so uploaded objects can be cleaned up
    between runs (keeps peak disk use to ~3x the file size)

Usage:
  OBJECT_DIRECTORY=/path/objects BENCH_DIR=/path/work \\
      python upload-behavior-demo/bench_throughput.py [size_mib] [runs]
"""

import asyncio
import io
import os
import pathlib
import sys
import time
import uuid

import aiohttp
import pycurl

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from simpler_objects.client import BLOCK_SIZE  # noqa: E402

LOCATOR = "http://localhost:29164"
BUCKET = "mybucket"
BENCH_DIR = pathlib.Path(os.environ.get("BENCH_DIR", "/tmp/so-demo"))
OBJECT_DIRECTORY = os.environ.get("OBJECT_DIRECTORY")
MIB = 1024 * 1024

# pycurl winning by less than this margin is treated as a tie -> aiohttp wins
# (async-native per #21, pure Python). See plan / README.
TIE_MARGIN = 1.15


def _url(key):
    return f"{LOCATOR}/{BUCKET}/{key}"


def _make_test_file(path: pathlib.Path, size: int):
    """Write a file of exactly `size` real (non-sparse) bytes if missing."""
    if path.exists() and path.stat().st_size == size:
        return
    block = os.urandom(4 * MIB)
    with open(path, "wb") as handle:
        written = 0
        while written < size:
            chunk = block[: min(len(block), size - written)]
            handle.write(chunk)
            written += len(chunk)


def _cleanup_object(key: str):
    """Remove a stored object so peak server-side disk stays bounded."""
    if not OBJECT_DIRECTORY:
        return
    obj = pathlib.Path(OBJECT_DIRECTORY) / BUCKET / key
    obj.unlink(missing_ok=True)


# --- upload candidates ------------------------------------------------------

async def _aiohttp_upload(path, key, size):
    timeout = aiohttp.ClientTimeout(total=600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with open(path, "rb") as body:
            async with session.put(_url(key), data=body, expect100=True) as resp:
                resp.raise_for_status()


def _pycurl_upload(path, key, size):
    curl = pycurl.Curl()
    curl.setopt(pycurl.URL, _url(key))
    curl.setopt(pycurl.UPLOAD, 1)
    curl.setopt(pycurl.FOLLOWLOCATION, 1)
    curl.setopt(pycurl.HTTPHEADER, ["Expect: 100-continue"])
    curl.setopt(pycurl.WRITEDATA, io.BytesIO())
    with open(path, "rb") as body:
        curl.setopt(pycurl.READDATA, body)
        curl.setopt(pycurl.INFILESIZE, size)
        curl.perform()
    code = curl.getinfo(pycurl.RESPONSE_CODE)
    curl.close()
    if code >= 400:
        raise RuntimeError(f"pycurl upload returned HTTP {code}")


# --- download candidates ----------------------------------------------------

async def _aiohttp_download(key, dest):
    timeout = aiohttp.ClientTimeout(total=600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(_url(key)) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as out:
                async for chunk in resp.content.iter_chunked(BLOCK_SIZE):
                    out.write(chunk)


def _pycurl_download(key, dest):
    curl = pycurl.Curl()
    curl.setopt(pycurl.URL, _url(key))
    curl.setopt(pycurl.FOLLOWLOCATION, 1)
    with open(dest, "wb") as out:
        curl.setopt(pycurl.WRITEDATA, out)
        curl.perform()
    code = curl.getinfo(pycurl.RESPONSE_CODE)
    curl.close()
    if code >= 400:
        raise RuntimeError(f"pycurl download returned HTTP {code}")


# --- runner -----------------------------------------------------------------

def _bench(label, run_once, size, runs):
    """Time `run_once` `runs` times; return (mean, best) throughput in MiB/s."""
    rates = []
    for _ in range(runs):
        start = time.perf_counter()
        run_once()
        elapsed = time.perf_counter() - start
        rates.append((size / MIB) / elapsed)
    mean = sum(rates) / len(rates)
    best = max(rates)
    print(f"  {label:<18}: mean {mean:7.1f} MiB/s   best {best:7.1f} MiB/s")
    return mean, best


def main():
    size = int(sys.argv[1]) * MIB if len(sys.argv) > 1 else 512 * MIB
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    token = uuid.uuid4().hex[:8]

    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    test_file = BENCH_DIR / f"bench-{size // MIB}m.bin"
    print(f"file size: {size // MIB} MiB   runs: {runs}   token: {token}\n")
    _make_test_file(test_file, size)
    if not OBJECT_DIRECTORY:
        print("WARNING: OBJECT_DIRECTORY not set — uploaded objects will not be "
              "cleaned up; server-side disk use grows with each run.\n")

    # one persistent object to download from
    src_key = f"bench-src-{token}"
    asyncio.run(_aiohttp_upload(test_file, src_key, size))

    print("UPLOAD  (local file -> locator -> object server)")

    def _up(lib, run_fn):
        keys = []

        def _once():
            key = f"bench-{token}-{lib}-up-{uuid.uuid4().hex[:6]}"
            keys.append(key)
            run_fn(test_file, key, size)
            _cleanup_object(key)

        result = _bench(lib, _once, size, runs)
        return result

    up_aiohttp = _up("aiohttp", lambda p, k, s: asyncio.run(_aiohttp_upload(p, k, s)))
    up_pycurl = _up("pycurl", _pycurl_upload)

    print("\nDOWNLOAD  (object server -> locator -> local file)")

    def _down(lib, run_fn):
        def _once():
            dest = BENCH_DIR / f"dl-{token}-{lib}-{uuid.uuid4().hex[:6]}"
            try:
                run_fn(src_key, dest)
            finally:
                dest.unlink(missing_ok=True)

        return _bench(lib, _once, size, runs)

    down_aiohttp = _down("aiohttp", lambda k, d: asyncio.run(_aiohttp_download(k, d)))
    down_pycurl = _down("pycurl", _pycurl_download)

    _cleanup_object(src_key)

    # verdict — upload is the critical path (it carries the 2x-penalty fix)
    ratio = up_pycurl[0] / up_aiohttp[0]
    if ratio >= TIE_MARGIN:
        winner = "pycurl"
        why = (f"pycurl upload is {ratio:.2f}x aiohttp's — a clear win "
               f"(>= {TIE_MARGIN:.2f}x).")
    else:
        winner = "aiohttp"
        why = (f"pycurl upload is only {ratio:.2f}x aiohttp's — within the tie "
               f"margin ({TIE_MARGIN:.2f}x); aiohttp wins on #21's asyncio "
               "requirement and being pure Python.")
    print(f"\nVERDICT: {winner}\n  {why}")
    print(f"  upload   aiohttp {up_aiohttp[0]:.1f}  |  pycurl {up_pycurl[0]:.1f}  MiB/s (mean)")
    print(f"  download aiohttp {down_aiohttp[0]:.1f}  |  pycurl {down_pycurl[0]:.1f}  MiB/s (mean)")


if __name__ == "__main__":
    main()
