"""End-to-end and preflight tests for the pdfpack assembler/verifier.

Run with: .venv/bin/python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject

from pdfpack.assemble import assemble
from pdfpack.errors import AssemblyError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from pdf_helpers import (  # noqa: E402
    make_direct_dest_link_pdf,
    make_document_javascript_pdf,
    make_encrypted_pdf,
    make_form_pdf,
    make_link_pdf,
)

FIXTURES = os.path.join(ROOT, "fixtures")


class _TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pdfpack-")
        self.out_pdf = os.path.join(self.tmp, "combined.pdf")
        self.report = os.path.join(self.tmp, "report.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_selection(self, sources, pages):
        path = os.path.join(self.tmp, "selection.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"sources": sources, "pages": pages}, handle)
        return path

    def assemble_fixture(self, pages):
        return self.write_selection(
            {"A": os.path.join(FIXTURES, "source-A.pdf"),
             "B": os.path.join(FIXTURES, "source-B.pdf")}, pages)

    def read_report(self):
        with open(self.report, encoding="utf-8") as handle:
            return json.load(handle)

    def assert_refused(self, selection_path, expected_codes):
        with self.assertRaises(AssemblyError) as ctx:
            assemble(selection_path, self.out_pdf, self.report)
        codes = {d["code"] for d in ctx.exception.diagnostics}
        for code in expected_codes:
            self.assertIn(code, codes, f"missing {code}; got {sorted(codes)}")
        self.assertFalse(os.path.exists(self.out_pdf),
                         "no combined PDF may be written on refusal")
        report = self.read_report()
        self.assertEqual(report["status"], "refused")
        return ctx.exception.diagnostics


class FixtureHappyPathTests(_TempCase):
    SELECTION = [
        {"source": "A", "page": 1}, {"source": "B", "page": 2},
        {"source": "A", "page": 3}, {"source": "B", "page": 1},
    ]

    def setUp(self):
        super().setUp()
        path = self.assemble_fixture(self.SELECTION)
        self.result = assemble(path, self.out_pdf, self.report)
        self.reader = PdfReader(self.out_pdf)

    def test_interleaved_page_order(self):
        texts = [p.extract_text().splitlines()[0] for p in self.reader.pages]
        self.assertEqual(texts, [
            "Source A page 1", "Source B page 2",
            "Source A page 3", "Source B page 1"])

    def test_no_orphan_pages(self):
        tree = {p.indirect_reference.idnum for p in self.reader.pages}
        size = int(self.reader.trailer["/Size"])
        orphans = [
            x for x in range(1, size)
            if isinstance(self.reader.get_object(x), DictionaryObject)
            and self.reader.get_object(x).get("/Type") == "/Page"
            and x not in tree]
        self.assertEqual(orphans, [])

    def test_named_destinations_namespaced_and_distinct(self):
        names = self.reader.named_destinations
        self.assertEqual(set(names), {"A:details", "B:details"})
        id2out = {p.indirect_reference.idnum: i
                  for i, p in enumerate(self.reader.pages)}
        # A:details -> A page 3 -> output page 3
        self.assertEqual(id2out[names["A:details"].page.indirect_reference.idnum], 2)
        # B:details -> B page 2 -> output page 2
        self.assertEqual(id2out[names["B:details"].page.indirect_reference.idnum], 1)

    def test_every_link_targets_correct_output_page(self):
        id2out = {p.indirect_reference.idnum: i + 1
                  for i, p in enumerate(self.reader.pages)}
        names = self.reader.named_destinations
        expected = {
            # (output page, annot index) -> output target page, or None
            (1, 0): 3, (1, 1): None,
            (2, 0): 4,
            (3, 0): 1,
            (4, 0): 2, (4, 1): 2, (4, 2): None,
        }
        for out_index, page in enumerate(self.reader.pages, start=1):
            for annot_index, ref in enumerate(page["/Annots"]):
                annot = ref.get_object()
                want = expected[(out_index, annot_index)]
                if want is None:
                    self.assertNotIn("/A", annot)
                    self.assertNotIn("/Dest", annot)
                    continue
                if "/A" in annot:
                    dest = annot["/A"].get_object()["/D"]
                else:
                    dest = annot["/Dest"]
                if isinstance(dest, str):
                    got = id2out[names[dest].page.indirect_reference.idnum]
                else:
                    got = id2out[dest[0].idnum]
                self.assertEqual(got, want)

    def test_omitted_links_keep_rectangle(self):
        source_a = PdfReader(os.path.join(FIXTURES, "source-A.pdf"))
        original = [float(v) for v in
                    source_a.pages[0]["/Annots"][1].get_object()["/Rect"]]
        out_annot = self.reader.pages[0]["/Annots"][1].get_object()
        self.assertEqual([float(v) for v in out_annot["/Rect"]], original)

    def test_outline_hierarchy_and_reattachment(self):
        tree = self._outline_tree()
        self.assertEqual(tree, [
            {"title": "Source A", "page": None, "children": [
                {"title": "A section", "page": 1, "children": [
                    {"title": "A details", "page": 3, "children": []}]}]},
            {"title": "Source B", "page": None, "children": [
                {"title": "B section", "page": 4, "children": [
                    {"title": "B middle", "page": 2, "children": []}]}]},
        ])

    def _outline_tree(self):
        root = self.reader.trailer["/Root"]["/Outlines"].get_object()

        def walk(node):
            result = []
            cur = node.get("/First")
            while cur is not None:
                item = cur.get_object()
                page = None
                if "/Dest" in item:
                    dest = item["/Dest"]
                    if isinstance(dest, ArrayObject):
                        idnum = dest[0].idnum
                    else:
                        idnum = self.reader.named_destinations[
                            str(dest)].page.indirect_reference.idnum
                    page = next(
                        i + 1 for i, p in enumerate(self.reader.pages)
                        if p.indirect_reference.idnum == idnum)
                result.append({"title": str(item["/Title"]), "page": page,
                               "children": walk(item) if "/First" in item else []})
                cur = item.get("/Next")
            return result

        return walk(root)

    def test_report_explains_every_link(self):
        report = self.read_report()
        self.assertEqual(len(report["links"]), 7)
        self.assertEqual(
            sum(1 for l in report["links"]
                if l["status"] == "target_omitted"), 2)
        self.assertEqual(
            sum(1 for l in report["links"] if l["status"] == "retargeted"), 5)
        removed = report["removed_actions"]
        self.assertEqual({(r["source"], r["annotation_index"]) for r in removed},
                         {("A", 1), ("B", 2)})
        for record in removed:
            self.assertEqual(record["diagnostic"], "target_omitted")

    def test_content_streams_byte_identical(self):
        sources = {
            "A": PdfReader(os.path.join(FIXTURES, "source-A.pdf")),
            "B": PdfReader(os.path.join(FIXTURES, "source-B.pdf")),
        }
        plan = [("A", 0), ("B", 1), ("A", 2), ("B", 0)]
        for out_index, (sid, src_index) in enumerate(plan):
            src = sources[sid].pages[src_index].get_contents().get_data()
            out = self.reader.pages[out_index].get_contents().get_data()
            self.assertEqual(src, out)


class PreflightRefusalTests(_TempCase):
    def _source_selection(self, path):
        return self.write_selection(
            {"S": path}, [{"source": "S", "page": 1}])

    def test_unknown_source_id(self):
        path = self.write_selection(
            {"A": os.path.join(FIXTURES, "source-A.pdf")},
            [{"source": "ZZ", "page": 1}])
        self.assert_refused(path, ["unknown_source"])

    def test_page_out_of_range(self):
        path = self.write_selection(
            {"A": os.path.join(FIXTURES, "source-A.pdf")},
            [{"source": "A", "page": 99}])
        self.assert_refused(path, ["page_out_of_range"])

    def test_duplicate_selection(self):
        path = self.write_selection(
            {"A": os.path.join(FIXTURES, "source-A.pdf")},
            [{"source": "A", "page": 1}, {"source": "A", "page": 1}])
        self.assert_refused(path, ["duplicate_source_page"])

    def test_missing_source_file(self):
        path = self.write_selection(
            {"A": os.path.join(self.tmp, "nope.pdf")},
            [{"source": "A", "page": 1}])
        self.assert_refused(path, ["source_unreadable"])

    def test_malformed_selection_json(self):
        path = os.path.join(self.tmp, "selection.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not valid json")
        self.assert_refused(path, ["selection_parse_error"])

    def test_schema_error(self):
        path = os.path.join(self.tmp, "selection.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"sources": {"A": "x.pdf"}, "pages": []}, handle)
        self.assert_refused(path, ["selection_schema_error"])

    def test_encrypted_pdf_refused(self):
        pdf = os.path.join(self.tmp, "enc.pdf")
        make_encrypted_pdf(pdf)
        self.assert_refused(self._source_selection(pdf),
                            ["encrypted_pdf_unsupported"])

    def test_form_refused(self):
        pdf = os.path.join(self.tmp, "form.pdf")
        make_form_pdf(pdf)
        self.assert_refused(self._source_selection(pdf), ["acroform_unsupported"])

    def test_gotor_refused(self):
        pdf = os.path.join(self.tmp, "gotor.pdf")
        make_link_pdf(pdf, action="/GoToR")
        diags = self.assert_refused(self._source_selection(pdf),
                                    ["unsupported_action"])
        self.assertTrue(any("/GoToR" in str(d) for d in diags))

    def test_uri_refused(self):
        pdf = os.path.join(self.tmp, "uri.pdf")
        make_link_pdf(pdf, action="/URI")
        self.assert_refused(self._source_selection(pdf),
                            ["unsupported_action"])

    def test_javascript_refused(self):
        pdf = os.path.join(self.tmp, "js.pdf")
        make_link_pdf(pdf, action="/JavaScript")
        self.assert_refused(self._source_selection(pdf),
                            ["unsupported_action"])

    def test_document_javascript_refused(self):
        pdf = os.path.join(self.tmp, "docjs.pdf")
        make_document_javascript_pdf(pdf)
        self.assert_refused(self._source_selection(pdf),
                            ["document_level_feature_unsupported"])

    def test_multiple_diagnostics_collected(self):
        # bad page + duplicate in one selection: both must be reported
        path = self.write_selection(
            {"A": os.path.join(FIXTURES, "source-A.pdf")},
            [{"source": "A", "page": 99}, {"source": "A", "page": 99}])
        diags = self.assert_refused(
            path, ["page_out_of_range", "duplicate_source_page"])
        self.assertGreaterEqual(len(diags), 2)


class GeometryAndDirectDestTests(_TempCase):
    def test_rotation_crop_and_direct_dest_preserved(self):
        pdf = os.path.join(self.tmp, "geom.pdf")
        make_direct_dest_link_pdf(pdf, target_page_index=1)
        # add rotation/crop by rebuilding through a second writer is complex;
        # instead synthesize a rotated source with a link via make_link_pdf
        rotated = os.path.join(self.tmp, "rot.pdf")
        make_link_pdf(rotated, link_kind="explicit", target_page_index=2,
                      rotation=90, crop=(10, 10, 300, 400))
        path = self.write_selection(
            {"R": rotated},
            [{"source": "R", "page": 1}, {"source": "R", "page": 3},
             {"source": "R", "page": 2}])
        assemble(path, self.out_pdf, self.report)
        reader = PdfReader(self.out_pdf)
        # output page 1 is source page 1: rotation + crop carried over
        self.assertEqual(int(reader.pages[0].get("/Rotate", 0)), 90)
        self.assertEqual([float(v) for v in reader.pages[0].cropbox],
                         [10, 10, 300, 400])
        # link on output page 1 originally targeted source page 3,
        # which lands on output page 2
        annot = reader.pages[0]["/Annots"][0].get_object()
        dest = annot["/A"].get_object()["/D"]
        id2out = {p.indirect_reference.idnum: i
                  for i, p in enumerate(reader.pages)}
        self.assertEqual(id2out[dest[0].idnum], 1)

    def test_direct_dest_holder_migrated(self):
        pdf = os.path.join(self.tmp, "direct.pdf")
        make_direct_dest_link_pdf(pdf, target_page_index=1)
        path = self.write_selection(
            {"D": pdf},
            [{"source": "D", "page": 1}, {"source": "D", "page": 2}])
        assemble(path, self.out_pdf, self.report)
        reader = PdfReader(self.out_pdf)
        annot = reader.pages[0]["/Annots"][0].get_object()
        self.assertIn("/Dest", annot)  # direct holder preserved, retargeted
        id2out = {p.indirect_reference.idnum: i
                  for i, p in enumerate(reader.pages)}
        self.assertEqual(id2out[annot["/Dest"][0].idnum], 1)


class CrossSourceSameNameTests(_TempCase):
    """The two sources may define the same destination name; the later source
    must never overwrite the earlier one. Namespacing keeps both targets."""

    def test_same_named_target_resolves_by_source_identity(self):
        p_a = os.path.join(self.tmp, "a.pdf")
        p_b = os.path.join(self.tmp, "b.pdf")
        make_link_pdf(p_a, link_kind="named", target_page_index=2)  # -> A p3
        make_link_pdf(p_b, link_kind="named", target_page_index=0)  # -> B p1
        path = self.write_selection(
            {"A": p_a, "B": p_b},
            [{"source": "A", "page": 1}, {"source": "B", "page": 1},
             {"source": "A", "page": 3}, {"source": "B", "page": 2}])
        assemble(path, self.out_pdf, self.report)
        reader = PdfReader(self.out_pdf)
        id2out = {p.indirect_reference.idnum: i + 1
                  for i, p in enumerate(reader.pages)}
        # output order: A1, B1, A3, B2
        self.assertEqual(
            id2out[reader.named_destinations["A:details"]
                   .page.indirect_reference.idnum], 3)
        self.assertEqual(
            id2out[reader.named_destinations["B:details"]
                   .page.indirect_reference.idnum], 2)
        a_link = reader.pages[0]["/Annots"][0].get_object()
        b_link = reader.pages[1]["/Annots"][0].get_object()
        self.assertEqual(
            str(a_link["/A"].get_object()["/D"]), "A:details")
        self.assertEqual(
            str(b_link["/A"].get_object()["/D"]), "B:details")


class OmittedNamedDestinationTests(_TempCase):
    def test_named_destination_to_omitted_page_is_dropped(self):
        # named 'details' points at page 3 (index 2); select only pages 1-2
        pdf = os.path.join(self.tmp, "named.pdf")
        make_link_pdf(pdf, link_kind="named", target_page_index=2)
        path = self.write_selection(
            {"N": pdf},
            [{"source": "N", "page": 1}, {"source": "N", "page": 2}])
        assemble(path, self.out_pdf, self.report)
        reader = PdfReader(self.out_pdf)
        # no names tree published
        self.assertNotIn("/Names", reader.trailer["/Root"])
        annot = reader.pages[0]["/Annots"][0].get_object()
        self.assertNotIn("/A", annot)
        self.assertNotIn("/Dest", annot)
        report = self.read_report()
        named = report["named_destinations"][0]
        self.assertEqual(named["status"], "omitted")
        self.assertEqual(named["diagnostic"], "target_omitted")


class CliTests(_TempCase):
    def test_cli_refusal_exit_code_and_report(self):
        path = self.write_selection(
            {"A": os.path.join(FIXTURES, "source-A.pdf")},
            [{"source": "A", "page": 400}])
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "assemble_pdf.py"),
             "--selection", path, "--output", self.out_pdf,
             "--report", self.report],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refused", proc.stderr)
        self.assertFalse(os.path.exists(self.out_pdf))

    def test_cli_assemble_then_verify_subprocess(self):
        path = self.assemble_fixture([
            {"source": "A", "page": 1}, {"source": "B", "page": 2},
            {"source": "A", "page": 3}, {"source": "B", "page": 1}])
        build = subprocess.run(
            [sys.executable, os.path.join(ROOT, "assemble_pdf.py"),
             "--selection", path, "--output", self.out_pdf,
             "--report", self.report],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(build.returncode, 0, build.stderr)
        verify_out = os.path.join(self.tmp, "verify.json")
        check = subprocess.run(
            [sys.executable, os.path.join(ROOT, "verify_pdf.py"),
             "--combined", self.out_pdf, "--report", self.report,
             "--fixtures-dir", FIXTURES, "--out", verify_out],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        with open(verify_out, encoding="utf-8") as handle:
            summary = json.load(handle)
        self.assertTrue(summary["ok"])
        self.assertGreater(summary["total_checks"], 50)


if __name__ == "__main__":
    unittest.main()
