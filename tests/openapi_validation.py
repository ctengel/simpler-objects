"""Validate HTTP responses against the hand-maintained openapi.yaml.

ValidatingTestClient wraps Starlette's TestClient so every response a test
makes is checked against the spec: the status code must be documented for the
operation, and any JSON body must conform to the documented schema. Responses
with a binary or empty body (object downloads, 201, redirects, HEAD) have only
their status code checked.

This is the runtime companion to test_openapi_drift.py: the drift test checks
the *shape* of the spec (which paths/methods exist); this checks that actual
responses *conform* to it.
"""

import pathlib
import re

import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

_SPEC_PATH = pathlib.Path(__file__).parent.parent / "openapi.yaml"
_SPEC = yaml.safe_load(_SPEC_PATH.read_text(encoding="utf-8"))
# Register the whole document under a named URI; schemas are validated via
# absolute refs into it so the spec's own intra-document refs resolve.
_SPEC_URI = "urn:simpler-objects-openapi"
_REGISTRY = Registry().with_resource(
    uri=_SPEC_URI, resource=Resource(contents=_SPEC, specification=DRAFT202012)
)


def _match_path(concrete_path):
    """Map a concrete request path to its openapi.yaml path template."""
    paths = _SPEC["paths"]
    if concrete_path in paths:
        return concrete_path
    for template in paths:
        if "{" not in template:
            continue
        regex = "/".join(
            "[^/]+" if seg.startswith("{") and seg.endswith("}") else re.escape(seg)
            for seg in template.split("/")
        )
        if re.fullmatch(regex, concrete_path):
            return template
    return None


def _json_pointer(*segments):
    """Build an RFC 6901 JSON pointer from raw key segments."""
    return "/" + "/".join(s.replace("~", "~0").replace("/", "~1") for s in segments)


def _schema_ref(template, method, status):
    """Absolute $ref to the JSON schema documented for a response, or None.

    Matches any json-ish media type, so application/problem+json error bodies
    are validated the same as application/json.
    """
    response_def = _SPEC["paths"][template][method]["responses"][status]
    for media_type, media in response_def.get("content", {}).items():
        if "json" in media_type and "schema" in media:
            pointer = _json_pointer(
                "paths", template, method, "responses", status,
                "content", media_type, "schema",
            )
            return _SPEC_URI + "#" + pointer
    return None


def validate_response(response):
    """Assert an httpx response conforms to openapi.yaml."""
    method = response.request.method.lower()
    path = response.request.url.path
    template = _match_path(path)
    assert template, f"no openapi.yaml path template matches {path!r}"

    operation = _SPEC["paths"][template].get(method)
    assert operation, f"openapi.yaml documents no {method.upper()} {template}"

    status = str(response.status_code)
    documented = operation.get("responses", {})
    assert status in documented, (
        f"{method.upper()} {template} returned undocumented status {status} "
        f"(openapi.yaml documents {sorted(documented)})"
    )

    ref = _schema_ref(template, method, status)
    if ref is None or method == "head" or not response.content:
        return
    Draft202012Validator({"$ref": ref}, registry=_REGISTRY).validate(response.json())


class ValidatingTestClient(TestClient):
    """TestClient that validates every response against openapi.yaml.

    Starlette's TestClient routes all verbs through request(); overriding it
    once covers get/put/head/etc.
    """

    def request(self, method, url, *args, **kwargs):
        response = super().request(method, url, *args, **kwargs)
        for hop in (*response.history, response):
            validate_response(hop)
        return response
