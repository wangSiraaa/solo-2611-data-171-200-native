"""Package exports."""

from .api import ExpandResult, expand_records
from .engine import Contract, TemplatePlan, parse_template, render_record
from .errors import (
    DocLocation,
    MissingLoopValueError,
    MissingValueError,
    StructureError,
    TemplateError,
    UnknownFieldError,
    UnpairedMarkerError,
)

__version__ = "0.1.0"

__all__ = [
    "Contract",
    "DocLocation",
    "ExpandResult",
    "MissingLoopValueError",
    "MissingValueError",
    "StructureError",
    "TemplateError",
    "TemplatePlan",
    "UnknownFieldError",
    "UnpairedMarkerError",
    "__version__",
    "expand_records",
    "parse_template",
    "render_record",
]
