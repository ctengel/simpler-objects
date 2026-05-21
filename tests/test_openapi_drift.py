"""Drift test — keep the hand-maintained openapi.yaml in sync with the code.

openapi.yaml is a single unified spec describing the combined contract of both
apps; each FastAPI app implements a subset, so the spec is compared against the
*union* of operations the two apps serve.

Scope is deliberately limited to path + method coverage. It does NOT compare
response status codes or schemas. FastAPI's generated spec only knows the
200/422 defaults plus what is declared on route decorators, whereas the
hand-maintained spec documents every HTTPException path (307, 404, 409, 411,
507, 503, ...) and headers that are not function arguments (Range, If-None-Match,
Expect). Comparing those would be all false positives. Path/method coverage
catches the realistic drift: an endpoint added, removed, or renamed without a
matching openapi.yaml update.
"""

import pathlib

import pytest
import yaml

import simpler_objects.locator_api as locator_api
import simpler_objects.object_server as object_server

SPEC_PATH = pathlib.Path(__file__).parent.parent / "openapi.yaml"
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


def _operations(openapi_doc):
    """Return the set of (path, method) operations in an OpenAPI document."""
    return {
        (path, method.lower())
        for path, item in openapi_doc.get("paths", {}).items()
        for method in item
        if method.lower() in HTTP_METHODS
    }


@pytest.fixture(scope="module")
def spec_operations():
    return _operations(yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def served_operations():
    return _operations(object_server.app.openapi()) | _operations(locator_api.app.openapi())


def test_every_served_operation_is_documented(spec_operations, served_operations):
    """An endpoint the apps serve must appear in openapi.yaml."""
    undocumented = served_operations - spec_operations
    assert not undocumented, (
        f"served by an app but missing from openapi.yaml: {sorted(undocumented)}"
    )


def test_every_documented_operation_is_served(spec_operations, served_operations):
    """An operation in openapi.yaml must be served by at least one app."""
    phantom = spec_operations - served_operations
    assert not phantom, (
        f"documented in openapi.yaml but no app serves it: {sorted(phantom)}"
    )
