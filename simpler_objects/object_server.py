"""Simpler Objects Server"""

# based on https://gist.github.com/fabiand/5628006

import argparse
import pathlib
import io
import json
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler, HTTPStatus
import shutil
import base64
import hashlib

BUFFER = 67108864

class PutHTTPRequestHandler(SimpleHTTPRequestHandler):
    """Extension to basic GET handler that also handles PUT"""

    def do_PUT(self):
        """Handle PUT requests"""

        # no PUT /health
        if self.path == '/health':
            self.send_response(403)
            self.end_headers()
            return

        # parse Content-Length
        try:
            length = int(self.headers["Content-Length"])
        except TypeError:
            self.send_response(400)
            self.end_headers()
            return

        # ensure unique fikename
        path = pathlib.Path(self.translate_path(self.path))
        if path.exists():
            self.send_response(409)
            self.end_headers()
            return

        # parse Digest
        request_digest = None
        digest_header = self.headers.get('Repr-Digest')
        if digest_header:
            request_digest = base64.b64decode([x.partition('=')[2]
                                               for x in digest_header.split(',')
                                               if x.partition('=')[0] == 'sha-256'][0].strip(':'))

        # Receive file
        pos = 0
        with open(path, "wb") as dst:
            while True:
                nextread = min(BUFFER, length - pos)
                if not nextread:
                    break
                dst.write(self.rfile.read(nextread))
                pos = pos + nextread
        assert path.stat().st_size == length

        # Hash and compare
        hash_sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(BUFFER), b""):
                hash_sha256.update(chunk)
        file_digest = hash_sha256.digest()
        if request_digest and file_digest != request_digest:
            self.send_response(400)
            self.end_headers()
            path.unlink()
            return

        # write hash to disk
        bucket = path.parent
        hash_file = bucket.parent.joinpath(bucket.name).with_suffix('.sha256')
        cksum_line = f"{file_digest.hex()}  {path.name}\n"
        with open(hash_file, 'a', encoding='utf-8') as hf:
            hf.write(cksum_line)

        # send response
        self.send_response(201)
        self.send_header("Repr-Digest", f"sha-256=:{base64.b64encode(file_digest).decode()}:")
        self.end_headers()

    def list_directory(self, path):
        """Override for JSON instead of HTML

        Return value is either a file object, or None (indicating an
        error).  In either case, the headers are sent, making the
        interface the same as for send_head().

        """
        # based on https://github.com/python/cpython/blob/3.13/Lib/http/server.py
        try:
            dir_path = pathlib.Path(path)
        except OSError:
            self.send_error(
                HTTPStatus.NOT_FOUND,
                "No permission to list directory")
            return None
        r = {"bucket": self.path,
             "objects": {}}
        for name in dir_path.iterdir():
            if name.is_dir():
                r['objects'][name.name] = {'directory': True,
                                           'size': 0}
            else:
                r['objects'][name.name] = {'directory': False,
                                           'size': name.stat().st_size}
        f = io.BytesIO(json.dumps(r).encode('utf-8'))
        length = len(f.getvalue())
        f.seek(0)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-type", "application/json")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        return f

    def do_HEAD(self):
        if self.path == '/health':
            self.send_response(HTTPStatus.OK)
            self.end_headers()
            return
        super().do_HEAD()

    def healthcheck(self):
        """Return basic info on node health"""
        disk_stats = shutil.disk_usage(pathlib.Path(self.translate_path('/')))
        r = {'read': True,
             'write': True,
             'available': disk_stats.free,
             'percent': int(float(disk_stats.free)/float(disk_stats.total)*100.0)}
        f = io.BytesIO(json.dumps(r).encode('utf-8'))
        length = len(f.getvalue())
        f.seek(0)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-type", "application/json")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        try:
            self.copyfile(f, self.wfile)
        finally:
            f.close()

    def do_GET(self):
        if self.path == '/health':
            self.healthcheck()
            return
        super().do_GET()


class DirHTTPServer(ThreadingHTTPServer):
    """An HTTP server in a particular directory"""

    directory = None

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.directory = directory

    def finish_request(self, request, client_address):
        self.RequestHandlerClass(request, client_address, self,
                                 directory=self.directory)


def run(server_class=DirHTTPServer, handler_class=PutHTTPRequestHandler,
        port=46579, directory=None):
    """Run this server"""
    server_address = ('', port)
    httpd = server_class(server_address, handler_class, directory=directory)
    httpd.serve_forever()

def cli():
    """CLI"""
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--directory")
    parser.add_argument("port", default=46579)
    args = parser.parse_args()
    run(port=int(args.port), directory=args.directory)

if __name__ == '__main__':
    cli()
