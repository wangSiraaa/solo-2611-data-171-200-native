"""In-memory model of one source PDF, built during preflight.

Everything that can disqualify a source (encryption, forms, JavaScript,
remote GoToR, malformed destinations, ...) is detected here, before any page
is copied.  Link annotations and outline items are captured as structured
records keyed by source page object identity so that assembly never has to
re-interpret raw PDF objects while writing.
"""

from dataclasses import dataclass, field

from pypdf import PdfReader
from pypdf.generic import DictionaryObject, IndirectObject, NameObject

from .destinations import (
    DestinationResolver,
    ResolvedDestination,
    build_named_destination_map,
)
from .errors import (
    E_ACROFORM,
    E_DOCUMENT_LEVEL_UNSUPPORTED,
    E_UNSUPPORTED_ACTION,
    E_UNSUPPORTED_ANNOTATION,
    E_UNSUPPORTED_NAMES_TREE,
)

# Action types that constitute ordinary internal navigation.  Everything else
# (URI, Launch, JavaScript, GoToR remote, Rendition, ...) is out of scope.
_GOTO = NameObject("/GoTo")
_SUPPORTED_ACTION_SUBTYPES = {"/GoTo"}
_LINK = NameObject("/Link")
_WIDGET = NameObject("/Widget")


@dataclass
class LinkRef:
    """One jumpable link annotation on a source page."""

    page_index: int                 # 0-based index inside the source
    page_idnum: int                 # source page object identity
    annot_index: int                # index in the page's /Annots array
    rect: list                      # original rectangle, preserved verbatim
    raw_destination: object         # raw /D (array, name, ...) for the report
    destination: ResolvedDestination
    holder: str                     # "/A" (action) or "/Dest" (direct)


@dataclass
class OutlineNode:
    """One source outline item, with children (which may be empty)."""

    title: str
    destination: ResolvedDestination | None  # None => structural container
    children: list = field(default_factory=list)


@dataclass
class SourceModel:
    source_id: str
    path: str
    reader: PdfReader
    resolver: DestinationResolver
    links: list                    # list[LinkRef]
    outline: list                  # list[OutlineNode] (top level)
    page_geometries: list          # geometry dict per source page

    # convenience ----------------------------------------------------------
    def links_on(self, page_index):
        return [link for link in self.links if link.page_index == page_index]


def build_source_model(source_id, path, diagnostics):
    """Open one source and run every preflight check, appending diagnostics."""
    reader = PdfReader(path)
    if reader.is_encrypted:
        diagnostics.append({
            "code": "encrypted_pdf_unsupported", "source": source_id,
            "detail": "encrypted sources are out of scope",
        })
        # Nothing else can be inspected reliably; return a bare model.
        return None

    named_map, name_diags = build_named_destination_map(reader)
    for diag in name_diags:
        diag["source"] = source_id
        diagnostics.append(diag)

    resolver = DestinationResolver(reader, named_map, source_id)

    root = reader.trailer["/Root"]
    _check_document_level(root, source_id, diagnostics)

    links = []
    page_geometries = []
    for page_index, page in enumerate(reader.pages):
        page_geometries.append(_geometry(page))
        links.extend(_scan_page_annotations(
            page, page_index, resolver, source_id, diagnostics))

    outline = _scan_outline(root, resolver, source_id, diagnostics)

    # Named destinations themselves must point at real pages.
    for name, raw in named_map.items():
        resolved = resolver.resolve(raw)
        if resolved.diagnostic is not None:
            diag = dict(resolved.diagnostic)
            diag.setdefault("source", source_id)
            diag.setdefault("name", name)
            diagnostics.append(diag)

    return SourceModel(
        source_id=source_id, path=path, reader=reader, resolver=resolver,
        links=links, outline=outline, page_geometries=page_geometries,
    )


# --------------------------------------------------------------------------
# document-level checks
# --------------------------------------------------------------------------

