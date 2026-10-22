"""Shared pytest setup: mark the end-to-end tests slow."""

import pytest


def pytest_collection_modifyitems(items):
    """Mark tests that run molt.py in a subprocess (through the env fixture) as slow.

    ./check.sh --fast runs pytest with -m "not slow" to skip them. test_install.py marks
    itself.
    """
    for item in items:
        if "env" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.slow)
