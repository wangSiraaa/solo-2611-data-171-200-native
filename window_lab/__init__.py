"""Deterministic window-frame explanation tool for a single PostgreSQL table.

The package derives window frames itself from raw rows and the parsed window
definition.  PostgreSQL is used only to load data and, in ``verify``, to
cross-check results that the explanation declares deterministic.
"""

__version__ = "1.0.0"
