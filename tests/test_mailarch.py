"""mailarch test suite.

Runs against the real fixture corpus in fixtures/eml (nested related,
forwarded message/rfc822, missing parent, identity conflict, reference
cycle, self reference, contradictory parents) plus generated edge-case
samples.  Standard library unittest only.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from mailarch import export, verify  # noqa: E402

FIXTURES = REPO / "fixtures" / "eml"

PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNQaDgAAAIkAWHdFJqQ\n"
    "AAAAAElFTkSuQmCC"
)

# ---------------------------------------------------------------------------
# generated edge-case samples
# ---------------------------------------------------------------------------
AMBIG_V1 = """From: a@example.invalid
Message-ID: <dup@t.invalid>
Subject: variant one
Date: Sat, 14 Nov 2026 10:00:00 +0800
Content-Type: text/plain; charset="utf-8"

version one
"""

AMBIG_V2 = """From: a@example.invalid
Message-ID: <dup@t.invalid>
Subject: variant two
Date: Sat, 14 Nov 2026 11:00:00 +0800
Content-Type: text/plain; charset="utf-8"

version two, different bytes
"""

AMBIG_REPLY = """From: b@example.invalid
Message-ID: <follower@t.invalid>
Subject: followup
References: <dup@t.invalid>
Content-Type: text/plain; charset="utf-8"

which one?
"""

EMPTY_HTML_ALT = """From: a@example.invalid
Message-ID: <emptyhtml@t.invalid>
Subject: empty html
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="ALT"

--ALT
Content-Type: text/plain; charset="utf-8"

plain wins
--ALT
Content-Type: text/html; charset="utf-8"

<html><body></body></html>
--ALT--
"""

RELATED_NO_START = """From: a@example.invalid
Message-ID: <nostart@t.invalid>
Subject: no start param
MIME-Version: 1.0
Content-Type: multipart/related; boundary="REL"

--REL
Content-Type: text/html; charset="utf-8"

<p>root html <img src="cid:px@t.invalid"></p>
--REL
Content-Type: image/png
Content-ID: <px@t.invalid>
Content-Transfer-Encoding: base64

{png}
--REL--
""".format(png=PNG_B64)

MULTI_IRT = """From: a@example.invalid
Message-ID: <multi@t.invalid>
Subject: multi irt
In-Reply-To: <a@t.invalid> <b@t.invalid>
Content-Type: text/plain; charset="utf-8"

multi
"""

NESTED_FORWARD = """From: a@example.invalid
Message-ID: <outer@t.invalid>
Subject: outer
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="OUTER"

--OUTER
Content-Type: text/plain; charset="utf-8"

outer body
--OUTER
Content-Type: message/rfc822
Content-Disposition: attachment

Message-ID: <mid@t.invalid>
From: b@example.invalid
Subject: mid
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="INNER"

--INNER
Content-Type: text/plain; charset="utf-8"

mid body
--INNER
Content-Type: message/rfc822
Content-Disposition: attachment

Message-ID: <innermost@t.invalid>
From: c@example.invalid
Subject: innermost
Content-Type: text/plain; charset="utf-8"

innermost body
--INNER--
--OUTER--
"""

UNKNOWN_MARKUP = """From: a@example.invalid
Message-ID: <markup@t.invalid>
Subject: markup
Content-Type: text/html; charset="utf-8"

<html><body><marquee>scroll</marquee><p onclick="evil()">hi</p><custom-tag>x</custom-tag></body></html>
"""

UNKNOWN_CHARSET = """From: a@example.invalid
Message-ID: <charset@t.invalid>
Subject: charset
Content-Type: text/plain; charset="x-unknown-charset"

abc
"""

BAD_BASE64 = """From: a@example.invalid
Message-ID: <badb64@t.invalid>
Subject: bad base64
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BB"

--BB
Content-Type: text/plain; charset="utf-8"

body ok
--BB
Content-Type: application/octet-stream
Content-Transfer-Encoding: base64
Content-Disposition: attachment; filename="broken.bin"

