"""Backward-compatible alias for :mod:`deadlock`.

The canonical module is ``deadlock.py`` (the name used in the project
synopsis). ``LiveDeadlock`` here is the same class object, so existing
imports and the engine's type checks continue to work unchanged.
"""

from deadlock import LiveDeadlock  # noqa: F401

__all__ = ["LiveDeadlock"]
