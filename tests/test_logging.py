"""Tests for the JSON formatter and request-id middleware."""

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from simpler_objects.logging_config import (
    JsonFormatter,
    install_request_id_middleware,
    request_id_var,
)


def _make_record(msg="hello", level=logging.INFO, extra=None, exc_info=None):
    record = logging.LogRecord(
        name="simpler_objects.test", level=level, pathname=__file__,
        lineno=0, msg=msg, args=(), exc_info=exc_info,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


def test_json_formatter_basic_shape():
    formatter = JsonFormatter()
    record = _make_record("hi", extra={"bucket": "b", "key": "k", "size": 7})
    payload = json.loads(formatter.format(record))
    assert payload["msg"] == "hi"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "simpler_objects.test"
    assert payload["bucket"] == "b"
    assert payload["key"] == "k"
    assert payload["size"] == 7
    assert "ts" in payload


def test_json_formatter_request_id_from_contextvar():
    formatter = JsonFormatter()
    token = request_id_var.set("abc123")
    try:
        payload = json.loads(formatter.format(_make_record()))
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "abc123"


def test_json_formatter_no_request_id_when_unset():
    formatter = JsonFormatter()
    # Make sure no prior test leaked an ID into the context.
    assert request_id_var.get() is None
    payload = json.loads(formatter.format(_make_record()))
    assert "request_id" not in payload


def test_json_formatter_includes_exc_info():
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = _make_record("crash", level=logging.ERROR, exc_info=sys.exc_info())
    payload = json.loads(formatter.format(record))
    assert "exc_info" in payload
    assert "ValueError: boom" in payload["exc_info"]


def _client_with_middleware():
    app = FastAPI()
    install_request_id_middleware(app)
    captured: dict = {}

    @app.get("/echo")
    def echo():
        captured["rid"] = request_id_var.get()
        return {"ok": True}

    return TestClient(app), captured


def test_middleware_generates_id_when_absent():
    client, captured = _client_with_middleware()
    resp = client.get("/echo")
    assert resp.status_code == 200
    rid = resp.headers["X-Request-Id"]
    assert len(rid) == 32  # uuid4().hex
    assert captured["rid"] == rid


def test_middleware_honours_inbound_header():
    client, captured = _client_with_middleware()
    resp = client.get("/echo", headers={"X-Request-Id": "trace-xyz"})
    assert resp.headers["X-Request-Id"] == "trace-xyz"
    assert captured["rid"] == "trace-xyz"


def test_middleware_logs_request_start_and_end(caplog):
    client, _ = _client_with_middleware()
    with caplog.at_level(logging.INFO, logger="simpler_objects.request"):
        client.get("/echo", headers={"X-Request-Id": "rid-9"})
    messages = [(r.levelname, r.message) for r in caplog.records
                if r.name == "simpler_objects.request"]
    assert ("INFO", "request.start") in messages
    assert ("INFO", "request.end") in messages
    # status should be attached to request.end
    end = next(r for r in caplog.records if r.message == "request.end")
    assert getattr(end, "status") == 200
    assert hasattr(end, "latency_ms")


def test_middleware_demotes_health_to_debug(caplog):
    app = FastAPI()
    install_request_id_middleware(app)

    @app.get("/health")
    def health():
        return {"ok": True}

    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="simpler_objects.request"):
        client.get("/health")
    # No INFO records for /health — it's DEBUG.
    info_records = [r for r in caplog.records
                    if r.name == "simpler_objects.request" and r.levelno >= logging.INFO]
    assert info_records == []
