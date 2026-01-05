"""Simpler Objects Server"""

import pathlib
import shutil
import base64
import hashlib
import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

app = FastAPI()

# TODO allow spec on command line
OBJECT_DIRECTORY = os.environ.get('OBJECT_DIRECTORY', '.')
BUFFER = 67108864

def parse_digest_header(value: str):
    """Get the SHA-256 checksum from a Content-Digest or Repr-Digest"""
    if not value:
        return None
    return base64.b64decode([x.partition('=')[2]
                            for x in value.split(',')
                            if x.partition('=')[0] == 'sha-256'][0].strip(':'))

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
    disk_stats = shutil.disk_usage(pathlib.Path(OBJECT_DIRECTORY))
    r = {'read': True,
         'write': True,
         'available': disk_stats.free,
         'percent': int(float(disk_stats.free)/float(disk_stats.total)*100.0)}
    return r

@app.get("/{bucket}/{key}")
async def get_object(bucket: str, key: str):
    """Handle GET requests"""
    # TODO make this safer
    return FileResponse(pathlib.Path(OBJECT_DIRECTORY).joinpath(bucket).joinpath(key))

@app.put("/{bucket}/{key}")
async def put_object(bucket: str, key: str, request: Request):
    """Handle PUT requests"""

    # parse Content-Length
    try:
        length = int(request.headers["Content-Length"])
    except TypeError as exc:
        raise HTTPException(status_code=400) from exc

    # ensure unique fikename
    # TODO make this safer
    path = pathlib.Path(OBJECT_DIRECTORY).joinpath(bucket).joinpath(key)
    if path.exists():
        raise HTTPException(status_code=409)

    # parse Digest
    request_digest = parse_digest_headers(request.headers)

    # Receive file
    with open(path, "wb") as dst:
        # TODO async? check length? lock?
        async for chunk in request.stream():
            dst.write(chunk)
    assert path.stat().st_size == length

    # Hash and compare
    file_digest = file_checksum(path)
    if request_digest and file_digest != request_digest:
        path.unlink()
        raise HTTPException(status_code=400)

    # write hash to disk
    bucketp = path.parent
    hash_file = bucketp.parent.joinpath(bucketp.name).with_suffix('.sha256')
    cksum_line = f"{file_digest.hex()}  {path.name}\n"
    with open(hash_file, 'a', encoding='utf-8') as hf:
        hf.write(cksum_line)

    # send response
    return Response(status_code=201, content=None,
                    headers={"Repr-Digest": f"sha-256=:{base64.b64encode(file_digest).decode()}:"})

# TODO root bucket list
@app.get("/{bucket}/")
def list_directory(bucket: str):
    """List objects in bucket"""
    try:
        dir_path = pathlib.Path(OBJECT_DIRECTORY).joinpath(bucket)
    except OSError as exc:
        raise HTTPException(status_code=404) from exc
    r = {"bucket": bucket,
         "objects": {}}
    try:
        for name in dir_path.iterdir():
            if name.is_dir():
                r['objects'][name.name] = {'directory': True,
                                           'size': 0}
            else:
                r['objects'][name.name] = {'directory': False,
                                           'size': name.stat().st_size}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404) from exc
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