def _check_document_level(root, source_id, diagnostics):
    if "/AcroForm" in root:
        diagnostics.append({
            "code": E_ACROFORM, "source": source_id,
            "detail": "interactive forms are out of scope",
        })

    if "/AA" in root:
        diagnostics.append({
            "code": E_DOCUMENT_LEVEL_UNSUPPORTED, "source": source_id,
            "feature": "/AA",
            "detail": "document-level additional actions (often JavaScript) "
                      "are out of scope",
        })

    open_action = root.get("/OpenAction")
    if open_action is not None:
        obj = open_action.get_object() if isinstance(open_action, IndirectObject) else open_action
        if isinstance(obj, DictionaryObject) and obj.get("/S"):
            if obj["/S"] != _GOTO:
                diagnostics.append({
                    "code": E_UNSUPPORTED_ACTION, "source": source_id,
                    "feature": "/OpenAction", "action": str(obj["/S"]),
                    "detail": "only internal /GoTo open actions could be migrated",
                })
        # an array /OpenAction is an explicit destination and handled by
        # assembly; it cannot carry JavaScript.

    names_root = root.get("/Names")
    if names_root is not None:
        for key in names_root.get_object().keys():
            if key == "/Dests":
                continue
            if key == "/JavaScript":
                diagnostics.append({
                    "code": E_DOCUMENT_LEVEL_UNSUPPORTED, "source": source_id,
                    "feature": "/Names/JavaScript",
                    "detail": "document-level JavaScript is out of scope",
                })
            else:
                diagnostics.append({
                    "code": E_UNSUPPORTED_NAMES_TREE, "source": source_id,
                    "name_tree": str(key),
                    "detail": "only the /Dests name tree is in scope",
                })

    if "/JavaScript" in root:
        diagnostics.append({
            "code": E_DOCUMENT_LEVEL_UNSUPPORTED, "source": source_id,
            "feature": "/JavaScript",
            "detail": "document-level JavaScript is out of scope",
        })


# --------------------------------------------------------------------------
# annotations
# --------------------------------------------------------------------------

def _scan_page_annotations(page, page_index, resolver, source_id, diagnostics):
    links = []
    annots = page.get("/Annots")
    if annots is None:
        return links

    for annot_index, annot_ref in enumerate(annots):
        annot = annot_ref.get_object() if isinstance(annot_ref, IndirectObject) else annot_ref
        subtype = annot.get("/Subtype")
        if subtype == _WIDGET:
            diagnostics.append({
                "code": E_ACROFORM, "source": source_id,
                "page": page_index + 1,
                "detail": "widget annotation implies an interactive form",
            })
            continue
        if subtype != _LINK:
            diagnostics.append({
                "code": E_UNSUPPORTED_ANNOTATION, "source": source_id,
                "page": page_index + 1, "subtype": str(subtype),
                "detail": "only /Link annotations are in scope",
            })
            continue

        rect = [float(v) for v in annot.get("/Rect", [])]
        if "/A" in annot:
            action = annot["/A"].get_object()
            _scan_action_chain(action, source_id, page_index, diagnostics)
            if action.get("/S") == _GOTO and "/D" in action:
                resolved = resolver.resolve(action["/D"])
                _absorb(resolved, source_id, diagnostics)
                links.append(LinkRef(
                    page_index, page.indirect_reference.idnum, annot_index,
                    rect, action["/D"], resolved, "/A",
                ))
        elif "/Dest" in annot:
            resolved = resolver.resolve(annot["/Dest"])
            _absorb(resolved, source_id, diagnostics)
            links.append(LinkRef(
                page_index, page.indirect_reference.idnum, annot_index,
                rect, annot["/Dest"], resolved, "/Dest",
            ))
        # A /Link without /A or /Dest is not jumpable; nothing to migrate.
    return links


