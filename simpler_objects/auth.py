"""Signed URLs and API-key authentication for Simpler Objects.

Two independent, opt-in mechanisms live here:

- **Signed URLs** (``sign``/``signed_query``/``verify``): the locator (and the
  replicator) append ``?exp=<unix-seconds>&sig=<hex>`` to every URL that hits
  an object server, HMAC-signed with a cluster-wide shared secret
  (``CLUSTER_SECRET``). Object servers verify the signature before touching
  the filesystem or reading a request body. They never see client
  credentials.

- **API keys** (``AuthConfig``): the locator authenticates external clients
  against a TOML file (``AUTH_CONFIG``) mapping each key to a client name and
  a per-bucket set of allowed operations.

The signature covers the *operation*, not the raw HTTP method: GET and HEAD
of an object are both ``read``, so the signature the locator mints for its
own HEAD probe is the same one the client uses on the redirected GET.
"""

import base64
import hashlib
import hmac
import os
import sys
import tomllib
import urllib.parse
import time

OP_READ = "read"
OP_WRITE = "write"
OP_LIST = "list"
OP_DELETE = "delete"

# Seconds past `exp` a signature is still accepted, absorbing clock drift
# between locator and object servers. The effective replay window is
# TTL + CLOCK_SKEW.
CLOCK_SKEW = 60

DEFAULT_TTL = 900


def canonical_string(operation: str, bucket: str, key: str, exp) -> str:
    """Build the string both signer and verifier feed to HMAC.

    ``bucket`` and ``key`` are the *decoded* path params as FastAPI hands them
    over; re-encoding with ``quote(safe='')`` makes the field boundaries
    unambiguous (a decoded newline or ``/`` cannot forge a different record).
    ``key`` is the empty string for bucket-level (list) operations.
    """
    return "\n".join(["so-sig-v1",
                      operation,
                      urllib.parse.quote(bucket, safe=""),
                      urllib.parse.quote(key, safe=""),
                      str(exp)])


def sign(secret: str, operation: str, bucket: str, key: str, exp) -> str:
    """Return the hex HMAC-SHA256 signature for one request."""
    canonical = canonical_string(operation, bucket, key, exp)
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def signed_query(secret: str, operation: str, bucket: str, key: str = "",
                 ttl: int = DEFAULT_TTL, now=None) -> str:
    """Return ``exp=...&sig=...`` authorizing one operation until now+ttl."""
    if now is None:
        now = time.time()
    exp = int(now) + ttl
    return f"exp={exp}&sig={sign(secret, operation, bucket, key, exp)}"


def verify(secret: str, operation: str, bucket: str, key: str,
           exp: str, sig: str, now=None) -> bool:
    """Check a signature and its expiry; malformed input is simply invalid."""
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        return False
    if now is None:
        now = time.time()
    if exp_int + CLOCK_SKEW < now:
        return False
    try:
        return hmac.compare_digest(sign(secret, operation, bucket, key, exp_int), sig)
    except TypeError:
        # compare_digest rejects non-ASCII str input; such a sig is just invalid
        return False


class AuthConfig:
    """Client API keys and their per-bucket permissions (see auth.toml docs).

    ``clients`` maps client name -> {"key": str, "buckets": {bucket: [ops]}}.
    A bucket entry of ``"*"`` applies to any bucket without an exact entry.
    """

    def __init__(self, clients: dict):
        for name, entry in clients.items():
            if not isinstance(entry.get("key"), str) or not entry["key"]:
                raise ValueError(f"auth config client {name!r}: missing or empty key")
            for bucket, ops in entry.get("buckets", {}).items():
                bad = set(ops) - {OP_READ, OP_WRITE, OP_LIST, OP_DELETE}
                if bad:
                    raise ValueError(
                        f"auth config client {name!r} bucket {bucket!r}: "
                        f"unknown operations {sorted(bad)}")
        self.clients = clients

    @classmethod
    def load(cls, path) -> "AuthConfig":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        config = cls(data.get("clients", {}))
        if os.stat(path).st_mode & 0o077:
            print(f"WARNING: auth config {path} is group/world-accessible; "
                  "chmod 600 recommended", file=sys.stderr)
        return config

    def authenticate(self, authorization: str | None) -> str | None:
        """Resolve an Authorization header to a client name, or None.

        Accepts ``Bearer <key>`` and ``Basic base64(name:key)``. Every
        configured key is always compared (constant-time), so response timing
        does not reveal whether a key exists.
        """
        scheme, _, param = (authorization or "").partition(" ")
        scheme = scheme.lower()
        param = param.strip()
        basic_name = presented = None
        if scheme == "bearer":
            presented = param
        elif scheme == "basic":
            try:
                decoded = base64.b64decode(param, validate=True).decode()
                basic_name, _, presented = decoded.partition(":")
            except (ValueError, UnicodeDecodeError):
                return None
        if not presented:
            return None
        matched = None
        for name, entry in self.clients.items():
            if hmac.compare_digest(entry["key"], presented):
                if basic_name is None or basic_name == name:
                    matched = name
        return matched

    def allowed(self, client: str, bucket: str, operation: str) -> bool:
        """Check a client's permission: exact bucket entry, else "*" entry."""
        buckets = self.clients[client].get("buckets", {})
        ops = buckets.get(bucket)
        if ops is None:
            ops = buckets.get("*", [])
        return operation in ops
