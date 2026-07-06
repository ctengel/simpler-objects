"""Simpler Objects Server"""

import asyncio
import errno
import pathlib
import shutil
import base64
import hashlib
import fcntl
import os
from typing import Annotated
from fastapi import Depends, FastAPI, HTTPException, Header, Request
from fastapi.responses import FileResponse, Response
from simpler_objects import auth
from simpler_objects.common import check_content_type_extension

from simpler_objects.common import ChecksumFile

app = FastAPI()

OBJECT_DIRECTORY = os.environ.get('OBJECT_DIRECTORY', '.')
READ_ONLY = bool(os.environ.get('READ_ONLY', ''))
# Shared HMAC secret for signed-URL verification; unset = no enforcement.
CLUSTER_SECRET = os.environ.get('CLUSTER_SECRET', '')
BUFFER = 67108864
RETRY_AFTER = "64"


def require_signature(operation):
    """Dependency factory enforcing a valid signed URL for one operation.

    Reads bucket/key from the matched path params rather than declaring them
    as function parameters, so the same dependency serves both object and
    bucket-level routes. Runs before any filesystem access and — critically
    for PUT — before the request body is consumed, so a rejected upload
    transfers no data (and a 100-continue client never sends the body).
    """
    def dependency(request: Request):
        if not CLUSTER_SECRET:
            return
        exp = request.query_params.get('exp')
        sig = request.query_params.get('sig')
        if exp is None or sig is None:
            raise HTTPException(status_code=401)
        bucket = request.path_params['bucket']
        key = request.path_params.get('key', '')
        if not auth.verify(CLUSTER_SECRET, operation, bucket, key, exp, sig):
            raise HTTPException(status_code=403)
    return dependency


def safe_path(*parts) -> pathlib.Path:
    """Resolve path and reject traversal outside OBJECT_DIRECTORY."""
    base = pathlib.Path(OBJECT_DIRECTORY)
    if not base.is_dir():
        # the base object directory cannot be reached
        raise HTTPException(status_code=500)
    resolved_base = base.resolve()
    candidate = base.joinpath(*parts).resolve()
    if not candidate.is_relative_to(resolved_base):
        # someone is doing something tricky
        raise HTTPException(status_code=404)
    return candidate

def object_filename(bucket, key):
    """Get the Path of an object"""
    return safe_path(bucket, key)

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


@app.get('/health')
def healthcheck():
    """Return basic info on node health"""
    try:
        disk_stats = shutil.disk_usage(pathlib.Path(OBJECT_DIRECTORY))
    except FileNotFoundError:
        raise HTTPException(status_code=500)
    r = {'read': True,
         'write': not READ_ONLY,
         'quota-available-bytes': disk_stats.free,
         'quota-used-bytes': disk_stats.used,
         'percent': int(float(disk_stats.free)/float(disk_stats.total)*100.0)}
    return r

@app.api_route("/{bucket}/{key}", methods=['GET', 'HEAD'],
               dependencies=[Depends(require_signature(auth.OP_READ))])
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
        raise HTTPException(status_code=404) from None
    try:
        # A shared lock, tested non-blocking: if a PUT holds the exclusive
        # lock the object is mid-upload — fail fast and retriable.
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(status_code=503,
                                headers={"Retry-After": RETRY_AFTER}) from None
        # Re-check: a PUT that failed may have unlinked the file between the
        # open above and acquiring the lock.
        if not path.is_file():
            raise HTTPException(status_code=404)
        my_cksum = ChecksumFile(path.parent).lookup(key)
    finally:
        os.close(fd)
    headers = None
    if my_cksum:
        headers = {"Repr-Digest": http_digest_head(my_cksum)}
    return FileResponse(path, headers=headers)

@app.put("/{bucket}/{key}", dependencies=[Depends(require_signature(auth.OP_WRITE))])
async def put_object(bucket: str, key: str, request: Request,
                     content_length: Annotated[int | None, Header()] = None):
    if READ_ONLY:
        raise HTTPException(status_code=405)

    if not check_content_type_extension(key, request.headers.get('content-type')):
        raise HTTPException(status_code=415)

    path = object_filename(bucket, key)

    # O_EXCL creates the object atomically: an existing key — or a racing
    # same-key PUT that won the create — lands here as 409.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise HTTPException(status_code=409) from None
    except (FileNotFoundError, NotADirectoryError):
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
            if content_length is not None and os.fstat(fd).st_size != content_length:
                raise HTTPException(status_code=400)
            file_digest = await asyncio.to_thread(file_checksum, path)
            if request_digest and file_digest != request_digest:
                raise HTTPException(status_code=400)
            ChecksumFile(path.parent).append(path.name, file_digest)
            return Response(status_code=201, content=None,
                            headers={"Repr-Digest": http_digest_head(file_digest)})
        except OSError as e:
            path.unlink(missing_ok=True)
            if e.errno == errno.ENOSPC:
                raise HTTPException(status_code=507) from None
            raise
        except BaseException:
            # Any non-crash failure (client disconnect, cancellation, bad
            # length/digest) must leave no partial object behind.
            path.unlink(missing_ok=True)
            raise
    finally:
        os.close(fd)

@app.get("/")
def list_buckets():
    """List buckets — not permitted"""
    raise HTTPException(status_code=403)

@app.head("/{bucket}/", dependencies=[Depends(require_signature(auth.OP_LIST))])
def head_bucket(bucket: str):
    dir_path = safe_path(bucket)
    if not dir_path.is_dir():
        raise HTTPException(status_code=404)
    return Response(status_code=200)


@app.get("/{bucket}/", dependencies=[Depends(require_signature(auth.OP_LIST))])
def list_directory(bucket: str):
    """List objects in bucket"""
    dir_path = safe_path(bucket)
    if not dir_path.is_dir():
        raise HTTPException(status_code=404)
    r = {"bucket": bucket,
         "objects": {}}
    hashes = ChecksumFile(dir_path).as_dict()
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
