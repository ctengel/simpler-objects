"""Shared utilities for locator and replication modules."""

import string


_HEX_CHARS = frozenset(string.hexdigits.lower())


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
