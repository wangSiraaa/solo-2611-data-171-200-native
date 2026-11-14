"""Assembly engine and CLI.

The work is split into two phases with an explicit boundary:

* **Preflight / eligibility.** The selection is validated and every source is
  modelled by :mod:`pdfpack.model`. Unknown pages, repeated selections and
  unsupported structures are all collected first; any fatal diagnostic means
  nothing is written and the tool exits non-zero with a ``"refused"`` report.
* **Writing.** Only reached when preflight is clean. A mapping from source
  page identity (source id + object number) to output page index is built
  first, then pages are cloned in the requested order, every jump is rewritten
  against that mapping, and the names tree / outline are rebuilt.

Source files are opened read-only; all mutations happen on in-memory objects
owned by this process.
"""

import argparse
import json
import os
import sys

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from .destinations import CONTAINER, NAMED
from .errors import (
    AssemblyError,
    D_TARGET_OMITTED,
    E_DUPLICATE_SELECTION,
    E_PAGE_OUT_OF_RANGE,
    E_SELECTION_PARSE,
    E_SELECTION_SCHEMA,
    E_SOURCE_UNREADABLE,
    E_UNKNOWN_SOURCE,
)
from .model import build_source_model

# Separator between source id and the original name in the output namespace.
# Documented in SCOPE.md; source ids / names containing it are still unique
# because the source id is fixed and validated.
_NAMESPACE_SEP = ":"


# ==========================================================================
# selection validation & preflight
# ==========================================================================

def load_selection(path, diagnostics):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        diagnostics.append({"code": E_SELECTION_PARSE, "path": path,
                            "detail": "selection file not found"})
        return None
    except json.JSONDecodeError as exc:
        diagnostics.append({"code": E_SELECTION_PARSE, "path": path,
                            "detail": f"invalid JSON: {exc}"})
        return None

    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict) \
            or not isinstance(data.get("pages"), list) or not data["pages"]:
        diagnostics.append({
            "code": E_SELECTION_SCHEMA, "path": path,
            "detail": "expected object with non-empty 'sources' map and 'pages' list",
        })
        return data if isinstance(data, dict) else None

    for key, value in data["sources"].items():
        if not isinstance(key, str) or not key or not isinstance(value, str) or not value:
            diagnostics.append({
                "code": E_SELECTION_SCHEMA,
                "detail": f"sources entry {key!r}: {value!r} must be non-empty strings",
            })

    for i, entry in enumerate(data["pages"]):
        if not isinstance(entry, dict) or set(entry.keys()) != {"source", "page"}:
            diagnostics.append({
                "code": E_SELECTION_SCHEMA, "entry_index": i,
                "detail": "each pages entry must be exactly {source, page}",
            })
            continue
        if not isinstance(entry["source"], str) or not isinstance(entry["page"], int) \
                or isinstance(entry["page"], bool) or entry["page"] < 1:
            diagnostics.append({
                "code": E_SELECTION_SCHEMA, "entry_index": i, "entry": entry,
                "detail": "source must be a string and page a positive 1-based integer",
            })
    return data


