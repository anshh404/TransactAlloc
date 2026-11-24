"""Shared pytest fixtures for the report-style tests at the repository root.

``test_deadlock.py`` and ``test_defrag.py`` are named in the project synopsis
and are written as simple functions taking a ``conn`` argument. Providing the
fixture here makes them runnable with a plain ``py -3 -m pytest -q`` from the
repository root as well as directly as scripts.
"""

import pytest

import db


@pytest.fixture
def conn():
    """An autocommit connection, as the deadlock/demo scripts require.

    The scripts open explicit ``conn.transaction()`` blocks and rely on a
    raised exception being a real ROLLBACK. On a non-autocommit connection a
    preceding SELECT would silently turn those blocks into savepoints, which
    is not the behaviour the tests claim to verify.
    """
    with db.connect(autocommit=True) as connection:
        yield connection
