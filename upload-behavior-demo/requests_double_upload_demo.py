"""Demonstrate that `requests` uploads a PUT body twice through the locator.

The locator answers PUT with a 307 redirect and never reads the request body.
`requests` does not send `Expect: 100-continue`, so it streams the whole object
to the locator, receives the 307, then re-uploads it to the object server.

We count every byte `requests` reads from the file. `requests` reads the file
only to put it on the wire, and re-reads it (after a rewind) to follow the
redirect -- so the total read count is the total bytes uploaded.

Do NOT measure this with file.tell() after the request: `requests` rewinds
seekable bodies, so the final position reads 0 and does not reflect bytes sent.
"""

import io
import os
import requests

LOCATOR = "http://localhost:29164"
PATH = "/tmp/so-demo/file8m.bin"


class CountingReader(io.BufferedReader):
    """A real BufferedReader that tallies every byte handed to requests."""

    def __init__(self, path):
        super().__init__(io.FileIO(path, "rb"))
        self.bytes_read = 0

    def read(self, size=-1):
        data = super().read(size)
        self.bytes_read += len(data)
        return data

    def read1(self, size=-1):
        data = super().read1(size)
        self.bytes_read += len(data)
        return data


def main():
    size = os.path.getsize(PATH)
    body = CountingReader(PATH)

    resp = requests.put(f"{LOCATOR}/mybucket/demo", data=body, timeout=60)
    resp.raise_for_status()

    hops = " -> ".join(str(h.status_code) for h in resp.history)
    print(f"file size                   : {size:,} bytes")
    print(f"redirect chain              : {hops} -> {resp.status_code}")
    print(f"total bytes read by requests: {body.bytes_read:,} bytes")
    print(f"upload multiplier           : {body.bytes_read / size:.2f}x the file")


if __name__ == "__main__":
    main()
