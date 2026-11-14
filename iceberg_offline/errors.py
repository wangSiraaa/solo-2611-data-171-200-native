"""Exception hierarchy used by the offline scanner."""


class IcebergError(Exception):
    """Base class for all expected scanner failures."""


class ResolutionError(IcebergError):
    """A URI/path could not be resolved through plan.json safely."""


class InvalidTableError(IcebergError):
    """Metadata or data files are internally inconsistent."""


class UnsupportedError(IcebergError):
    """The table uses a structure outside the supported subset.

    The message always starts with ``unsupported:`` so the CLI and tests can
    distinguish "we refuse to guess" from an ordinary malformed input.
    """


class RowNotFoundError(IcebergError):
    """The requested data file / physical row is not visible in the snapshot."""
