"""Simpler Objects client library.

Lightweight, synchronous upload/download helpers for a Simpler Objects locator
(or an object server directly), built on pycurl. Uploads send
``Expect: 100-continue`` so the body is never transferred to the locator and
then again to the object server (the 2x penalty in issue #26). pycurl was
chosen over aiohttp after the throughput bake-off in ``upload-behavior-demo/``.
"""

import base64
import datetime
import hashlib
import io
import logging
import mimetypes
import pathlib
import uuid

import pycurl

BLOCK_SIZE = 16 * 1024 * 1024  # 16 MiB streaming chunk
_DOWNLOAD_BUFFER = 256 * 1024  # libcurl receive buffer size for downloads

# Library logger — silent unless the embedding application configures handlers.
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class ClientError(Exception):
    """An HTTP failure or an integrity-check failure from a client call."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# --- header / digest helpers ------------------------------------------------

def encode_digest_header(digest: bytes) -> str:
    """Encode a raw SHA-256 digest as an RFC 9530 ``sha-256=:base64:`` value."""
    return f"sha-256=:{base64.b64encode(digest).decode()}:"


def parse_digest_header(value: str | None) -> bytes | None:
    """Extract the raw SHA-256 digest from a Content-Digest/Repr-Digest value."""
    if not value:
        return None
    for pair in value.split(','):
        algo, _, digest = pair.partition('=')
        if algo.strip().lower() != 'sha-256':
            continue
        return base64.b64decode(digest.strip(': '))
    return None


def read_content_disposition(value: str | None) -> str | None:
    """Read the filename from a Content-Disposition header value."""
    if not value or 'filename=' not in value:
        return None
    start = value.find('filename=') + len('filename=')
    return value[start:].strip('";')


def read_http_datetime(value: str | None) -> datetime.datetime | None:
    """Parse an HTTP date (e.g. Last-Modified) into a datetime."""
    if not value:
        return None
    return datetime.datetime.strptime(value, '%a, %d %b %Y %X %Z')


def file_checksum(path) -> bytes:
    """Return the raw SHA-256 digest of a file, read in chunks."""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(BLOCK_SIZE), b''):
            digest.update(chunk)
    return digest.digest()


# --- internal ---------------------------------------------------------------

def _header_collector(store: dict):
    """Build a pycurl HEADERFUNCTION recording only the final response's headers.

    With FOLLOWLOCATION the callback also sees the locator's 307; clearing on
    each status line drops those so `store` reflects the object server's reply.
    """
    def _on_header(line: bytes):
        decoded = line.decode('iso-8859-1').rstrip()
        if decoded.upper().startswith('HTTP/'):
            store.clear()
            return
        name, sep, value = decoded.partition(':')
        if sep:
            store[name.strip().lower()] = value.strip()
    return _on_header


# --- public API -------------------------------------------------------------

def simple_upload(filename, url, file_mime=None, checksum_val=None) -> bytes:
    """PUT a local file to a Simpler Objects locator (or object server) URL.

    The body is uploaded once: ``Expect: 100-continue`` lets the locator answer
    307 before any body is sent. The file's SHA-256 is sent as Content-Digest
    and verified against the object server's Repr-Digest reply. Returns the
    raw SHA-256 digest. Raises ClientError on HTTP failure or digest mismatch.
    """
    path = pathlib.Path(filename)
    size = path.stat().st_size
    if file_mime is None:
        file_mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    if checksum_val is None:
        checksum_val = file_checksum(path)

    request_id = uuid.uuid4().hex
    logger.debug("client.upload.start", extra={
        "url": url, "file_path": str(path), "file_size": size,
        "sha256_hex": checksum_val.hex(), "client_request_id": request_id,
    })
    response_headers: dict = {}
    curl = pycurl.Curl()
    curl.setopt(pycurl.URL, url)
    curl.setopt(pycurl.UPLOAD, 1)
    curl.setopt(pycurl.FOLLOWLOCATION, 1)
    curl.setopt(pycurl.INFILESIZE_LARGE, size)
    curl.setopt(pycurl.HTTPHEADER, [
        'Expect: 100-continue',
        f'Content-Type: {file_mime}',
        f'Content-Digest: {encode_digest_header(checksum_val)}',
        f'X-Request-Id: {request_id}',
    ])
    curl.setopt(pycurl.HEADERFUNCTION, _header_collector(response_headers))
    curl.setopt(pycurl.WRITEDATA, io.BytesIO())  # discard the empty 201 body
    try:
        with open(path, 'rb') as body:
            curl.setopt(pycurl.READDATA, body)
            curl.perform()
        code = curl.getinfo(pycurl.RESPONSE_CODE)
        redirects = curl.getinfo(pycurl.REDIRECT_COUNT)
        total_time = curl.getinfo(pycurl.TOTAL_TIME)
    except pycurl.error as exc:
        raise ClientError(f"upload of {url} failed: {exc}") from exc
    finally:
        curl.close()

    if code >= 400:
        raise ClientError(f"upload of {url} failed: HTTP {code}", status=code)
    server_digest = parse_digest_header(response_headers.get('repr-digest'))
    if server_digest is not None and server_digest != checksum_val:
        logger.warning("client.upload.digest_mismatch", extra={
            "url": url, "client_request_id": request_id,
            "expected_sha256_hex": checksum_val.hex(),
            "server_sha256_hex": server_digest.hex(),
        })
        raise ClientError(f"digest mismatch after upload of {url}")
    logger.info("client.upload.done", extra={
        "url": url, "file_size": size, "sha256_hex": checksum_val.hex(),
        "status": code, "redirects": redirects,
        "total_time_ms": round(total_time * 1000.0, 2),
        "client_request_id": request_id,
    })
    return checksum_val


def simple_download(url, filename):
    """GET an object to a local file.

    Streams to disk while computing the SHA-256, and verifies it against the
    object server's Repr-Digest reply (raising ClientError on mismatch). Note
    the server returns the digest as ``Repr-Digest``, not ``Content-Digest``.
    Returns ``(digest, mime, sugg_fname, mtime)``.
    """
    path = pathlib.Path(filename)
    request_id = uuid.uuid4().hex
    logger.debug("client.download.start", extra={
        "url": url, "file_path": str(path), "client_request_id": request_id,
    })
    response_headers: dict = {}
    digest = hashlib.sha256()
    curl = pycurl.Curl()
    curl.setopt(pycurl.URL, url)
    curl.setopt(pycurl.FOLLOWLOCATION, 1)
    curl.setopt(pycurl.BUFFERSIZE, _DOWNLOAD_BUFFER)
    curl.setopt(pycurl.HTTPHEADER, [
        'Want-Content-Digest: sha-256=9',
        f'X-Request-Id: {request_id}',
    ])
    curl.setopt(pycurl.HEADERFUNCTION, _header_collector(response_headers))
    try:
        with open(path, 'wb') as out:
            def _write(chunk: bytes):
                digest.update(chunk)
                out.write(chunk)
            curl.setopt(pycurl.WRITEFUNCTION, _write)
            curl.perform()
        code = curl.getinfo(pycurl.RESPONSE_CODE)
        redirects = curl.getinfo(pycurl.REDIRECT_COUNT)
        total_time = curl.getinfo(pycurl.TOTAL_TIME)
    except pycurl.error as exc:
        path.unlink(missing_ok=True)
        raise ClientError(f"download of {url} failed: {exc}") from exc
    finally:
        curl.close()

    if code >= 400:
        path.unlink(missing_ok=True)
        raise ClientError(f"download of {url} failed: HTTP {code}", status=code)

    file_digest = digest.digest()
    server_digest = parse_digest_header(response_headers.get('repr-digest'))
    if server_digest is not None and server_digest != file_digest:
        path.unlink(missing_ok=True)
        logger.warning("client.download.digest_mismatch", extra={
            "url": url, "client_request_id": request_id,
            "computed_sha256_hex": file_digest.hex(),
            "server_sha256_hex": server_digest.hex(),
        })
        raise ClientError(f"digest mismatch after download of {url}")

    logger.info("client.download.done", extra={
        "url": url, "file_size": path.stat().st_size,
        "sha256_hex": file_digest.hex(),
        "status": code, "redirects": redirects,
        "total_time_ms": round(total_time * 1000.0, 2),
        "client_request_id": request_id,
    })
    return (file_digest,
            response_headers.get('content-type'),
            read_content_disposition(response_headers.get('content-disposition')),
            read_http_datetime(response_headers.get('last-modified')))