def preflight(selection, selection_dir, diagnostics):
    """Validate references and build source models. Returns models or raises."""
    models = {}
    known_sources = selection["sources"]

    # 1. source ids used by the selection
    for entry in selection["pages"]:
        if isinstance(entry, dict) and entry.get("source") not in known_sources:
            diagnostics.append({
                "code": E_UNKNOWN_SOURCE, "source": entry.get("source"),
                "known_sources": sorted(known_sources),
            })

    # 2. every referenced (source, page) exists and is unique
    seen_pairs = set()
    for entry in selection["pages"]:
        if not isinstance(entry, dict) or "source" not in entry or "page" not in entry:
            continue
        sid, page_no = entry["source"], entry["page"]
        if sid not in known_sources or not isinstance(page_no, int):
            continue
        pair = (sid, page_no)
        if pair in seen_pairs:
            diagnostics.append({
                "code": E_DUPLICATE_SELECTION, "source": sid,
                "page": page_no,
                "detail": "each source page may be selected at most once",
            })
        seen_pairs.add(pair)

    # 3. open every declared source once (even unused ones are diagnosed)
    for sid, rel_path in known_sources.items():
        full_path = rel_path if os.path.isabs(rel_path) \
            else os.path.normpath(os.path.join(selection_dir, rel_path))
        if not os.path.isfile(full_path):
            diagnostics.append({
                "code": E_SOURCE_UNREADABLE, "source": sid, "path": full_path,
                "detail": "source file does not exist",
            })
            continue
        try:
            model = build_source_model(sid, full_path, diagnostics)
        except Exception as exc:  # pragma: no cover - defensive
            diagnostics.append({
                "code": E_SOURCE_UNREADABLE, "source": sid, "path": full_path,
                "detail": f"failed to parse source: {type(exc).__name__}: {exc}",
            })
            continue
        if model is not None:
            models[sid] = model

    # 4. page-range checks now that page counts are known
    for sid, page_no in sorted(seen_pairs):
        model = models.get(sid)
        if model is None:
            continue
        if page_no > len(model.reader.pages):
            diagnostics.append({
                "code": E_PAGE_OUT_OF_RANGE, "source": sid, "page": page_no,
                "page_count": len(model.reader.pages),
            })

    if diagnostics:
        raise AssemblyError(diagnostics)
    return models


# ==========================================================================
# planning: source identity -> output page mapping
# ==========================================================================

def build_plan(selection, models):
    """Compute the full mapping / report skeleton before touching the writer."""
    # ordered output pages: (source id, 0-based source index)
    output_pages = [(e["source"], e["page"] - 1) for e in selection["pages"]]

    # identity key (source id, source page object number) -> output index
    identity_to_output = {}
    page_mapping = []
    for out_index, (sid, src_index) in enumerate(output_pages):
        page_obj = models[sid].reader.pages[src_index]
        identity = (sid, page_obj.indirect_reference.idnum)
        identity_to_output[identity] = out_index
        page_mapping.append({
            "output_page": out_index + 1,            # 1-based for the report
            "output_index": out_index,
            "source": sid,
            "source_page": src_index + 1,
            "source_page_object_id": page_obj.indirect_reference.idnum,
            "geometry": models[sid].page_geometries[src_index],
        })

    selected_identities = set(identity_to_output)
    return output_pages, identity_to_output, selected_identities, page_mapping


def _is_selected(model, target_idnum, selected_identities):
    return (model.source_id, target_idnum) in selected_identities


# ==========================================================================
# phase 2a: sanitize in-memory source readers before pypdf clones pages
# ==========================================================================

def neutralize_omitted_links(models, selected_identities, report):
    """Strip every jump holder and annotation /P back-pointer before cloning.

    pypdf's deep cloner follows every indirect reference reachable from a
    page, including:

    * the target page inside a link's explicit destination array;
    * the page referenced by an annotation's optional ``/P`` back-pointer.

    Either one drags source pages (selected-but-not-yet-added or fully
    omitted) into the writer as orphan ``/Page`` objects outside the page
    tree.  To make cloning depend on nothing but the page's own content, all
    jump holders (``/A`` and ``/Dest``) and every ``/P`` are removed from the
    in-memory source annotations first.  The annotations themselves and their
    rectangles are untouched.  Jumps are reattached after cloning in
    :func:`_rewrite_links`, using the explicit source-identity -> output-page
    mapping.  Links whose target was omitted stay jump-less and are reported
    as ``target_omitted``.

    The source files on disk are never modified.
    """
    for model in models.values():
        for page in model.reader.pages:
            for annot_ref in page.get("/Annots", []) or []:
                annot = annot_ref.get_object()
                if NameObject("/P") in annot:
                    del annot[NameObject("/P")]
                if NameObject("/A") in annot:
                    del annot[NameObject("/A")]
                if NameObject("/Dest") in annot:
                    del annot[NameObject("/Dest")]

        for link in model.links:
            if link.destination.target_idnum < 0:
                continue  # broken destinations were a fatal preflight error
            if not _is_selected(model, link.destination.target_idnum,
                                selected_identities):
                report["removed_actions"].append(_omitted_record(model, link))


