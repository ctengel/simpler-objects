"""Shared utilities for locator and replication modules."""

import mimetypes


def check_content_type_extension(key: str, content_type: str | None) -> bool:
    """Return False when content_type conflicts with the MIME type implied by key's extension.

    Only rejects when both the Content-Type and the extension MIME are known and
    disagree. application/octet-stream is accepted for any extension.
    """
    if not content_type:
        return True
    ct_base = content_type.split(';')[0].strip()
    ext_mime, _ = mimetypes.guess_type(key)
    if ext_mime is None or ct_base == 'application/octet-stream':
        return True
    return ct_base == ext_mime


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