QUJDRA
--BB--
"""


def write_files(src: Path, files: dict[str, str | bytes]):
    src.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        src.joinpath(name).write_bytes(
            data.encode("utf-8") if isinstance(data, str) else data)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def ingest(self, files: dict[str, str | bytes]):
        src = self.tmp / "src"
        out = self.tmp / "out"
        write_files(src, files)
        manifest = export.ingest(src, out)
        return manifest, src, out

    def ingest_fixtures(self):
        src = self.tmp / "src"
        out = self.tmp / "out"
        shutil.copytree(FIXTURES, src)
        manifest = export.ingest(src, out)
        return manifest, src, out


def by_msgid(manifest, msgid):
    return [m for m in manifest["messages"] if m["message_id"] == msgid]


def diags(manifest, dtype):
    return [d for d in manifest["threads"]["diagnostics"] if d["type"] == dtype]


class TestFixturesThreading(Base):
    def setUp(self):
        super().setUp()
        self.manifest, self.src, self.out = self.ingest_fixtures()

    def test_byte_identical_duplicate_collapses(self):
        self.assertEqual(len(self.manifest["files"]), 11)
        self.assertEqual(len(self.manifest["messages"]), 10)
        replies = by_msgid(self.manifest, "<reply@example.invalid>")
        self.assertEqual(len(replies), 2)  # 02/05 collapsed + 06 conflict variant
        collapsed = [r for r in replies if len(r["source_files"]) == 2]
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(sorted(collapsed[0]["source_files"]),
                         ["02-reply-with-forward.eml", "05-identical-duplicate.eml"])

    def test_conflicting_id_variants_kept(self):
        conflicts = diags(self.manifest, "id-conflict")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["message_id"], "<reply@example.invalid>")
        self.assertEqual(len(conflicts[0]["nodes"]), 2)
        # both variants keep distinct raw digests; neither was picked as "the" one
        replies = by_msgid(self.manifest, "<reply@example.invalid>")
        self.assertNotEqual(replies[0]["raw_sha256"], replies[1]["raw_sha256"])

    def test_missing_parent_placeholder(self):
        orphans = by_msgid(self.manifest, "<orphan@example.invalid>")
        self.assertEqual(len(orphans), 1)
        orphan = orphans[0]
        self.assertTrue(orphan["parent"].startswith("g_"))
        mp = diags(self.manifest, "missing-parent")
        self.assertEqual(len(mp), 1)
        self.assertEqual(mp[0]["parent_msgid"], "<missing@example.invalid>")
        ghosts = self.manifest["threads"]["placeholders"]
        self.assertEqual(len(ghosts), 1)
        conv = [c for c in self.manifest["threads"]["conversations"]
                if orphan["node_id"] in c["members"]][0]
        self.assertIn(orphan["parent"], conv["members"])
        self.assertIn("missing-parent", conv["flags"])

    def test_contradictory_parent_conflict_retained(self):
        contra = by_msgid(self.manifest, "<contradiction@example.invalid>")[0]
        orphan = by_msgid(self.manifest, "<orphan@example.invalid>")[0]
        # References last item wins as parent candidate
        self.assertEqual(contra["parent"], orphan["node_id"])
        # but the In-Reply-To disagreement is preserved as a conflict
        irt = diags(self.manifest, "irt-conflict")
        self.assertEqual(len(irt), 1)
        self.assertEqual(irt[0]["references_last"], "<orphan@example.invalid>")
        self.assertEqual(irt[0]["in_reply_to"], ["<independent@example.invalid>"])
        # both headers retained as evidence on the node
        self.assertEqual(contra["references"],
                         ["<root@example.invalid>", "<orphan@example.invalid>"])
        self.assertEqual(contra["in_reply_to"], ["<independent@example.invalid>"])

    def test_reference_cycle_and_self_reference(self):
        cyc = diags(self.manifest, "reference-cycle")
        self.assertEqual(len(cyc), 1)
        members = cyc[0]["members"]
        self.assertEqual(len(members), 2)
        # both edges are kept, none dropped to hide the cycle
        edges = {(e["child"], e["parent"]) for e in self.manifest["threads"]["edges"]}
        a, b = members
        self.assertIn((a, b), edges)
        self.assertIn((b, a), edges)
        conv = [c for c in self.manifest["threads"]["conversations"]
                if a in c["members"]][0]
        self.assertIn("cycle", conv["flags"])
        selfref = diags(self.manifest, "self-reference")
        self.assertEqual(len(selfref), 1)
        selfnode = by_msgid(self.manifest, "<self@example.invalid>")[0]
        self.assertEqual(selfnode["parent"], selfnode["node_id"])

    def test_same_subject_without_references_stays_independent(self):
        root = by_msgid(self.manifest, "<root@example.invalid>")[0]
        indep = by_msgid(self.manifest, "<independent@example.invalid>")[0]
        self.assertEqual(root["subject"], indep["subject"])
        self.assertNotEqual(root["conversation"], indep["conversation"])
        self.assertIsNone(indep["parent"])

    def test_forwarded_rfc822_not_a_conversation_member(self):
        attached = self.manifest["attached_messages"]
        self.assertEqual(len(attached), 1)
        att = attached[0]
        self.assertEqual(att["message_id"], "<attached-only@example.invalid>")
        all_members = {m for c in self.manifest["threads"]["conversations"]
                       for m in c["members"]}
        self.assertNotIn(att["node_id"], all_members)
        # its own dangling reference must not create a top-level placeholder
        ghost_ids = {g["message_id"] for g in self.manifest["threads"]["placeholders"]}
        self.assertNotIn("<ghost-attached@example.invalid>", ghost_ids)
        # but the evidence is retained on the attached message itself
        self.assertEqual(att["references"], ["<ghost-attached@example.invalid>"])
        # attached message page exists and its .eml asset digest matches
        self.assertTrue((self.out / "messages" / f"{att['node_id']}.html").exists())


class TestFixturesMime(Base):
    def setUp(self):
        super().setUp()
        self.manifest, self.src, self.out = self.ingest_fixtures()

    def _msg(self, msgid):
        return by_msgid(self.manifest, msgid)[0]

    def test_alternative_prefers_html_keeps_plain(self):
        root = self._msg("<root@example.invalid>")
        self.assertEqual(root["body"], {"path": "1.2.1", "format": "html"})
        self.assertEqual(root["alternatives"], ["1.1"])
        roles = {p["path"]: p["role"] for p in root["parts"]}
        self.assertEqual(roles["1.1"], "alternative")
        self.assertEqual(roles["1.2.1"], "body")

    def test_related_start_selects_root(self):
        scopes = self._msg("<scopes@example.invalid>")
        # start=<main@scope> points at the third child
        self.assertEqual(scopes["body"], {"path": "3", "format": "html"})
        roles = {p["path"]: p["role"] for p in scopes["parts"]}
        self.assertEqual(roles["1"], "inline")   # first child is NOT the root here
        self.assertEqual(roles["2"], "inline")

    def test_cid_scopes_do_not_leak(self):
        scopes = self._msg("<scopes@example.invalid>")
        self.assertEqual(scopes["scopes"]["s1"], {"reused@scope": "2"})
        self.assertEqual(scopes["scopes"]["s2"], {"reused@scope": "4.2"})
        refs = {(r["part"], r["cid"]): r["bound_to"] for r in scopes["cid_refs"]}
        # outer html binds the outer png; nested html binds the nested
        # octet-stream; the same CID in the other scope never wins
        self.assertEqual(refs[("3", "reused@scope")], "2")
        self.assertEqual(refs[("4.1", "reused@scope")], "4.2")
        self.assertIsNone(refs[("3", "missing@scope")])
        unbound = [i for i in self.manifest["mime_issues"]
                   if i["code"] == "cid-unbound"]
        self.assertEqual(len(unbound), 1)
        self.assertEqual(unbound[0]["detail"], "missing@scope")

    def test_attachment_filename_display_only(self):
        root = self._msg("<root@example.invalid>")
        csv = [p for p in root["parts"] if p["role"] == "attachment"][0]
        self.assertEqual(csv["display_name"], "预算表.csv")  # decoded, display only
        self.assertNotIn("预算表", csv["asset"])
        self.assertRegex(csv["asset"], r"^assets/[0-9a-f]{16}\.csv$")
        inline = [p for p in root["parts"] if p["role"] == "inline"][0]
        self.assertEqual(inline["display_name"], "内嵌图.png")
        # declared disposition says attachment, actual role is inline resource
        self.assertEqual(inline["disposition"], "attachment")

    def test_html_sanitized_and_cid_rewritten(self):
        root = self._msg("<root@example.invalid>")
        page = (self.out / "messages" / f"{root['node_id']}.html").read_text()
        self.assertNotIn("<script", page.lower())
        self.assertNotIn('src="http', page)
        self.assertIn("remote image blocked", page)  # remote URL shown as blocked
        inline = [p for p in root["parts"] if p["role"] == "inline"][0]
        self.assertIn(f'src="../{inline["asset"]}"', page)  # cid rewritten to local asset

    def test_unbound_cid_gap_visible(self):
        scopes = self._msg("<scopes@example.invalid>")
        page = (self.out / "messages" / f"{scopes['node_id']}.html").read_text()
        self.assertIn("missing inline resource: missing@scope", page)

    def test_nested_inline_html_rendered_with_own_scope(self):
        scopes = self._msg("<scopes@example.invalid>")
        page = (self.out / "messages" / f"{scopes['node_id']}.html").read_text()
        self.assertIn("内嵌 HTML 资源", page)
        nested = [p for p in scopes["parts"] if p["path"] == "4.2"][0]
        self.assertIn(nested["asset"], page)


class TestGeneratedSamples(Base):
    def test_ambiguous_parent_not_decided(self):
        manifest, src, out = self.ingest({
            "v1.eml": AMBIG_V1, "v2.eml": AMBIG_V2, "reply.eml": AMBIG_REPLY})
        amb = diags(manifest, "ambiguous-parent")
        self.assertEqual(len(amb), 1)
        self.assertEqual(len(amb[0]["candidates"]), 2)
        reply = by_msgid(manifest, "<follower@t.invalid>")[0]
        self.assertIsNone(reply["parent"])  # no arbitrary choice
        self.assertEqual(len(reply["ambiguous_parents"]), 2)

    def test_empty_html_falls_back_to_plain(self):
        manifest, src, out = self.ingest({"e.eml": EMPTY_HTML_ALT})
        msg = manifest["messages"][0]
        self.assertEqual(msg["body"]["format"], "plain")
        roles = {p["path"]: p["role"] for p in msg["parts"]}
        self.assertEqual(roles["2"], "alternative")  # empty html kept as alternative

    def test_related_without_start_uses_first_child(self):
        manifest, src, out = self.ingest({"r.eml": RELATED_NO_START})
        msg = manifest["messages"][0]
        self.assertEqual(msg["body"], {"path": "1", "format": "html"})
        self.assertEqual(msg["cid_refs"][0]["bound_to"], "2")

    def test_multiple_in_reply_to_no_parent(self):
        manifest, src, out = self.ingest({"m.eml": MULTI_IRT})
        d = diags(manifest, "multiple-in-reply-to")
        self.assertEqual(len(d), 1)
        self.assertIsNone(manifest["messages"][0]["parent"])

    def test_nested_forwarded_messages(self):
        manifest, src, out = self.ingest({"f.eml": NESTED_FORWARD})
        self.assertEqual(len(manifest["attached_messages"]), 2)
        ids = {a["message_id"] for a in manifest["attached_messages"]}
        self.assertEqual(ids, {"<mid@t.invalid>", "<innermost@t.invalid>"})
        innermost = [a for a in manifest["attached_messages"]
                     if a["message_id"] == "<innermost@t.invalid>"][0]
        mid = [a for a in manifest["attached_messages"]
               if a["message_id"] == "<mid@t.invalid>"][0]
        self.assertEqual(innermost["attached_to"]["message"], mid["node_id"])
        for a in manifest["attached_messages"]:
            self.assertTrue((out / "messages" / f"{a['node_id']}.html").exists())

    def test_unsupported_markup_escaped(self):
        manifest, src, out = self.ingest({"u.eml": UNKNOWN_MARKUP})
        page = (out / "messages" / f"{manifest['messages'][0]['node_id']}.html").read_text()
        self.assertIn("&lt;marquee&gt;", page)   # escaped, visible as text
        self.assertNotIn("<marquee>", page)
        self.assertNotIn("onclick", page)        # event attributes gone

    def test_unknown_charset_reported(self):
        manifest, src, out = self.ingest({"c.eml": UNKNOWN_CHARSET})
        codes = [i["code"] for i in manifest["mime_issues"]]
        self.assertIn("charset-fallback", codes)
        problem_parts = manifest["report"]["problem_parts"]
        self.assertTrue(any(p["code"] == "charset-fallback" for p in problem_parts))

    def test_broken_base64_kept_and_reported(self):
        manifest, src, out = self.ingest({"b.eml": BAD_BASE64})
        msg = manifest["messages"][0]
        # the message survives with its confirmable structure
        self.assertEqual(msg["body"]["format"], "plain")
        att = [p for p in msg["parts"] if p["role"] == "attachment"][0]
        self.assertIsNotNone(att["sha256"])
        issues = {i["code"] for i in msg["issues"]}
        self.assertTrue(issues & {"defect", "decode-failed"})


class TestVerifyAndDeterminism(Base):
    def test_verify_ok_then_tamper_fails(self):
        manifest, src, out = self.ingest_fixtures()
        ok, problems = verify.verify(out, src)
        self.assertTrue(ok, problems)
        # tamper with one exported asset
        victim = out / manifest["assets"][0]["path"]
        victim.write_bytes(victim.read_bytes() + b"x")
        ok, problems = verify.verify(out, src)
        self.assertFalse(ok)
        self.assertTrue(any("digest mismatch" in p for p in problems))

    def test_verify_detects_missing_page(self):
        manifest, src, out = self.ingest_fixtures()
        victim = out / "messages" / f"{manifest['messages'][0]['node_id']}.html"
        victim.unlink()
        ok, problems = verify.verify(out, src)
        self.assertFalse(ok)
        self.assertTrue(any("page missing" in p for p in problems))

    def test_verify_detects_stray_asset(self):
        manifest, src, out = self.ingest_fixtures()
        (out / "assets" / "stray.bin").write_bytes(b"stray")
        ok, problems = verify.verify(out, src)
        self.assertFalse(ok)
        self.assertTrue(any("unexpected file" in p for p in problems))

    def test_shuffled_input_same_conversation_evidence(self):
        names_a = sorted(p.name for p in FIXTURES.glob("*.eml"))
        names_b = list(reversed(names_a))
        manifests = []
        for i, names in enumerate((names_a, names_b)):
            src = self.tmp / f"src{i}"
            src.mkdir()
            for j, name in enumerate(names):
                # different file names and write order; identical bytes
                shutil.copy(FIXTURES / name, src / f"{j:02d}-{name}")
            out = self.tmp / f"out{i}"
            manifests.append(export.ingest(src, out))
        ma, mb = manifests
        self.assertEqual(ma["threads"], mb["threads"])
        self.assertEqual(ma["assets"], mb["assets"])
        self.assertEqual(ma["mime_issues"], mb["mime_issues"])
        self.assertEqual(ma["attached_messages"], mb["attached_messages"])

        def strip(messages):
            return [{k: v for k, v in m.items() if k != "source_files"}
                    for m in messages]
        self.assertEqual(strip(ma["messages"]), strip(mb["messages"]))

    def test_attached_message_digest_matches_source_part(self):
        manifest, src, out = self.ingest_fixtures()
        ok, problems = verify.verify(out, src)
        self.assertTrue(ok, problems)
        att = manifest["attached_messages"][0]
        owner = [m for m in manifest["messages"]
                 if m["node_id"] == att["attached_to"]["message"]][0]
        part = [p for p in owner["parts"]
                if p["path"] == att["attached_to"]["part"]][0]
        self.assertEqual(part["sha256"], att["raw_sha256"])
        self.assertEqual(part["digest_note"], "canonical-reserialization")


class TestCli(Base):
    def test_cli_ingest_and_verify(self):
        src = self.tmp / "src"
        out = self.tmp / "out"
        shutil.copytree(FIXTURES, src)
        env = dict(PYTHONPATH=str(REPO))
        r = subprocess.run([sys.executable, "-m", "mailarch", "ingest", str(src), str(out)],
                           capture_output=True, text=True, cwd=REPO,
                           env={**__import__("os").environ, **env})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ingest:", r.stdout)
        r = subprocess.run([sys.executable, "-m", "mailarch", "verify", str(out),
                            "--src", str(src)],
                           capture_output=True, text=True, cwd=REPO,
                           env={**__import__("os").environ, **env})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
