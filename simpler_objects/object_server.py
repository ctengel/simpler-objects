"""Simpler Objects Server"""

import asyncio
import errno
import logging
import pathlib
import shutil
import base64
import hashlib
import fcntl
import os
from typing import Annotated
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import FileResponse, Response

from simpler_objects.logging_config import configure, install_request_id_middleware

configure()
logger = logging.getLogger(__name__)

app = FastAPI()
install_request_id_middleware(app)

OBJECT_DIRECTORY = os.environ.get('OBJECT_DIRECTORY', '.')
READ_ONLY = bool(os.environ.get('READ_ONLY', ''))
BUFFER = 67108864
RETRY_AFTER = "64"

def checksum_filename(bucket: pathlib.Path):
    """Determine a bucket checksum file"""
    return bucket.parent.joinpath(bucket.name).with_suffix('.sha256')

def safe_path(base: pathlib.Path, *parts) -> pathlib.Path:
    """Resolve path and reject traversal outside base."""
    resolved_base = base.resolve()
    candidate = base.joinpath(*parts).resolve()
    if not candidate.is_relative_to(resolved_base):
        logger.warning("path.traversal", extra={
            "base": str(resolved_base),
            "attempted": str(candidate),
            "parts": [str(p) for p in parts],
        })
        raise HTTPException(status_code=404)
    return candidate

def object_filename(bucket, key):
    """Get the Path of an object"""
    return safe_path(pathlib.Path(OBJECT_DIRECTORY), bucket, key)

def http_digest_head(file_digest: bytes) -> str:
    """Write an http digest header"""
    return f"sha-256=:{base64.b64encode(file_digest).decode()}:"

def parse_digest_header(value: str):
    """Get the SHA-256 checksum from a Content-Digest or Repr-Digest"""
    if not value:
        return None
    shavalue = [x.partition('=')[2]
                            for x in value.split(',')
                            if x.partition('=')[0] == 'sha-256']
    if not shavalue:
        return None
    return base64.b64decode(shavalue[0].strip(':'))

def parse_digest_headers(headers: dict):
    """Get one SHA-256 from multiple headers"""
    options = set(parse_digest_header(headers.get(x)) for x in ['Repr-Digest', 'Content-Digest'])
    options.discard(None)
    if len(options) > 1:
        raise HTTPException(status_code=400)
    if len(options) == 0:
        return None
    return options.pop()

def file_checksum(path):
    """Return SHA-256 of a file on disk"""
    hash_sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(BUFFER), b""):
            hash_sha256.update(chunk)
    return hash_sha256.digest()

