"""Shared fixtures."""

import pytest

from surveycto_python_dict.pii import PiiConfig, build_analyzer


@pytest.fixture(scope="session")
def analyzer():
    """Presidio with the default models; loading them takes seconds, so once per run."""
    return build_analyzer(PiiConfig())