def _omitted_record(model, link):
    dest = link.destination
    record = {
        "diagnostic": D_TARGET_OMITTED,
        "source": model.source_id,
        "source_page": link.page_index + 1,
        "annotation_index": link.annot_index,
        "rect": link.rect,
        "holder": link.holder,
        "original_target": _describe_target(dest),
        "resolution": "jump action removed; annotation rectangle and page "
                      "contents retained; no fallback target substituted",
    }
    return record


def _describe_target(dest):
    if dest.kind == NAMED:
        return {
            "type": "named",
            "name": dest.name,
            "target_source_page": dest.target_page_index + 1,
            "target_page_object_id": dest.target_idnum,
            "view": [str(v) for v in dest.view],
        }
    return {
        "type": "explicit",
        "target_source_page": dest.target_page_index + 1,
        "target_page_object_id": dest.target_idnum,
        "view": [str(v) for v in dest.view],
    }


# ==========================================================================
# phase 2b: clone pages and rewrite surviving links
# ==========================================================================

def write_combined(path, selection, models, plan, report):
    output_pages, identity_to_output, selected_identities, page_mapping = plan
    writer = PdfWriter()

    # clone in output order (interleaving sources is just more add_page calls)
    for sid, src_index in output_pages:
        writer.add_page(models[sid].reader.pages[src_index])
    output_refs = [page.indirect_reference for page in writer.pages]

    _rewrite_links(writer, models, output_pages, identity_to_output,
                   selected_identities, report)
    _rewrite_open_action(writer, models, identity_to_output, selected_identities,
                         report)
    _build_names(writer, models, output_refs, identity_to_output,
                 selected_identities, report)
    _build_outline(writer, models, output_refs, identity_to_output,
                   selected_identities, report)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as handle:
        writer.write(handle)
    return writer


def _rewrite_links(writer, models, output_pages, identity_to_output,
                   selected_identities, report):
    for out_index, (sid, src_index) in enumerate(output_pages):
        model = models[sid]
        out_page = writer.pages[out_index]
        annots = out_page.get("/Annots")
        if annots is None:
            continue
        for link in model.links_on(src_index):
            dest = link.destination
            annot = annots[link.annot_index].get_object()
            if not _is_selected(model, dest.target_idnum, selected_identities):
                # jump holder was stripped pre-clone and is intentionally not
                # reattached; the annotation rectangle still marks the link
                report["links"].append(_link_report(model, link, out_index,
                                                     status="target_omitted"))
                continue

            target_out = identity_to_output[(model.source_id, dest.target_idnum)]
            if dest.kind == NAMED:
                new_d = TextStringObject(
                    f"{model.source_id}{_NAMESPACE_SEP}{dest.name}")
            else:
                new_d = _explicit_dest(writer.pages[target_out].indirect_reference,
                                       dest.view)
            _attach_jump(annot, link.holder, new_d)
            report["links"].append(_link_report(
                model, link, out_index, status="retargeted",
                target_output_page=target_out + 1,
                new_destination=(_namespaced_name(model, dest)
                                 if dest.kind == NAMED else
                                 {"type": "explicit",
                                  "output_page": target_out + 1})))


def _explicit_dest(page_ref, view):
    array = ArrayObject([page_ref])
    for param in view:
        if isinstance(param, IndirectObject):
            array.append(param)
        else:
            array.append(param)
    return array


def _attach_jump(annot, holder, new_d):
    if holder == "/Dest":
        annot[NameObject("/Dest")] = new_d
        return
    action = annot.get("/A")
    if action is None:
        # holder was /A but the object changed shape; create a fresh action
        action = DictionaryObject()
        annot[NameObject("/A")] = action
    else:
        action = action.get_object()
    action[NameObject("/S")] = NameObject("/GoTo")
    action[NameObject("/D")] = new_d


def _namespaced_name(model, dest):
    return {"type": "named",
            "name": f"{model.source_id}{_NAMESPACE_SEP}{dest.name}",
            "original_name": dest.name,
            "source": model.source_id}


