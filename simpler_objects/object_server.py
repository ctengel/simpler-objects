"""Simpler Objects Server"""

import argparse
import pathlib
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

class PutHTTPRequestHandler(SimpleHTTPRequestHandler):
    """Extension to basic GET handler that also handles PUT"""

    def do_PUT(self):
        """Handle PUT requests"""
        length = int(self.headers["Content-Length"])
        path = pathlib.Path(self.translate_path(self.path))
        if path.exists():
            self.send_response(409)
            self.end_headers()
            return
        with open(path, "wb") as dst:
            dst.write(self.rfile.read(length))
        self.send_response(200)
        self.end_headers()

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
