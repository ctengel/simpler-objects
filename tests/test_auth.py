"""Unit tests for simpler_objects.auth — signing/verification and AuthConfig."""

import time

import pytest

from simpler_objects import auth

SECRET = "test-cluster-secret"
BUCKET = "mybucket"
KEY = "myfile.bin"
NOW = 1_700_000_000


def _parse(query: str) -> dict:
    return dict(pair.split("=", 1) for pair in query.split("&"))


# ---------------------------------------------------------------------------
# sign / signed_query / verify round trips
# ---------------------------------------------------------------------------

def test_round_trip():
    q = _parse(auth.signed_query(SECRET, auth.OP_READ, BUCKET, KEY, ttl=900, now=NOW))
    assert auth.verify(SECRET, auth.OP_READ, BUCKET, KEY, q["exp"], q["sig"], now=NOW)


def test_round_trip_list_no_key():
    q = _parse(auth.signed_query(SECRET, auth.OP_LIST, BUCKET, now=NOW))
    assert auth.verify(SECRET, auth.OP_LIST, BUCKET, "", q["exp"], q["sig"], now=NOW)


def test_tampered_sig_rejected():
    q = _parse(auth.signed_query(SECRET, auth.OP_READ, BUCKET, KEY, now=NOW))
    bad = ("0" if q["sig"][0] != "0" else "1") + q["sig"][1:]
    assert not auth.verify(SECRET, auth.OP_READ, BUCKET, KEY, q["exp"], bad, now=NOW)


def test_tampered_exp_rejected():
    q = _parse(auth.signed_query(SECRET, auth.OP_READ, BUCKET, KEY, now=NOW))
    later = str(int(q["exp"]) + 1)
    assert not auth.verify(SECRET, auth.OP_READ, BUCKET, KEY, later, q["sig"], now=NOW)


def test_wrong_secret_rejected():
    q = _parse(auth.signed_query(SECRET, auth.OP_READ, BUCKET, KEY, now=NOW))
    assert not auth.verify("other-secret", auth.OP_READ, BUCKET, KEY,
                           q["exp"], q["sig"], now=NOW)


def test_wrong_operation_rejected():
    """A signature minted for read must not authorize a write (or list)."""
    q = _parse(auth.signed_query(SECRET, auth.OP_READ, BUCKET, KEY, now=NOW))
    assert not auth.verify(SECRET, auth.OP_WRITE, BUCKET, KEY, q["exp"], q["sig"], now=NOW)
    assert not auth.verify(SECRET, auth.OP_LIST, BUCKET, KEY, q["exp"], q["sig"], now=NOW)


def test_wrong_object_rejected():
    q = _parse(auth.signed_query(SECRET, auth.OP_READ, BUCKET, KEY, now=NOW))
    assert not auth.verify(SECRET, auth.OP_READ, BUCKET, "other.bin",
                           q["exp"], q["sig"], now=NOW)
    assert not auth.verify(SECRET, auth.OP_READ, "otherbucket", KEY,
                           q["exp"], q["sig"], now=NOW)


# ---------------------------------------------------------------------------
# expiry and clock skew
# ---------------------------------------------------------------------------

def test_expiry_boundary():
    exp = NOW
    sig = auth.sign(SECRET, auth.OP_READ, BUCKET, KEY, exp)
    # Accepted right up to exp + CLOCK_SKEW, rejected after.
    assert auth.verify(SECRET, auth.OP_READ, BUCKET, KEY, str(exp), sig,
                       now=NOW + auth.CLOCK_SKEW)
    assert not auth.verify(SECRET, auth.OP_READ, BUCKET, KEY, str(exp), sig,
                           now=NOW + auth.CLOCK_SKEW + 1)


def test_default_now_is_wall_clock():
    q = _parse(auth.signed_query(SECRET, auth.OP_READ, BUCKET, KEY))
    assert auth.verify(SECRET, auth.OP_READ, BUCKET, KEY, q["exp"], q["sig"])
    assert int(q["exp"]) == pytest.approx(time.time() + auth.DEFAULT_TTL, abs=5)


@pytest.mark.parametrize("exp", ["", "abc", "12.5", None, "9" * 100 + "x"])
def test_malformed_exp_rejected(exp):
    sig = auth.sign(SECRET, auth.OP_READ, BUCKET, KEY, NOW)
    assert not auth.verify(SECRET, auth.OP_READ, BUCKET, KEY, exp, sig, now=NOW)


def test_non_ascii_sig_rejected():
    assert not auth.verify(SECRET, auth.OP_READ, BUCKET, KEY, str(NOW), "sigé", now=NOW)