def _link_report(model, link, out_index, *, status, target_output_page=None,
                 new_destination=None):
    return {
        "source": model.source_id,
        "source_page": link.page_index + 1,
        "output_page": out_index + 1,
        "annotation_index": link.annot_index,
        "rect": link.rect,
        "holder": link.holder,
        "original_target": _describe_target(link.destination),
        "status": status,
        "target_output_page": target_output_page,
        "new_destination": new_destination,
    }


def _rewrite_open_action(writer, models, identity_to_output,
                         selected_identities, report):
    # At most one /OpenAction per source; each retained source root is mapped
    # only if its destination page survived.
    for model in models.values():
        root = model.reader.trailer["/Root"]
        open_action = root.get("/OpenAction")
        if open_action is None:
            continue
        obj = open_action.get_object() if isinstance(open_action, IndirectObject) \
            else open_action
        if isinstance(obj, DictionaryObject) and obj.get("/S") != "/GoTo":
            continue  # unsupported variant refused during preflight
        raw_d = obj["/D"] if isinstance(obj, DictionaryObject) else obj
        resolved = model.resolver.resolve(raw_d)
        if resolved.target_idnum < 0:
            continue
        if not _is_selected(model, resolved.target_idnum, selected_identities):
            report["removed_actions"].append({
                "diagnostic": D_TARGET_OMITTED,
                "source": model.source_id,
                "feature": "/OpenAction",
                "original_target": _describe_target(resolved),
                "resolution": "document open action dropped; target page omitted",
            })
            continue
        target_out = identity_to_output[(model.source_id, resolved.target_idnum)]
        if isinstance(obj, DictionaryObject):
            new_action = DictionaryObject()
            new_action[NameObject("/S")] = NameObject("/GoTo")
            new_action[NameObject("/D")] = _explicit_dest(
                writer.pages[target_out].indirect_reference, resolved.view)
            writer._root_object[NameObject("/OpenAction")] = writer._add_object(
                new_action)
        else:
            writer._root_object[NameObject("/OpenAction")] = _explicit_dest(
                writer.pages[target_out].indirect_reference, resolved.view)
        report["open_action"] = {
            "source": model.source_id, "output_page": target_out + 1}


# ==========================================================================
# names tree (namespace by source id; collisions cannot overwrite)
# ==========================================================================

def _build_names(writer, models, output_refs, identity_to_output,
                 selected_identities, report):
    pairs = ArrayObject()
    claimed = {}  # output name -> source id, collision guard

    for model in models.values():
        for name, raw in model.resolver.named_map.items():
            resolved = model.resolver.resolve(raw)
            record = {
                "source": model.source_id,
                "original_name": name,
                "original_target": _describe_target(resolved),
            }
            if not _is_selected(model, resolved.target_idnum,
                                selected_identities):
                record.update({
                    "diagnostic": D_TARGET_OMITTED,
                    "status": "omitted",
                    "resolution": "named destination not published; any link "
                                  "using it had its jump action removed",
                })
                report["named_destinations"].append(record)
                continue

            output_name = f"{model.source_id}{_NAMESPACE_SEP}{name}"
            if output_name in claimed and claimed[output_name] != model.source_id:
                # Cannot happen given source-id namespacing; kept as a hard
                # guard so later readers can never overwrite earlier ones.
                raise AssemblyError([{
                    "code": "named_destination_collision",
                    "output_name": output_name,
                    "sources": [claimed[output_name], model.source_id],
                }])
            claimed[output_name] = model.source_id
            target_out = identity_to_output[(model.source_id,
                                             resolved.target_idnum)]
            dest_array = _explicit_dest(output_refs[target_out], resolved.view)
            pairs.append(TextStringObject(output_name))
            pairs.append(dest_array)

            record.update({
                "status": "published",
                "output_name": output_name,
                "target_output_page": target_out + 1,
            })
            report["named_destinations"].append(record)

    if not pairs:
        return
    dests_node = DictionaryObject()
    dests_node[NameObject("/Names")] = pairs
    names_root = DictionaryObject()
    names_root[NameObject("/Dests")] = writer._add_object(dests_node)
    writer._root_object[NameObject("/Names")] = writer._add_object(names_root)


# ==========================================================================
# outline (hierarchical; omitted nodes removed, children reattached)
# ==========================================================================

