"""
Shared test hooks.

main.py's slowapi limiter keeps in-memory counters for the life of the
process. Every test starts with empty buckets so endpoint tests stay
independent of order; the limits themselves are exercised in
test_F1_rate_limit.py.
"""

import sys

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    main = sys.modules.get("main")
    limiter = getattr(main, "limiter", None) if main else None
    if limiter is not None:
        limiter.reset()
    yield
