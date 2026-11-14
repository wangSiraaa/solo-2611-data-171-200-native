"""Resolution of PDF destination objects inside one source document.

A *destination* can appear in four shapes (PDF 1.7 spec, table 151):

1. an explicit destination array ``[page, /Fit, ...]``;
2. a name string resolved through the document's name tree
   (``/Root /Names /Dests``) or the legacy ``/Root /Dests`` dictionary;
3. an indirect reference to a destination dictionary carrying ``/D``;
4. such a destination dictionary inline.

This module turns any of them into a :class:`ResolvedDestination` whose target
is identified by **source page identity** (the page object's object number in
that source), never by numeric page index.  Numeric page numbers are only
derived afterwards for human-readable reports.
"""

from dataclasses import dataclass, field

from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    DictionaryObject,
    IndirectObject,
    TextStringObject,
)

from .errors import E_BROKEN_DESTINATION, E_NAMED_DEST_UNRESOLVABLE

EXPLICIT = "explicit"
NAMED = "named"
CONTAINER = "container"


@dataclass
class ResolvedDestination:
    """Outcome of resolving one raw destination value."""

    kind: str                      # EXPLICIT or NAMED
    target_idnum: int              # object number of the target page in its source
    target_page_index: int         # 0-based index of that page in the source
    view: list = field(default_factory=list)   # raw view parameters after the page ref
    name: str | None = None        # original name string for NAMED destinations
    diagnostic: dict | None = None # set instead of raising when unresolvable


def build_named_destination_map(reader):
    """Return ``(name -> raw destination)`` plus structural diagnostics.

    Walks both the modern name tree (``/Names /Dests``, including ``/Kids``)
    and the legacy ``/Dests`` dictionary.  A duplicate name inside one source
    is malformed PDF and reported rather than letting one entry silently
    overwrite the other.
    """
    names = {}
    diagnostics = []
    root = reader.trailer["/Root"]

    names_root = root.get("/Names")
    if names_root is not None:
        dests_node = names_root.get_object().get("/Dests")
        if dests_node is not None:
            stack = [dests_node.get_object()]
            while stack:
                node = stack.pop()
                pairs = node.get("/Names")
                if pairs is not None:
                    for i in range(0, len(pairs) - 1, 2):
                        name = str(pairs[i])
                        raw = pairs[i + 1]
                        if name in names:
                            diagnostics.append({
                                "code": "duplicate_named_destination",
                                "name": name,
                                "detail": "name tree defines the same name twice "
                                          "in one source; target is ambiguous",
                            })
                        names[name] = raw
                stack.extend(kid.get_object() for kid in node.get("/Kids", []))

    legacy = root.get("/Dests")
    if legacy is not None:
        for name, raw in legacy.get_object().items():
            name = str(name)
            if name in names:
                diagnostics.append({
                    "code": "duplicate_named_destination",
                    "name": name,
                    "detail": "name present in both /Names/Dests and legacy /Dests",
                })
            names[name] = raw

    return names, diagnostics


class DestinationResolver:
    """Resolves raw destinations within a single :class:`PdfReader`."""

    def __init__(self, reader, named_map, source_id):
        self.reader = reader
        self.named_map = named_map
        self.source_id = source_id
        # page object identity -> 0-based page index
        self.page_index_by_idnum = {
            page.indirect_reference.idnum: i for i, page in enumerate(reader.pages)
        }

    def resolve(self, raw, *, via_name=None):
        if isinstance(raw, (TextStringObject, ByteStringObject)):
            return self._resolve_named(str(raw))
        if isinstance(raw, IndirectObject):
            obj = raw.get_object()
            if isinstance(obj, DictionaryObject) and "/D" in obj:
                return self.resolve(obj["/D"], via_name=via_name)
            if isinstance(obj, ArrayObject):
                return self._resolve_explicit(obj, via_name=via_name)
            return self._broken(raw, via_name)
        if isinstance(raw, DictionaryObject) and "/D" in raw:
            return self.resolve(raw["/D"], via_name=via_name)
        if isinstance(raw, ArrayObject):
            return self._resolve_explicit(raw, via_name=via_name)
        return self._broken(raw, via_name)

    def _resolve_named(self, name):
        raw = self.named_map.get(name)
        if raw is None:
            return ResolvedDestination(
                kind=NAMED, target_idnum=-1, target_page_index=-1, name=name,
                diagnostic={
                    "code": E_NAMED_DEST_UNRESOLVABLE,
                    "source": self.source_id,
                    "name": name,
                    "detail": "GoTo name is not defined in /Names/Dests or /Dests",
                },
            )
        inner = self.resolve(raw, via_name=name)
        inner.kind = NAMED
        inner.name = name
        return inner

    def _resolve_explicit(self, arr, via_name):
        if not arr:
            return self._broken(arr, via_name)
        page_ref = arr[0]
        if isinstance(page_ref, IndirectObject):
            idnum = page_ref.idnum
            page_obj = page_ref.get_object()
        else:  # spec allows an inline page dictionary in theory
            page_obj = page_ref
            idnum = next(
                (pid for pid, p in self._page_objects() if p is page_obj), -1
            )
        if not isinstance(page_obj, DictionaryObject) or page_obj.get("/Type") != "/Page":
            return self._broken(arr, via_name)
        index = self.page_index_by_idnum.get(idnum, -1)
        if index < 0:
            return self._broken(arr, via_name)
        return ResolvedDestination(
            kind=NAMED if via_name is not None else EXPLICIT,
            target_idnum=idnum,
            target_page_index=index,
            view=list(arr[1:]),
            name=via_name,
        )

    def _page_objects(self):
        for i, page in enumerate(self.reader.pages):
            yield page.indirect_reference.idnum, page

    def _broken(self, raw, via_name):
        return ResolvedDestination(
            kind=NAMED if via_name is not None else EXPLICIT,
            target_idnum=-1, target_page_index=-1, name=via_name,
            diagnostic={
                "code": E_BROKEN_DESTINATION,
                "source": self.source_id,
                "via_name": via_name,
                "raw": _safe_repr(raw),
                "detail": "destination does not resolve to a page of this source",
            },
        )


def _safe_repr(raw, limit=120):
    text = repr(raw)
    return text if len(text) <= limit else text[:limit] + "..."
