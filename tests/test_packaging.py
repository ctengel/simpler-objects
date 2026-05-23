"""Smoke tests for the package's console scripts and metadata."""

import importlib
import pathlib
import tomllib

import pytest


PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def _load_scripts() -> dict[str, str]:
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["scripts"]


@pytest.mark.parametrize("name,target", list(_load_scripts().items()))
def test_console_script_targets_resolve(name, target):
    """Every [project.scripts] entry must point at an importable callable."""
    module_name, _, attr = target.partition(":")
    assert module_name and attr, f"{name} target {target!r} is malformed"
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attr)), (
        f"{name} -> {target}: attribute is not callable"
    )