# ---------------------------------------------------------------------------
# canonicalization — awkward bucket/key values must not collide or break
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "spaces in name.txt",
    "percent%20literal.bin",
    "unicode-é中.txt",
    "newline\nafter-decode",
    "query?&=chars",
])
def test_awkward_keys_round_trip(key):
    q = _parse(auth.signed_query(SECRET, auth.OP_READ, BUCKET, key, now=NOW))
    assert auth.verify(SECRET, auth.OP_READ, BUCKET, key, q["exp"], q["sig"], now=NOW)
    assert not auth.verify(SECRET, auth.OP_READ, BUCKET, key + "x",
                           q["exp"], q["sig"], now=NOW)


def test_field_boundaries_unambiguous():
    """A newline in a decoded key cannot forge the record of another object."""
    sig_a = auth.sign(SECRET, auth.OP_READ, "b", "k\nx", NOW)
    sig_b = auth.sign(SECRET, auth.OP_READ, "b\nk", "x", NOW)
    assert sig_a != sig_b


# ---------------------------------------------------------------------------
# AuthConfig
# ---------------------------------------------------------------------------

CONFIG_TOML = """
[clients.oi]
key = "oi-key-abc"
[clients.oi.buckets]
photos = ["read", "write", "list"]
indexes = ["read"]

[clients.pv]
key = "pv-key-def"
[clients.pv.buckets]
"*" = ["read"]
"""


@pytest.fixture()
def config(tmp_path):
    path = tmp_path / "auth.toml"
    path.write_text(CONFIG_TOML)
    path.chmod(0o600)
    return auth.AuthConfig.load(path)


def _basic(name, key):
    import base64
    return "Basic " + base64.b64encode(f"{name}:{key}".encode()).decode()


def test_authenticate_bearer(config):
    assert config.authenticate("Bearer oi-key-abc") == "oi"
    assert config.authenticate("bearer pv-key-def") == "pv"


def test_authenticate_basic(config):
    assert config.authenticate(_basic("oi", "oi-key-abc")) == "oi"


def test_authenticate_basic_wrong_username(config):
    """A valid key under the wrong client name must not authenticate."""
    assert config.authenticate(_basic("pv", "oi-key-abc")) is None


def test_authenticate_failures(config):
    assert config.authenticate(None) is None
    assert config.authenticate("") is None
    assert config.authenticate("Bearer wrong-key") is None
    assert config.authenticate("Bearer ") is None
    assert config.authenticate("Basic not!base64") is None
    assert config.authenticate("Digest whatever") is None


def test_allowed_exact_bucket(config):
    assert config.allowed("oi", "photos", auth.OP_WRITE)
    assert config.allowed("oi", "indexes", auth.OP_READ)
    assert not config.allowed("oi", "indexes", auth.OP_WRITE)
    assert not config.allowed("oi", "elsewhere", auth.OP_READ)


def test_allowed_wildcard(config):
    assert config.allowed("pv", "anything", auth.OP_READ)
    assert not config.allowed("pv", "anything", auth.OP_WRITE)


def test_exact_bucket_overrides_wildcard(tmp_path):
    path = tmp_path / "auth.toml"
    path.write_text("""
[clients.c]
key = "k"
[clients.c.buckets]
"*" = ["read", "write"]
locked = ["read"]
""")
    config = auth.AuthConfig.load(path)
    assert config.allowed("c", "open", auth.OP_WRITE)
    assert not config.allowed("c", "locked", auth.OP_WRITE)


def test_load_rejects_missing_key(tmp_path):
    path = tmp_path / "auth.toml"
    path.write_text("[clients.broken]\n[clients.broken.buckets]\nb = ['read']\n")
    with pytest.raises(ValueError):
        auth.AuthConfig.load(path)


def test_load_rejects_unknown_operation(tmp_path):
    path = tmp_path / "auth.toml"
    path.write_text("[clients.c]\nkey = 'k'\n[clients.c.buckets]\nb = ['purge']\n")
    with pytest.raises(ValueError):
        auth.AuthConfig.load(path)


def test_load_accepts_delete_operation(tmp_path):
    path = tmp_path / "auth.toml"
    path.write_text("[clients.c]\nkey = 'k'\n[clients.c.buckets]\nb = ['delete']\n")
    path.chmod(0o600)
    config = auth.AuthConfig.load(path)
    assert config.allowed("c", "b", auth.OP_DELETE)
    assert not config.allowed("c", "b", auth.OP_READ)


def test_load_warns_on_loose_permissions(tmp_path, capsys):
    path = tmp_path / "auth.toml"
    path.write_text("[clients.c]\nkey = 'k'\n")
    path.chmod(0o644)
    auth.AuthConfig.load(path)
    assert "group/world-accessible" in capsys.readouterr().err
