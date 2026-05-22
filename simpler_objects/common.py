"""Shared utilities for locator and replication modules."""


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
