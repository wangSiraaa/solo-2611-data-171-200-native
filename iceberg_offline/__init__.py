"""Offline Iceberg v2 row scanner for the fixture provided in this workspace.

The package intentionally implements only the read path described in README.md:
unpartitioned tables, a flat fixed schema, and data/position-delete/equality-delete
files.  It never talks to a catalog or to S3.
"""

from .errors import (
    IcebergError,
    ResolutionError,
    InvalidTableError,
    UnsupportedError,
    RowNotFoundError,
)

__all__ = [
    "IcebergError",
    "ResolutionError",
    "InvalidTableError",
    "UnsupportedError",
    "RowNotFoundError",
]
