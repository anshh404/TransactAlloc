"""Backward-compatible alias for :mod:`defrag`.

The canonical module is ``defrag.py`` (the name used in the project synopsis).
Imports of ``frag`` are kept working so older scripts and existing tests keep
running against the exact same objects.
"""

from defrag import (  # noqa: F401
    Hole,
    allocated_blocks,
    apply_defrag,
    cluster_status,
    defrag_plan,
    free_holes,
    total_free,
)

__all__ = [
    "Hole",
    "cluster_status",
    "free_holes",
    "allocated_blocks",
    "total_free",
    "defrag_plan",
    "apply_defrag",
]
