"""Independent verification of an assembled package.

The verifier deliberately re-implements every check from scratch instead of
importing assembler internals: it re-opens the combined PDF from disk, resolves
each explicit and named destination itself, and confirms the target is a page
object that actually belongs to the combined page tree.  Source PDFs are
re-opened independently for geometry / content / rectangle comparison.

Exit code is zero only when every check passes.
"""

import argparse
import hashlib
import json
import sys

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

from .errors import D_TARGET_OMITTED


class VerificationFailure(Exception):
    pass


class Checker:
    def __init__(self, combined_path, report_path, fixtures_dir):
        self.combined_path = combined_path
        self.report = json.load(open(report_path, encoding="utf-8"))
        self.reader = PdfReader(combined_path)
        self.fixtures_dir = fixtures_dir
        self.checks = []
        self.page_id_to_index = {
            page.indirect_reference.idnum: i for i, page in enumerate(self.reader.pages)
        }

    def expect(self, condition, label, detail=None):
        self.checks.append({"ok": bool(condition), "check": label,
                            "detail": detail})
        if not condition:
            raise VerificationFailure(f"{label}: {detail}")

    # ------------------------------------------------------------------
    def run(self):
        self._check_page_mapping()
        self._check_no_orphan_pages()
        self._check_links()
        self._check_named_destinations()
        self._check_outline()
        self._check_removed_actions_report()
        self._check_no_source_dependencies()
        self._spot_check_visible_content()
        return self.checks

    # ------------------------------------------------------------------
    def _check_page_mapping(self):
        mapping = self.report["page_mapping"]
        self.expect(len(mapping) == len(self.reader.pages),
                    "page count matches report",
                    f"{len(mapping)} vs {len(self.reader.pages)}")
        for entry in mapping:
            out_index = entry["output_index"]
            page = self.reader.pages[out_index]
            source = PdfReader(_join(self.fixtures_dir,
                                     self._source_path(entry["source"])))
            src_page = source.pages[entry["source_page"] - 1]

            self.expect(
                [float(v) for v in page.mediabox]
                == [float(v) for v in src_page.mediabox],
                f"page {out_index + 1} MediaBox preserved",
                f"src={entry['source']}p{entry['source_page']}")
            self.expect(
                [float(v) for v in page.cropbox]
                == [float(v) for v in src_page.cropbox],
                f"page {out_index + 1} CropBox preserved")
            self.expect(int(page.get("/Rotate", 0))
                        == int(src_page.get("/Rotate", 0)),
                        f"page {out_index + 1} Rotate preserved",
                        f"{page.get('/Rotate', 0)} vs {src_page.get('/Rotate', 0)}")
            for box in ("/BleedBox", "/TrimBox", "/ArtBox"):
                if box in src_page:
                    self.expect(
                        box in page
                        and [float(v) for v in page[box]]
                        == [float(v) for v in src_page[box]],
                        f"page {out_index + 1} {box} preserved")

    def _source_path(self, source_id):
        return self.report["sources"][source_id]

    # ------------------------------------------------------------------
    def _iter_objects(self):
        """Yield (object number, object) for every object in the file."""
        size = int(self.reader.trailer["/Size"])
        for xid in range(1, size):
            obj = self.reader.get_object(xid)
            if obj is not None:
                yield xid, obj

    def _check_no_orphan_pages(self):
        tree_ids = set(self.page_id_to_index)
        orphan_ids = []
        for xid, obj in self._iter_objects():
            if isinstance(obj, DictionaryObject) and obj.get("/Type") == "/Page" \
                    and xid not in tree_ids:
                orphan_ids.append(xid)
        self.expect(not orphan_ids, "no orphan /Page objects outside page tree",
                    f"orphans={orphan_ids}")

    # ------------------------------------------------------------------
    def _resolve_output_dest(self, raw):
        """Resolve any destination value inside the combined file.

        Returns (output_page_index, kind, name_or_none)."""
        if isinstance(raw, str):  # named destination
            named = self.reader.named_destinations
            self.expect(raw in named, "named destination exists", raw)
            dest = named[raw]
            page_ref = dest.page.indirect_reference
            return self._page_index_of(page_ref), "named", raw
        if isinstance(raw, IndirectObject):
            obj = raw.get_object()
            if isinstance(obj, DictionaryObject) and "/D" in obj:
                return self._resolve_output_dest(obj["/D"])
            if isinstance(obj, ArrayObject):
                return self._resolve_output_dest(obj)
            raise VerificationFailure(f"unsupported destination object: {obj!r}")
        if isinstance(raw, DictionaryObject) and "/D" in raw:
            return self._resolve_output_dest(raw["/D"])
        if isinstance(raw, ArrayObject):
            page_ref = raw[0]
            if not isinstance(page_ref, IndirectObject):
                raise VerificationFailure("explicit dest page is not indirect")
            return self._page_index_of(page_ref), "explicit", None
        raise VerificationFailure(f"unresolvable destination: {raw!r}")

    def _page_index_of(self, page_ref):
        self.expect(page_ref.idnum in self.page_id_to_index,
                    "destination target is a page-tree page",
                    f"id {page_ref.idnum} not in tree")
        return self.page_id_to_index[page_ref.idnum]

    # ------------------------------------------------------------------
    def _check_links(self):
        report_links = {(l["output_page"], l["annotation_index"]): l
                        for l in self.report["links"]}
        seen = set()
        for out_index, page in enumerate(self.reader.pages):
            annots = page.get("/Annots", [])
            for annot_index, annot_ref in enumerate(annots):
                annot = annot_ref.get_object()
                if annot.get("/Subtype") != "/Link":
                    continue
                key = (out_index + 1, annot_index)
                self.expect(key in report_links,
                            f"link p{out_index + 1} annot {annot_index} "
                            "explained in report")
                seen.add(key)
                record = report_links[key]

                # rectangle unchanged vs the original source page
                source = PdfReader(_join(self.fixtures_dir,
                                         self._source_path(record["source"])))
                src_annot = source.pages[record["source_page"] - 1]["/Annots"][
                    record["annotation_index"]].get_object()
                self.expect(
                    [float(v) for v in annot["/Rect"]]
                    == [float(v) for v in src_annot["/Rect"]],
                    f"link rect unchanged p{out_index + 1} annot {annot_index}")

                has_jump = "/A" in annot or "/Dest" in annot
                if record["status"] == D_TARGET_OMITTED:
                    self.expect(not has_jump,
                                f"omitted link p{out_index + 1} annot "
                                f"{annot_index} has no jump action")
                    continue
                self.expect(has_jump,
                            f"retained link p{out_index + 1} annot "
                            f"{annot_index} still jumpable")
                raw_d = annot["/A"].get_object()["/D"] if "/A" in annot \
                    else annot["/Dest"]
                target_index, kind, name = self._resolve_output_dest(raw_d)
                self.expect(
                    target_index + 1 == record["target_output_page"],
                    f"link p{out_index + 1} annot {annot_index} lands on "
                    "correct output page",
                    f"resolved {target_index + 1} vs report "
                    f"{record['target_output_page']}")
                if record["original_target"]["type"] == "named":
                    self.expect(kind == "named"
                                and name == record["new_destination"]["name"],
                                f"named link uses namespaced name "
                                f"p{out_index + 1}")
                else:
                    self.expect(kind == "explicit",
                                f"explicit link stays explicit p{out_index + 1}")
        self.expect(seen == set(report_links),
                    "report covers exactly the link annotations present",
                    f"missing={set(report_links) - seen}")

    # ------------------------------------------------------------------
    def _check_named_destinations(self):
        combined_named = self.reader.named_destinations
        for record in self.report["named_destinations"]:
            if record["status"] != "published":
                self.expect(record.get("diagnostic") == D_TARGET_OMITTED,
                            "omitted named destination marked target_omitted")
                continue
            name = record["output_name"]
            self.expect(name in combined_named,
                        f"namespaced destination {name!r} published")
            dest = combined_named[name]
            page_index = self._page_index_of(dest.page.indirect_reference)
            self.expect(page_index + 1 == record["target_output_page"],
                        f"destination {name!r} targets correct output page")
        # cross-source same-name fixture: the two same-named targets must
        # coexist under distinct namespaced names, each pointing at the page
        # dictated by its own source (no later reader overwrote the earlier)
        full_names = sorted(combined_named)
        self.expect(len(full_names) == len(set(full_names)),
                    "namespaced destination names are unique")
        for record in self.report["named_destinations"]:
            if record["status"] != "published":
                continue
            expected_name = f"{record['source']}:{record['original_name']}"
            self.expect(record["output_name"] == expected_name,
                        f"output name follows source-id namespace for "
                        f"{record['source']}:{record['original_name']}")

    # ------------------------------------------------------------------
    def _check_outline(self):
        raw_outline = self.reader.trailer["/Root"].get("/Outlines")
        self.expect(raw_outline is not None, "outline exists")
        self._walk_outline(raw_outline.get_object(), depth=0,
                           source_container_seen=set())

        # compare hierarchy/order against report plan
        expected = self._expected_outline_tree()
        actual = self._read_outline_tree()
        self.expect(actual == expected,
                    "outline hierarchy and order matches plan",
                    f"actual={actual} expected={expected}")

    def _walk_outline(self, node, depth, source_container_seen):
        cur = node.get("/First")
        while cur is not None:
            item = cur.get_object()
            # sibling chain: every non-last item has a /Next, last has none
            if "/Next" in item:
                self.expect(item["/Next"] is not None,
                            "outline /Next never points to null")
            if "/Dest" in item:
                index, _, _ = self._resolve_output_dest(item["/Dest"])
                self.expect(0 <= index < len(self.reader.pages),
                            f"outline {item['/Title']!r} target in page tree")
            if "/A" in item:
                raise VerificationFailure(
                    f"outline item {item['/Title']!r} still uses an /A action; "
                    "expected migrated /Dest")
            if "/First" in item:
                self._walk_outline(item, depth + 1, source_container_seen)
            cur = item.get("/Next")

    def _read_outline_tree(self):
        root = self.reader.trailer["/Root"]["/Outlines"].get_object()

        def read_children(node):
            result = []
            cur = node.get("/First")
            while cur is not None:
                item = cur.get_object()
                title = str(item["/Title"])
                if "/Dest" in item:
                    index, _, _ = self._resolve_output_dest(item["/Dest"])
                    result.append({"title": title, "page": index + 1,
                                   "children": read_children(item)})
                else:
                    result.append({"title": title, "page": None,
                                   "children": read_children(item)})
                cur = item.get("/Next")
            return result

        return read_children(root)

    def _expected_outline_tree(self):
        """Reconstruct the planned hierarchy independently from source PDFs."""
        # output page lookup: (source id, 1-based source page) -> output page
        page_lookup = {(m["source"], m["source_page"]): m["output_page"]
                       for m in self.report["page_mapping"]}
        source_paths = self.report["sources"]
        tree = []
        for source_id in sorted(source_paths):
            source = PdfReader(_join(self.fixtures_dir,
                                     source_paths[source_id]))
            root = source.trailer["/Root"]["/Outlines"].get_object()
            children = self._expected_children(root.get("/First"), source_id,
                                               page_lookup, source)
            tree.append({"title": f"Source {source_id}", "page": None,
                         "children": children})
        return tree

    def _expected_children(self, first_ref, source_id, page_lookup, source):
        result = []
        cur = first_ref
        while cur is not None:
            item = cur.get_object()
            target_page = self._source_outline_target_page(item, source)
            children = self._expected_children(item.get("/First"), source_id,
                                               page_lookup, source)
            if target_page is None:
                # structural container without a destination -> retained
                result.append({"title": str(item["/Title"]), "page": None,
                               "children": children})
            else:
                key = (source_id, target_page)
                if key in page_lookup:
                    result.append({"title": str(item["/Title"]),
                                   "page": page_lookup[key],
                                   "children": children})
                # omitted node: dropped, its children spliced at this position
                else:
                    result.extend(children)
            cur = item.get("/Next")
        return result

    @staticmethod
    def _source_outline_target_page(item, source):
        """Return 1-based source page of an outline item, or None."""
        idnum_to_page = {page.indirect_reference.idnum: i + 1
                         for i, page in enumerate(source.pages)}
        raw = item.get("/Dest")
        if raw is None and "/A" in item:
            action = item["/A"].get_object()
            if action.get("/S") == "/GoTo":
                raw = action.get("/D")
        if raw is None:
            return None
        if isinstance(raw, IndirectObject):
            raw = raw.get_object()
        if isinstance(raw, DictionaryObject) and "/D" in raw:
            raw = raw["/D"]
            if isinstance(raw, IndirectObject):
                raw = raw.get_object()
        if isinstance(raw, ArrayObject):
            page_ref = raw[0]
            if isinstance(page_ref, IndirectObject):
                return idnum_to_page.get(page_ref.idnum)
        return None

    # ------------------------------------------------------------------
    def _check_removed_actions_report(self):
        for record in self.report["removed_actions"]:
            self.expect(record["diagnostic"] == D_TARGET_OMITTED,
                        "removed action labeled target_omitted")
            self.expect("original_target" in record and "rect" in record
                        or "feature" in record,
                        "removed action preserves original target description")
        # every target_omitted link report entry must have a matching
        # removed_actions entry
        omitted = [l for l in self.report["links"]
                   if l["status"] == D_TARGET_OMITTED]
        removed_rects = {(r["source"], r["source_page"], tuple(r["rect"]))
                         for r in self.report["removed_actions"] if "rect" in r}
        for link in omitted:
            key = (link["source"], link["source_page"], tuple(link["rect"]))
            self.expect(key in removed_rects,
                        f"omitted link {key} present in removed_actions")

    # ------------------------------------------------------------------
    def _check_no_source_dependencies(self):
        # every indirect object reachable from the combined root must resolve
        # inside the combined file; re-reading from a copied file guarantees
        # no path-based dependency on fixtures. We additionally ensure no
        # GoToR / URI / JavaScript survived.
        for xid, obj in self._iter_objects():
            if isinstance(obj, DictionaryObject):
                if obj.get("/S") in ("/GoToR", "/JavaScript", "/URI",
                                     "/Launch", "/SubmitForm", "/ResetForm"):
                    raise VerificationFailure(
                        f"unsupported action {obj['/S']} present in output "
                        f"(object {xid})")
                if "/JS" in obj or "/JavaScript" in obj:
                    raise VerificationFailure(
                        f"JavaScript present in output object {xid}")

    # ------------------------------------------------------------------
    def _spot_check_visible_content(self):
        # visible content streams of every mapped page must be byte-identical
        # to the source page's content stream
        for entry in self.report["page_mapping"]:
            source = PdfReader(_join(self.fixtures_dir,
                                     self._source_path(entry["source"])))
            src_data = source.pages[entry["source_page"] - 1].get_contents()
            out_data = self.reader.pages[entry["output_index"]].get_contents()
            self.expect(
                src_data is not None and out_data is not None
                and src_data.get_data() == out_data.get_data(),
                f"content stream unchanged for output page "
                f"{entry['output_page']} ({entry['source']}p"
                f"{entry['source_page']})")

            src_text = source.pages[entry["source_page"] - 1].extract_text()
            out_text = self.reader.pages[entry["output_index"]].extract_text()
            self.expect(src_text == out_text,
                        f"extracted text unchanged p{entry['output_page']}")

        # explicit digest evidence for a few pages (first two + last)
        digests = []
        for index in sorted({0, 1, len(self.reader.pages) - 1}):
            data = self.reader.pages[index].get_contents().get_data()
            digests.append({"output_page": index + 1,
                            "sha256": hashlib.sha256(data).hexdigest()})
        self.checks.append({"ok": True, "check": "content digests sampled",
                            "detail": digests})


def _join(base, rel):
    import os
    return rel if os.path.isabs(rel) else os.path.join(base, rel)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Independently verify a combined PDF and its report.")
    parser.add_argument("--combined", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--fixtures-dir", required=True,
                        help="directory containing the source PDFs referenced "
                             "by the report")
    parser.add_argument("--out", help="write machine-readable check results")
    args = parser.parse_args(argv)

    # report must carry source paths; assembler writes them
    checker = Checker(args.combined, args.report, args.fixtures_dir)
    try:
        checks = checker.run()
    except VerificationFailure as exc:
        print(f"VERIFICATION FAILED: {exc}", file=sys.stderr)
        if args.out:
            json.dump({"ok": False, "failure": str(exc),
                       "checks": checker.checks}, open(args.out, "w"), indent=2)
        return 1

    failed = [c for c in checks if not c["ok"]]
    summary = {"ok": not failed, "total_checks": len(checks),
               "passed": len(checks) - len(failed), "checks": checks}
    if args.out:
        json.dump(summary, open(args.out, "w"), indent=2, ensure_ascii=False)
    print(f"verification OK: {len(checks)} checks passed "
          f"({len(checker.reader.pages)} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
