"""Identity maintenance for cloned table rows.

A cloned ``w:tr`` repeats every child verbatim, which would duplicate bookmark
IDs/names (Word requires ``w:bookmarkStart``/``w:bookmarkEnd`` pairs to share an
ID and names to be unique within a part) and drawing object IDs
(``wp:docPr/@id`` must be unique).  This module assigns fresh, unused
identities while keeping start/end pairs matched.  Image relationships are
never touched, so cloned drawings may legitimately share one ``r:embed``.
"""

from __future__ import annotations

import copy
from typing import Optional

from lxml import etree

from .errors import StructureError
from .ooxml import W, WP


def collect_identities(root: etree._Element) -> tuple[set[int], set[str], set[int]]:
    """Collect bookmark ids, bookmark names and docPr ids used in ``root``."""
    bookmark_ids: set[int] = set()
    bookmark_names: set[str] = set()
    docpr_ids: set[int] = set()
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if el.tag in (W("bookmarkStart"), W("bookmarkEnd")):
            ident = el.get(W("id"))
            if ident is not None and ident.lstrip("-").isdigit():
                bookmark_ids.add(int(ident))
            name = el.get(W("name"))
            if el.tag == W("bookmarkStart") and name:
                bookmark_names.add(name)
        elif el.tag == WP("docPr"):
            ident = el.get("id")
            if ident is not None and ident.lstrip("-").isdigit():
                docpr_ids.add(int(ident))
    return bookmark_ids, bookmark_names, docpr_ids


def _name_taken(name: str, used: set[str]) -> bool:
    folded = name.casefold()
    return any(existing.casefold() == folded for existing in used)


def _pairing_signature(root: etree._Element) -> tuple:
    """Order/pairing signature that is invariant to renumbering and renames."""
    depth: dict[Optional[str], int] = {}
    ordinals: list[int] = []
    signature = []
    for el in root.iter():
        if el.tag == W("bookmarkStart"):
            ordinals.append(len(ordinals))
            depth[el.get(W("id"))] = ordinals[-1]
            signature.append(("S", ordinals[-1]))
        elif el.tag == W("bookmarkEnd"):
            ordinal = depth.pop(el.get(W("id")), None)
            signature.append(("E", ordinal))
    if depth:
        signature.append(("UNMATCHED", len(depth)))
    return tuple(signature)


def reidentify_clone(clone: etree._Element, used_ids: set[int],
                     used_names: set[str], used_docpr: set[int],
                     suffix: str) -> dict:
    """Give every identity inside ``clone`` a unique value, in place.

    ``used_*`` sets are the currently allocated values across the document and
    are updated with the new assignments.  Raises :class:`StructureError`
    (without location) if a bookmark pair is split across the row boundary.
    """
    before = _pairing_signature(clone)
    next_bookmark = max(used_ids | {0}) + 1
    next_docpr = max(used_docpr | {0}) + 1
    bookmark_remap: dict[str, str] = {}
    docpr_remap: dict[str, str] = {}

    name_ordinal = 1
    for el in clone.iter(W("bookmarkStart")):
        old = el.get(W("id"))
        new_id = next_bookmark
        next_bookmark += 1
        base = el.get(W("name")) or "bookmark"
        new_name = base
        while _name_taken(new_name, used_names):
            new_name = f"{base}_{suffix}_{name_ordinal}"
            name_ordinal += 1
        used_ids.add(new_id)
        used_names.add(new_name)
        if old is not None:
            bookmark_remap[old] = str(new_id)
        el.set(W("id"), str(new_id))
        el.set(W("name"), new_name)

    for el in clone.iter(W("bookmarkEnd")):
        old = el.get(W("id"))
        new = bookmark_remap.get(old) if old is not None else None
        if new is None:
            raise StructureError(
                "bookmark end inside cloned row has no matching start in that row")
        el.set(W("id"), new)

    for el in clone.iter(WP("docPr")):
        old = el.get("id")
        new = str(next_docpr)
        next_docpr += 1
        if old is not None:
            docpr_remap[old] = new
        el.set("id", new)
        used_docpr.add(int(new))

    if _pairing_signature(clone) != before:
        raise StructureError("bookmark pairing changed while cloning row")
    return {"bookmark_ids": bookmark_remap, "docpr_ids": docpr_remap}


def clone_element(el: etree._Element) -> etree._Element:
    return copy.deepcopy(el)
