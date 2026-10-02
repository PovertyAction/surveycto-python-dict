"""Shared test setup.

Live tests (``@pytest.mark.live``) hit the real server named in ``.env`` and run only
with ``SCTO_LIVE=1``. They assert on structure derived from a pull (counts, column-name
sets, booleans), never on a value: a failing assert prints its operands, and the live
form holds real submissions.
"""

import os

import pytest
from dotenv import load_dotenv


def pytest_collection_modifyitems(config, items):
    if os.environ.get("SCTO_LIVE") == "1":
        load_dotenv()
        return
    skip = pytest.mark.skip(reason="live test; set SCTO_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