def append_checksum(path: pathlib.Path, file_digest: bytes):
    """Durably append a sha256sum-format line for an object to its bucket checksum file.

    The single O_APPEND os.write() is atomic against concurrent appenders (POSIX),
    so different-key PUTs in the same bucket need no extra serialisation.
    """
    cksum_line = f"{file_digest.hex()}  {path.name}\n"
    fd = os.open(checksum_filename(path.parent),
                 os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, cksum_line.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    logger.debug("checksum.append", extra={
        "bucket": path.parent.name,
        "key": path.name,
        "sha256_hex": file_digest.hex(),
    })

def read_checksum(bucket_dir: pathlib.Path, key: str):
    """Return the recorded SHA-256 digest for a key, or None.

    Lines that do not parse cleanly (torn by a crash, or a torn fragment
    merged with the next append) are skipped rather than raising.
    """
    try:
        with open(checksum_filename(bucket_dir), encoding='utf-8') as fp:
            for line_no, line in enumerate(fp, start=1):
                parts = line.strip().split()
                # A valid sha256sum line has exactly two fields and a 64-char hex digest.
                # Fewer/more fields catches truly torn lines; the length check catches a
                # torn fragment that absorbed a later append (making one garbage field).
                if len(parts) != 2 or len(parts[0]) != 64:
                    logger.warning("checksum.torn", extra={
                        "bucket": bucket_dir.name,
                        "line_no": line_no,
                        "field_count": len(parts),
                        "digest_len": len(parts[0]) if parts else 0,
                    })
                    continue
                checksum, file_name = parts
                if file_name == key:
                    return bytes.fromhex(checksum)
    except FileNotFoundError:
        pass
    return None

@app.get('/health')
def healthcheck():
    """Return basic info on node health"""
    disk_stats = shutil.disk_usage(pathlib.Path(OBJECT_DIRECTORY))
    r = {'read': True,
         'write': not READ_ONLY,
         'quota-available-bytes': disk_stats.free,
         'quota-used-bytes': disk_stats.used,
         'percent': int(float(disk_stats.free)/float(disk_stats.total)*100.0)}
    return r

@app.api_route("/{bucket}/{key}", methods=['GET', 'HEAD'])
def get_object(bucket: str, key: str):
    """Handle GET requests.

    A sync route: every operation here is a blocking syscall with nothing to
    await, so FastAPI runs it in the worker threadpool and the event loop is
    never stalled. The returned FileResponse is still streamed asynchronously.
    """
    path = object_filename(bucket, key)
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        logger.info("object.get.missing", extra={
            "bucket": bucket, "key": key, "phase": "open",
        })
        raise HTTPException(status_code=404) from None
    try:
        # A shared lock, tested non-blocking: if a PUT holds the exclusive
        # lock the object is mid-upload — fail fast and retriable.
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.warning("object.get.busy", extra={
                "bucket": bucket, "key": key,
            })
            raise HTTPException(status_code=503,
                                headers={"Retry-After": RETRY_AFTER}) from None
        # Re-check: a PUT that failed may have unlinked the file between the
        # open above and acquiring the lock.
        if not path.is_file():
            logger.info("object.get.missing", extra={
                "bucket": bucket, "key": key, "phase": "post-lock",
            })
            raise HTTPException(status_code=404)
        my_cksum = read_checksum(path.parent, key)
    finally:
        os.close(fd)
    headers = None
    if my_cksum:
        headers = {"Repr-Digest": http_digest_head(my_cksum)}
    logger.info("object.get", extra={
        "bucket": bucket,
        "key": key,
        "size": path.stat().st_size,
        "sha256_hex": my_cksum.hex() if my_cksum else None,
    })
    return FileResponse(path, headers=headers)

@app.put("/{bucket}/{key}")
async def put_object(bucket: str, key: str, request: Request,
                     content_length: Annotated[int | None, Header()] = None):
    if READ_ONLY:
        raise HTTPException(status_code=405)

    path = object_filename(bucket, key)

    # O_EXCL creates the object atomically: an existing key — or a racing
    # same-key PUT that won the create — lands here as 409.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        logger.info("object.put.conflict", extra={"bucket": bucket, "key": key})
        raise HTTPException(status_code=409) from None
    except (FileNotFoundError, NotADirectoryError):
        logger.info("object.put.no_bucket", extra={"bucket": bucket, "key": key})
        raise HTTPException(status_code=404) from None

    try:
        # Hold an exclusive lock for the whole upload so a concurrent GET
        # fails fast with 503 rather than reading a partial file.
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            request_digest = parse_digest_headers(request.headers)
            with os.fdopen(fd, "wb", closefd=False) as dst:
                async for chunk in request.stream():
                    dst.write(chunk)
            # Offload the blocking commit steps (fsync, whole-file hash) so a
            # large upload does not stall the event loop for other requests.
            await asyncio.to_thread(os.fsync, fd)
            actual_size = os.fstat(fd).st_size
            if content_length is not None and actual_size != content_length:
                logger.warning("object.put.mismatch", extra={
                    "bucket": bucket, "key": key, "reason": "content_length",
                    "expected_size": content_length, "actual_size": actual_size,
                })
                raise HTTPException(status_code=400)
            file_digest = await asyncio.to_thread(file_checksum, path)
            if request_digest and file_digest != request_digest:
                logger.warning("object.put.mismatch", extra={
                    "bucket": bucket, "key": key, "reason": "digest",
                    "expected_sha256_hex": request_digest.hex(),
                    "actual_sha256_hex": file_digest.hex(),
                })
                raise HTTPException(status_code=400)
            append_checksum(path, file_digest)
            logger.info("object.put", extra={
                "bucket": bucket,
                "key": key,
                "size": actual_size,
                "sha256_hex": file_digest.hex(),
            })
            return Response(status_code=201, content=None,
                            headers={"Repr-Digest": http_digest_head(file_digest)})
        except OSError as e:
            path.unlink(missing_ok=True)
            if e.errno == errno.ENOSPC:
                logger.error("object.put.nospace", extra={
                    "bucket": bucket, "key": key, "errno": e.errno,
                })
                raise HTTPException(status_code=507) from None
            logger.error("object.put.crash", exc_info=True,
                         extra={"bucket": bucket, "key": key})
            raise
        except HTTPException:
            # Mismatch (400) already logged at WARNING above.
            path.unlink(missing_ok=True)
            raise
        except Exception:
            path.unlink(missing_ok=True)
            logger.error("object.put.crash", exc_info=True,
                         extra={"bucket": bucket, "key": key})
            raise
        except BaseException:
            # CancelledError / client disconnect: clean up quietly.
            path.unlink(missing_ok=True)
            raise
    finally:
        os.close(fd)

@app.get("/")
def list_buckets():
    """List buckets — not permitted"""
    raise HTTPException(status_code=403)

@app.head("/{bucket}/")
def head_bucket(bucket: str):
    dir_path = safe_path(pathlib.Path(OBJECT_DIRECTORY), bucket)
    if not dir_path.is_dir():
        raise HTTPException(status_code=404)
    return Response(status_code=200)


@app.get("/{bucket}/")
def list_directory(bucket: str):
    """List objects in bucket"""
    dir_path = safe_path(pathlib.Path(OBJECT_DIRECTORY), bucket)
    if not dir_path.is_dir():
        raise HTTPException(status_code=404)
    r = {"bucket": bucket,
         "objects": {}}
    hashes = {}
    try:
        with open(checksum_filename(dir_path), encoding='utf-8') as fp:
            for line_no, line in enumerate(fp, start=1):
                parts = line.strip().split()
                if len(parts) != 2:
                    logger.warning("checksum.torn", extra={
                        "bucket": bucket,
                        "line_no": line_no,
                        "field_count": len(parts),
                    })
                    continue
                checksum, file_name = parts
                hashes[file_name] = checksum
    except FileNotFoundError:
        pass
    for name in dir_path.iterdir():
        if name.is_dir():
            r['objects'][name.name] = {'directory': True,
                                        'size': None,
                                        'checksum': None}
        else:
            r['objects'][name.name] = {'directory': False,
                                        'size': name.stat().st_size,
                                        'checksum': hashes.get(name.name)}
    return r


#def run(server_class=DirHTTPServer, handler_class=PutHTTPRequestHandler,
#        port=46579, directory=None):
#    """Run this server"""
#    server_address = ('', port)
#    httpd = server_class(server_address, handler_class, directory=directory)
#    httpd.serve_forever()

#def cli():
#    """CLI"""
#    parser = argparse.ArgumentParser()
#    parser.add_argument("-d", "--directory")
#    parser.add_argument("port", default=46579)
#    args = parser.parse_args()
#    run(port=int(args.port), directory=args.directory)

#if __name__ == '__main__':
#    cli()
