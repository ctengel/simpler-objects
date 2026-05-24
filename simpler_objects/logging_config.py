"""Structured logging for Simpler Objects.

Single owner of the JSON-lines log format and the per-request ``X-Request-Id``
contextvar that lets a locator log line be tied to the object-server log line
it triggered.

Server entry points (``object_server.py``, ``locator_api.py``) and the CLI
(``async_replicate.py``) call :func:`configure`. The client library
(``client.py``) attaches a :class:`NullHandler` and lets its embedder decide.
"""

import contextvars
import datetime
import json
import logging
import logging.config
import os
import sys
import time
import uuid

# Set by the FastAPI middleware (see install_request_id_middleware); read by
# JsonFormatter so every record inside a request inherits the same ID.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

# LogRecord fields populated by the logging machinery itself. Anything else on
# the record came from logger.<level>(..., extra={...}) and should land in the
# JSON output as a structured field.
_STD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.datetime.fromtimestamp(
                record.created, tz=datetime.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # An explicit attribute on the record (set by a filter) wins over the
        # contextvar so callers can override per-record if they need to.
        rid = getattr(record, "request_id", None) or request_id_var.get()
        if rid is not None:
            payload["request_id"] = rid
        for key, value in record.__dict__.items():
            if key in _STD_ATTRS or key.startswith("_") or key == "request_id":
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(level: str | None = None) -> dict:
    """Install the JSON formatter on stderr. Safe to call multiple times.

    Precedence: explicit ``level`` arg, then ``LOG_LEVEL`` env var, then INFO.
    Returns the dictConfig so it can be persisted to a JSON file and passed to
    uvicorn via ``--log-config`` for production deployments where ``fastapi
    run`` would otherwise reset the config to its text default.
    """
    resolved = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {"()": "simpler_objects.logging_config.JsonFormatter"},
        },
        "handlers": {
            "stderr": {
                "class": "logging.StreamHandler",
                "stream": sys.stderr,
                "formatter": "json",
            },
        },
        # Empty handler lists + propagate=True replace uvicorn's own text
        # handlers and route every record through the single root stderr/JSON
        # handler. caplog (which hooks at root) sees our records too.
        "loggers": {
            "simpler_objects": {"level": resolved, "handlers": [], "propagate": True},
            "uvicorn": {"level": resolved, "handlers": [], "propagate": True},
            "uvicorn.error": {"level": resolved, "handlers": [], "propagate": True},
            "uvicorn.access": {"level": resolved, "handlers": [], "propagate": True},
        },
        "root": {"level": resolved, "handlers": ["stderr"]},
    }
    logging.config.dictConfig(config)
    return config


def install_request_id_middleware(app) -> None:
    """Attach a FastAPI middleware that owns the per-request X-Request-Id.

    Honours an inbound ``X-Request-Id`` header (so the locator can pass its ID
    through to the object server) and generates a uuid4 hex string otherwise.
    Echoes the ID back on the response so callers can correlate.

    ``/health`` is demoted to DEBUG to avoid swamping logs with health-check
    chatter; everything else logs request.start + request.end at INFO.
    """
    from starlette.requests import Request

    logger = logging.getLogger("simpler_objects.request")

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        start = time.monotonic()
        is_health = request.url.path == "/health"
        level = logging.DEBUG if is_health else logging.INFO
        client_ip = request.client.host if request.client else None
        try:
            logger.log(level, "request.start", extra={
                "method": request.method,
                "path": request.url.path,
                "client_ip": client_ip,
                "content_length": request.headers.get("content-length"),
            })
            try:
                response = await call_next(request)
            except Exception:
                latency_ms = round((time.monotonic() - start) * 1000.0, 2)
                logger.error("request.crash", exc_info=True, extra={
                    "method": request.method,
                    "path": request.url.path,
                    "latency_ms": latency_ms,
                })
                raise
            latency_ms = round((time.monotonic() - start) * 1000.0, 2)
            logger.log(level, "request.end", extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": latency_ms,
            })
            response.headers["X-Request-Id"] = rid
            return response
        finally:
            request_id_var.reset(token)