def _build_outline(writer, models, output_refs, identity_to_output,
                   selected_identities, report):
    root = DictionaryObject()
    root[NameObject("/Type")] = NameObject("/Outlines")
    root[NameObject("/Count")] = NumberObject(0)
    root_ref = writer._add_object(root)
    writer._root_object[NameObject("/Outlines")] = root_ref

    top_refs = []
    for model in models.values():
        # every source gets a non-jumpable root container so retained nodes
        # always have a source root to hang from
        container_ref = writer._add_object(
            _new_outline_dict(writer, f"Source {model.source_id}"))
        container_ref.get_object()[NameObject("/Parent")] = root_ref
        top_refs.append(container_ref)

        child_refs = _plan_children(writer, model, model.outline,
                                    identity_to_output, selected_identities,
                                    report, parent_ref=container_ref,
                                    ancestor_chain=[container_ref])
        _link_children(container_ref, child_refs)

    _link_siblings(top_refs)
    root[NameObject("/First")] = top_refs[0] if top_refs else None
    root[NameObject("/Last")] = top_refs[-1] if top_refs else None
    # clean up explicit nulls when empty
    if not top_refs:
        del root[NameObject("/First")]
        del root[NameObject("/Last")]
    root[NameObject("/Count")] = NumberObject(_visible_count(top_refs))


def _plan_children(writer, model, nodes, identity_to_output,
                   selected_identities, report, parent_ref, ancestor_chain):
    """Return output outline refs for one level, splicing omitted nodes."""
    refs = []
    for node in nodes:
        dest = node.destination
        retained = (
            dest is None
            or dest.kind == CONTAINER
            or (dest.target_idnum >= 0
                and _is_selected(model, dest.target_idnum, selected_identities))
        )
        if not retained:
            # omitted node: delete it, reattach its children to the nearest
            # retained ancestor (the current level's parent chain)
            report["outline"].append({
                "source": model.source_id,
                "title": node.title,
                "original_target": _describe_target(dest),
                "status": "omitted",
                "diagnostic": D_TARGET_OMITTED,
                "reattached_children": len(node.children),
                "resolution": "outline node removed; descendants reattached "
                              "to nearest retained ancestor",
            })
            refs.extend(_plan_children(
                writer, model, node.children, identity_to_output,
                selected_identities, report, parent_ref, ancestor_chain))
            continue

        out_node = _new_outline_dict(writer, node.title)
        out_ref = writer._add_object(out_node)
        if dest is not None and dest.target_idnum >= 0:
            target_out = identity_to_output[(model.source_id, dest.target_idnum)]
            out_node[NameObject("/Dest")] = _explicit_dest(
                output_ref_of(writer, target_out), dest.view)
            report["outline"].append({
                "source": model.source_id, "title": node.title,
                "original_target": _describe_target(dest),
                "status": "retained",
                "target_output_page": target_out + 1,
            })
        else:
            report["outline"].append({
                "source": model.source_id, "title": node.title,
                "status": "retained_container",
                "resolution": "structural node without a destination",
            })

        child_refs = _plan_children(
            writer, model, node.children, identity_to_output,
            selected_identities, report, out_ref,
            ancestor_chain + [out_ref])
        _link_children(out_ref, child_refs)
        refs.append(out_ref)

    _link_siblings(refs)
    return refs


def output_ref_of(writer, out_index):
    return writer.pages[out_index].indirect_reference


def _new_outline_dict(writer, title):
    node = DictionaryObject()
    node[NameObject("/Title")] = TextStringObject(title)
    node[NameObject("/Count")] = NumberObject(0)
    return node


def _link_children(node_ref, child_refs):
    """Wire /First//Last//Count and the children's /Parent pointers."""
    node = node_ref.get_object()
    if child_refs:
        node[NameObject("/First")] = child_refs[0]
        node[NameObject("/Last")] = child_refs[-1]
        node[NameObject("/Count")] = NumberObject(_visible_count(child_refs))
        for ref in child_refs:
            ref.get_object()[NameObject("/Parent")] = node_ref
    else:
        node[NameObject("/Count")] = NumberObject(0)


