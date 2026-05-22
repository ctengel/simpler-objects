"""Tests for simpler_objects.common shared utilities."""

import pytest
from simpler_objects.common import filter_write_candidates

SERVER = "http://node1:29171/"
MB = 1024 * 1024

def _node(write=True, percent=50, available=100 * MB):
    return {'write': write, 'percent': percent, 'quota-available-bytes': available,
            'quota-used-bytes': 0}

def test_eligible_server_included():
    health = {SERVER: _node()}
    result = filter_write_candidates(health, 10 * MB)
    assert SERVER in result
    assert result[SERVER] == _node()['quota-available-bytes'] * _node()['percent']

def test_write_false_excluded():
    health = {SERVER: _node(write=False)}
    assert filter_write_candidates(health, 10 * MB) == {}

def test_percent_too_low_excluded():
    health = {SERVER: _node(percent=1)}
    assert filter_write_candidates(health, 10 * MB) == {}

def test_insufficient_quota_excluded():
    # object_size + 1 MiB overhead must fit; make available exactly equal → still excluded
    health = {SERVER: _node(available=10 * MB + MB)}  # exactly at threshold → excluded
    assert filter_write_candidates(health, 10 * MB) == {}

def test_sufficient_quota_included():
    health = {SERVER: _node(available=10 * MB + MB + 1)}
    assert SERVER in filter_write_candidates(health, 10 * MB)

def test_exclude_set_removes_server():
    health = {SERVER: _node()}
    assert filter_write_candidates(health, 10 * MB, exclude={SERVER}) == {}

def test_empty_health_returns_empty():
    assert filter_write_candidates({}, 10 * MB) == {}

def test_multiple_servers_weighted():
    s1, s2 = "http://a/", "http://b/"
    h1 = _node(available=200 * MB, percent=80)
    h2 = _node(available=100 * MB, percent=50)
    result = filter_write_candidates({s1: h1, s2: h2}, 10 * MB)
    assert result[s1] == 200 * MB * 80
    assert result[s2] == 100 * MB * 50