def _scan_action_chain(action, source_id, page_index, diagnostics):
    seen = set()
    stack = [action]
    while stack:
        current = stack.pop()
        if isinstance(current, IndirectObject):
            if current.idnum in seen:
                break
            seen.add(current.idnum)
            current = current.get_object()
        subtype = current.get("/S")
        if subtype is not None and str(subtype) not in _SUPPORTED_ACTION_SUBTYPES:
            diagnostics.append({
                "code": E_UNSUPPORTED_ACTION, "source": source_id,
                "page": page_index + 1, "action": str(subtype),
                "detail": {
                    "/GoToR": "remote GoTo actions are out of scope",
                    "/URI": "URI actions are out of scope",
                    "/Launch": "launch actions are out of scope",
                    "/JavaScript": "JavaScript actions are out of scope",
                    "/Named": "named viewer actions are out of scope",
                    "/Rendition": "media rendition actions are out of scope",
                    "/Sound": "sound actions are out of scope",
                    "/Movie": "movie actions are out of scope",
                    "/SubmitForm": "form actions are out of scope",
                    "/ResetForm": "form actions are out of scope",
                    "/ImportData": "form actions are out of scope",
                }.get(str(subtype), "action type is out of scope"),
            })
        for nxt in current.get("/Next", []) or []:
            stack.append(nxt)


def _absorb(resolved, source_id, diagnostics):
    if resolved.diagnostic is not None:
        diag = dict(resolved.diagnostic)
        diag.setdefault("source", source_id)
        diagnostics.append(diag)


# --------------------------------------------------------------------------
# outline
# --------------------------------------------------------------------------

def _scan_outline(root, resolver, source_id, diagnostics):
    outlines_root = root.get("/Outlines")
    if outlines_root is None:
        return []
    outlines_root = outlines_root.get_object()
    seen = set()
    return _walk_outline_siblings(outlines_root.get("/First"), resolver,
                                  source_id, diagnostics, seen)


def _walk_outline_siblings(first_ref, resolver, source_id, diagnostics, seen):
    nodes = []
    current = first_ref
    while current is not None:
        if isinstance(current, IndirectObject):
            if current.idnum in seen:
                diagnostics.append({
                    "code": E_DOCUMENT_LEVEL_UNSUPPORTED, "source": source_id,
                    "feature": "/Outlines",
                    "detail": "cycle detected while walking outline",
                })
                break
            seen.add(current.idnum)
            item = current.get_object()
        else:
            item = current

        destination = None
        if "/Dest" in item:
            destination = resolver.resolve(item["/Dest"])
            _absorb(destination, source_id, diagnostics)
        elif "/A" in item:
            action = item["/A"].get_object()
            _scan_action_chain(action, source_id, None, diagnostics)
            if action.get("/S") == _GOTO and "/D" in action:
                destination = resolver.resolve(action["/D"])
                _absorb(destination, source_id, diagnostics)
            elif action.get("/S") is not None:
                destination = None  # unsupported action already diagnosed

        node = OutlineNode(title=str(item.get("/Title", "")),
                           destination=destination,
                           children=_walk_outline_siblings(
                               item.get("/First"), resolver, source_id,
                               diagnostics, seen))
        nodes.append(node)
        current = item.get("/Next")
    return nodes


def _geometry(page):
    def explicit_box(name):
        value = page.get(name)
        if value is None:
            return None
        return [float(v) for v in value]

    def effective_box(attr):
        # pypdf falls back to MediaBox for missing boxes; record the value a
        # reader will actually use, plus whether it was explicitly declared
        value = getattr(page, attr)
        return [float(v) for v in value]

    return {
        "media_box": effective_box("mediabox"),
        "crop_box": effective_box("cropbox"),
        "crop_box_explicit": page.get("/CropBox") is not None,
        "bleed_box": explicit_box("/BleedBox"),
        "trim_box": explicit_box("/TrimBox"),
        "art_box": explicit_box("/ArtBox"),
        "rotation": int(page.get("/Rotate", 0)),
    }