def _link_siblings(refs):
    for i, ref in enumerate(refs):
        obj = ref.get_object()
        if i > 0:
            obj[NameObject("/Prev")] = refs[i - 1]
        else:
            obj.pop(NameObject("/Prev"), None)
        if i + 1 < len(refs):
            obj[NameObject("/Next")] = refs[i + 1]
        else:
            obj.pop(NameObject("/Next"), None)


def _visible_count(refs):
    # PDF outline /Count counts the number of visible descendants when open.
    total = 0
    for ref in refs:
        total += 1
        obj = ref.get_object()
        if "/First" in obj:
            total += int(obj["/Count"])
    return total


# ==========================================================================
# top level
# ==========================================================================

def assemble(selection_path, output_pdf, report_path):
    diagnostics = []
    selection = load_selection(selection_path, diagnostics)
    if diagnostics:
        _write_report(report_path, _refused_report(selection_path, output_pdf,
                                                   diagnostics))
        raise AssemblyError(diagnostics)

    selection_dir = os.path.dirname(os.path.abspath(selection_path))
    try:
        models = preflight(selection, selection_dir, diagnostics)
    except AssemblyError:
        _write_report(report_path, _refused_report(selection_path, output_pdf,
                                                   diagnostics))
        raise

    plan = build_plan(selection, models)
    report = _empty_report(selection_path, output_pdf)
    report["sources"] = dict(selection["sources"])
    report["page_mapping"] = plan[3]

    neutralize_omitted_links(models, plan[2], report)
    write_combined(output_pdf, selection, models, plan, report)

    report["status"] = "assembled"
    report["page_count"] = len(plan[0])
    report["diagnostics"] = _summarize(report)
    _write_report(report_path, report)
    return report


def _summarize(report):
    counts = {"target_omitted": 0, "retargeted_links": 0,
              "named_destinations_published": 0,
              "named_destinations_omitted": 0,
              "outline_retained": 0, "outline_omitted": 0}
    for link in report["links"]:
        if link["status"] == "target_omitted":
            counts["target_omitted"] += 1
        elif link["status"] == "retargeted":
            counts["retargeted_links"] += 1
    for item in report["named_destinations"]:
        if item["status"] == "published":
            counts["named_destinations_published"] += 1
        else:
            counts["named_destinations_omitted"] += 1
            counts["target_omitted"] += 1
    for item in report["outline"]:
        if item["status"] == "omitted":
            counts["outline_omitted"] += 1
            counts["target_omitted"] += 1
        else:
            counts["outline_retained"] += 1
    counts["removed_actions_total"] = len(report["removed_actions"])
    return counts


def _empty_report(selection_path, output_pdf):
    return {
        "status": "assembled",
        "selection": os.path.abspath(selection_path),
        "output_pdf": os.path.abspath(output_pdf),
        "sources": {},
        "page_count": 0,
        "page_mapping": [],
        "named_destinations": [],
        "links": [],
        "outline": [],
        "removed_actions": [],
        "diagnostics": {},
    }


def _refused_report(selection_path, output_pdf, diagnostics):
    return {
        "status": "refused",
        "selection": os.path.abspath(selection_path),
        "output_pdf": os.path.abspath(output_pdf),
        "page_count": 0,
        "fatal_diagnostics": diagnostics,
        "note": "no combined PDF was written",
    }


def _write_report(path, report):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Assemble selected PDF pages with faithful link/outline "
                    "migration (internal GoTo and named destinations only).")
    parser.add_argument("--selection", required=True,
                        help="path to selection.json")
    parser.add_argument("--output", required=True,
                        help="path of the combined PDF to write")
    parser.add_argument("--report", required=True,
                        help="path of the mapping/diagnostic JSON to write")
    args = parser.parse_args(argv)

    try:
        report = assemble(args.selection, args.output, args.report)
    except AssemblyError as exc:
        print(f"assembly refused with {len(exc.diagnostics)} diagnostic(s); "
              f"see {args.report}", file=sys.stderr)
        for diag in exc.diagnostics:
            print(f"  - {diag.get('code')}: {diag.get('detail', '')}",
                  file=sys.stderr)
        return 1

    print(f"assembled {report['page_count']} pages -> {args.output}")
    print(f"report -> {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
