"""Shared utilities for locator and replication modules."""

import os
import pathlib
import ssl
import string
import mimetypes


_HEX_CHARS = frozenset(string.hexdigits.lower())


def httpx_verify(ca_bundle: str):
    """Return an httpx verify= value for an optional CA bundle path.

    httpx deprecates passing the path as a string; hand it an SSLContext.
    An empty/unset bundle means the default system trust store.
    """
    if not ca_bundle:
        return True
    return ssl.create_default_context(cafile=ca_bundle)


def check_content_type_extension(key: str, content_type: str | None) -> bool:
    """Return False when content_type conflicts with the MIME type implied by key's extension.

    Only rejects when both the Content-Type and the extension MIME are known and
    disagree. application/octet-stream is accepted for any extension.
    """
    if not content_type:
        return True
    ct_base = content_type.split(';')[0].strip()
    ext_mime, _ = mimetypes.guess_type(key, strict=False)
    if ext_mime is None or ct_base == 'application/octet-stream':
        return True
    if ct_base == ext_mime:
        return True
    # Return False if there are better extensions for this type
    return not mimetypes.guess_all_extensions(ct_base, strict=False)


def filter_write_candidates(health: dict, object_size: int, exclude=()) -> dict:
    """Return {server_url: weight} for servers able to store object_size bytes."""
    return {
        server: stats['quota-available-bytes'] * stats['percent']
        for server, stats in health.items()
        if stats['write']
        and stats['percent'] > 1
        and stats['quota-available-bytes'] > object_size + 1024 * 1024
        and server not in exclude
    }


def parse_checksum_line(line: str):
    """Return (hex_digest, filename) for a valid sha256sum line, else None.

    A valid line has exactly two whitespace-separated fields and the first
    is a 64-char lowercase hex digest. Catches torn lines (wrong field
    count) and torn-fragment-merged-with-next-append (non-hex first field).
    """
    parts = line.strip().split()
    if len(parts) != 2:
        return None
    digest, filename = parts
    if len(digest) != 64:
        return None
    if not all(c in _HEX_CHARS for c in digest):
        return None
    return digest, filename


def iter_checksum_file(path: pathlib.Path):
    """Yield (digest, filename) for every valid line; silently handle missing file."""
    try:
        with open(path, encoding='utf-8') as fp:
            for line in fp:
                parsed = parse_checksum_line(line)
                if parsed is not None:
                    yield parsed
    except FileNotFoundError:
        pass


class ChecksumFile:
    """Handle for a bucket's <name>.sha256 file."""

    def __init__(self, bucket_dir: pathlib.Path):
        self.path = bucket_dir.parent / f"{bucket_dir.name}.sha256"

    def __iter__(self):
        return iter_checksum_file(self.path)

    def lookup(self, key: str):
        """Return bytes digest for key, or None."""
        for digest, filename in self:
            if filename == key:
                return bytes.fromhex(digest)
        return None

    def as_dict(self) -> dict:
        """Return {filename: digest_hex} for all valid entries."""
        return {filename: digest for digest, filename in self}

    def append(self, key: str, digest: bytes) -> None:
        """Durably append a sha256sum-format line for key.

        The single O_APPEND os.write() is atomic against concurrent appenders (POSIX),
        so different-key PUTs in the same bucket need no extra serialisation.
        """
        cksum_line = f"{digest.hex()}  {key}\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, cksum_line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
