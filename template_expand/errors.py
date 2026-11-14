"""Error types shared by the template-expand package.

Every structured failure carries a :class:`DocLocation` so that callers can
report *where* a problem happened without parsing an English message.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass(frozen=True)
class DocLocation:
    """A stable, human-readable position inside the main document body.

    ``part`` is always ``word/document.xml`` for the supported scope.  Body
    children are numbered by their index among ``w:body`` children (paragraphs,
    tables and the trailing ``w:sectPr`` all count), which keeps the location
    meaningful even before table rows are expanded.
    """

    part: str = "word/document.xml"
    body_index: Optional[int] = None
    paragraph_index: Optional[int] = None
    table_index: Optional[int] = None
    row_index: Optional[int] = None
    cell_index: Optional[int] = None
    char_offset: Optional[int] = None

    def describe(self) -> str:
        bits = [self.part]
        if self.table_index is not None:
            bits.append(f"tbl#{self.table_index}")
        if self.row_index is not None:
            bits.append(f"row#{self.row_index}")
        if self.cell_index is not None:
            bits.append(f"cell#{self.cell_index}")
        if self.body_index is not None:
            bits.append(f"body#{self.body_index}")
        if self.paragraph_index is not None:
            bits.append(f"p#{self.paragraph_index}")
        if self.char_offset is not None:
            bits.append(f"off#{self.char_offset}")
        return " ".join(bits)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


class TemplateError(Exception):
    """Base class for expected, reportable template problems."""

    code = "template_error"

    def __init__(self, message: str, *, location: Optional[DocLocation] = None,
                 record_id: Optional[str] = None, field: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.location = location
        self.record_id = record_id
        self.field = field

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.location is not None:
            out["location"] = self.location.describe()
            out["location_detail"] = self.location.to_dict()
        if self.record_id is not None:
            out["record_id"] = self.record_id
        if self.field is not None:
            out["field"] = self.field
        return out


class StructureError(TemplateError):
    """Malformed or unsupported template structure (expression scope)."""

    code = "unsupported_structure"


class UnpairedMarkerError(TemplateError):
    """A ``{{#items}}`` / ``{{/items}}`` pair is missing or misordered."""

    code = "unpaired_marker"


class UnknownFieldError(TemplateError):
    """A placeholder references a field the template contract does not declare."""

    code = "unknown_field"


class MissingValueError(TemplateError):
    """A declared field is absent from (or non-scalar in) the record."""

    code = "missing_value"


class MissingLoopValueError(TemplateError):
    """The record has no list value for the declared loop."""

    code = "missing_loop_value"


class ScopeError(TemplateError):
    """A field that is only meaningful inside the loop used outside it."""

    code = "out_of_scope_field"
