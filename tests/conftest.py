"""Test-suite logging defaults.

The server modules call ``logging_config.configure()`` at import time with the
INFO default, which spams stderr during pytest. This fixture rebinds the level
to WARNING for every test (caplog can still raise it locally with
``caplog.set_level(...)``).
"""

import pytest

from simpler_objects.logging_config import configure


@pytest.fixture(autouse=True, scope="session")
def _quiet_logging():
    configure(level="WARNING")
    yield
