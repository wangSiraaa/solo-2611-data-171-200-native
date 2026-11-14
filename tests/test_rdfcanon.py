"""验证套件: fixtures、词项身份、随机重命名不变性、微型图独立枚举核对。"""

from __future__ import annotations

import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rdfcanon import model, parse_nquads, parse_nquads_file, canonicalize, compare
from rdfcanon.compare import _apply_mapping

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def fixture(name):
    return parse_nquads_file(os.path.join(FIXTURES, name))


# ---------------------------------------------------------------------------
# 独立暴力判等器: 枚举全部空白节点双射, 逐四元组比对集合 (n! 适用于 n<=6)
# ---------------------------------------------------------------------------

def brute_force_isomorphic(quads_a, quads_b):
    nodes_a = sorted({t[1] for q in quads_a for t in q if model.is_bnode(t)})
    nodes_b = sorted({t[1] for q in quads_b for t in q if model.is_bnode(t)})
    if len(nodes_a) != len(nodes_b):
        return False
    for perm in itertools.permutations(nodes_b):
        mapping = dict(zip(nodes_a, perm))
        if _apply_mapping(quads_a, mapping) == quads_b:
            return True
    return False


def relabeled_text(ds, rng):
    """随机换空白标签 + 随机换语句顺序后的 N-Quads 文本。"""
    labels = ds.bnode_labels
    fresh = rng.sample([f"n{i}{rng.choice('xyzw')}" for i in range(len(labels))], len(labels))
    rename = dict(zip(labels, fresh))
    lines = [model.quad_line(q, rename.get) for q in ds.quads]
    rng.shuffle(lines)
    return "".join(line + "\n" for line in lines)


def random_dataset(rng, max_nodes=6):
    """随机微型数据集: 空白节点 + IRI + 各形 literal + 默认/命名图 (图名可为空白节点)。"""
    n = rng.randint(0, max_nodes)
    preds = [f"https://ex.invalid/p{i}" for i in range(3)]
    lits = [
        model.literal("01", "http://www.w3.org/2001/XMLSchema#integer"),
        model.literal("1", "http://www.w3.org/2001/XMLSchema#integer"),
        model.literal("x"),  # plain -> xsd:string
        model.literal("x", lang="en-US"),
        model.literal("y", lang="ZH-cn"),
        model.literal("é\n样", "http://www.w3.org/2001/XMLSchema#string"),
    ]
    ds = model.Dataset(source="random")
    for i in range(rng.randint(0, n * 2 + 2)):
        s = model.bnode(f"b{rng.randint(0, max(n - 1, 0))}") if n and rng.random() < 0.8 else model.iri(preds[0])
        p = model.iri(rng.choice(preds))
        r = rng.random()
        if n and r < 0.5:
            o = model.bnode(f"b{rng.randint(0, max(n - 1, 0))}")
        elif r < 0.75:
            o = rng.choice(lits)
        else:
            o = model.iri(rng.choice(preds))
        r = rng.random()
        if r < 0.5:
            g = model.DEFAULT_GRAPH
        elif n and r < 0.7:
            g = model.bnode(f"b{rng.randint(0, max(n - 1, 0))}")
        else:
            g = model.iri("https://ex.invalid/g")
        ds.add((s, p, o, g), i + 1, "")
    return ds


def cycle(n, pred="https://ex.invalid/next"):
    ds = model.Dataset()
    for i in range(n):
        ds.add((model.bnode(f"v{i}"), model.iri(pred), model.bnode(f"v{(i + 1) % n}"), model.DEFAULT_GRAPH), i + 1, "")
    return ds


class TestFixtures(unittest.TestCase):
    def test_base_equiv_renamed(self):
        r = compare(fixture("base.nq"), fixture("renamed.nq"))
        self.assertTrue(r.equivalent)
        self.assertTrue(r.mapping_replay_ok)
        self.assertEqual(len(r.bnode_mapping), 4)

    def test_base_not_equiv_changed(self):
        # changed 把 "01" 改成 "1": 词法身份不同 -> 不等价
        r = compare(fixture("base.nq"), fixture("changed.nq"))
        self.assertFalse(r.equivalent)
        self.assertTrue(r.only_in_a and r.only_in_b)
        self.assertIn('"01"', r.only_in_a[0])

    def test_symmetric_equiv_renamed(self):
        r = compare(fixture("symmetric.nq"), fixture("symmetric-renamed.nq"))
        self.assertTrue(r.equivalent)
        self.assertTrue(r.mapping_replay_ok)
        self.assertEqual(len(r.bnode_mapping), 6)

    def test_symmetric_not_equiv_nonisomorphic(self):
        # C6 与 C3+C3: 全部节点同度, 精化无法区分, 须靠完整搜索判异
        r = compare(fixture("symmetric.nq"), fixture("nonisomorphic.nq"))
        self.assertFalse(r.equivalent)

    def test_duplicate_quads_folded(self):
        ds = fixture("base.nq")
        self.assertEqual(ds.duplicate_count, 1)
        self.assertEqual(len(ds), 10)


class TestTermIdentity(unittest.TestCase):
    def canon(self, text):
        return canonicalize(parse_nquads(text)).canonical_nquads

    def test_numeric_lexical_not_normalized(self):
        a = self.canon('_:a <https://ex.invalid/p> "01"^^<http://www.w3.org/2001/XMLSchema#integer> .\n')
        b = self.canon('_:a <https://ex.invalid/p> "1"^^<http://www.w3.org/2001/XMLSchema#integer> .\n')
        self.assertNotEqual(a, b)

    def test_plain_equals_xsd_string(self):
        a = self.canon('_:a <https://ex.invalid/p> "s" .\n')
        b = self.canon('_:a <https://ex.invalid/p> "s"^^<http://www.w3.org/2001/XMLSchema#string> .\n')
        self.assertEqual(a, b)

    def test_lang_case_insensitive(self):
        a = self.canon('_:a <https://ex.invalid/p> "s"@ZH-cn .\n')
        b = self.canon('_:a <https://ex.invalid/p> "s"@zh-CN .\n')
        self.assertEqual(a, b)

    def test_lang_differs_from_plain(self):
        a = self.canon('_:a <https://ex.invalid/p> "s"@en .\n')
        b = self.canon('_:a <https://ex.invalid/p> "s" .\n')
        self.assertNotEqual(a, b)

    def test_default_graph_differs_from_named(self):
        a = self.canon('_:a <https://ex.invalid/p> _:b .\n')
        b = self.canon('_:a <https://ex.invalid/p> _:b <https://ex.invalid/g> .\n')
        self.assertNotEqual(a, b)

    def test_bnode_graph_name_is_dataset_node(self):
        # 图名空白节点与主语空白节点是否同一个, 影响判等
        a = self.canon('_:a <https://ex.invalid/p> "x" _:a .\n_:a <https://ex.invalid/q> "y" .\n')
        b = self.canon('_:a <https://ex.invalid/p> "x" _:g .\n_:a <https://ex.invalid/q> "y" .\n_:g <https://ex.invalid/r> "z" .\n')
        self.assertNotEqual(a, b)

    def test_duplicate_statement_folds(self):
        one = '_:a <https://ex.invalid/p> _:b .\n'
        two = one + '_:b <https://ex.invalid/p> _:a .\n_:a <https://ex.invalid/p> _:b .\n'
        self.assertEqual(
            self.canon('_:b <https://ex.invalid/p> _:a .\n' + one),
            self.canon(two),
        )


class TestRandomRelabelInvariance(unittest.TestCase):
    def test_fixtures(self):
        rng = random.Random(20261114)
        for name in ("base.nq", "renamed.nq", "changed.nq", "symmetric.nq",
                     "symmetric-renamed.nq", "nonisomorphic.nq"):
            ds = fixture(name)
            want = canonicalize(ds).canonical_bytes
            for _ in range(25):
                variant = parse_nquads(relabeled_text(ds, rng))
                self.assertEqual(canonicalize(variant).canonical_bytes, want, name)

    def test_random_datasets(self):
        rng = random.Random(7)
        for _ in range(60):
            ds = random_dataset(rng)
            want = canonicalize(ds).canonical_bytes
            for _ in range(5):
                variant = parse_nquads(relabeled_text(ds, rng))
                self.assertEqual(canonicalize(variant).canonical_bytes, want)


class TestMicroExhaustive(unittest.TestCase):
    """<=6 节点微型图: 与独立枚举判等器对照, 并逐四元组重放所报对应。"""

    def check_pair(self, ds_a, ds_b):
        r = compare(ds_a, ds_b)
        want = brute_force_isomorphic(ds_a.quads, ds_b.quads)
        self.assertEqual(r.equivalent, want)
        if r.equivalent:
            # 逐四元组重放
            self.assertEqual(_apply_mapping(ds_a.quads, r.bnode_mapping), ds_b.quads)
            self.assertTrue(r.mapping_replay_ok)
        return r

    def test_all_digraphs_n3(self):
        # n=3 全部有向图 (无自环, 单谓词): 64 个图, 全部两两对照
        nodes = range(3)
        edges = [(i, j) for i in nodes for j in nodes if i != j]
        graphs = []
        for mask in range(1 << len(edges)):
            ds = model.Dataset()
            for k, (i, j) in enumerate(edges):
                if mask >> k & 1:
                    ds.add((model.bnode(f"v{i}"), model.iri("https://ex.invalid/e"),
                            model.bnode(f"v{j}"), model.DEFAULT_GRAPH), k, "")
            graphs.append(ds)
        for a, b in itertools.combinations(graphs, 2):
            self.check_pair(a, b)

    def test_structured_families(self):
        # 环、环的不交并、路径、星形: 同度异构必须判异
        fam = {"C6": cycle(6), "C3+C3": None, "C4": cycle(4), "C5": cycle(5)}
        two = model.Dataset()
        for offset, n in ((0, 3), (3, 3)):
            for i in range(n):
                two.add((model.bnode(f"w{offset + i}"), model.iri("https://ex.invalid/next"),
                         model.bnode(f"w{offset + (i + 1) % n}"), model.DEFAULT_GRAPH), i, "")
        fam["C3+C3"] = two
        keys = list(fam)
        for i, j in itertools.combinations(range(len(keys)), 2):
            self.check_pair(fam[keys[i]], fam[keys[j]])
        # 环与自身重命名变体
        rng = random.Random(3)
        for name, ds in fam.items():
            variant = parse_nquads(relabeled_text(ds, rng))
            r = self.check_pair(ds, variant)
            self.assertTrue(r.equivalent, name)

    def test_random_datasets_vs_brute_force(self):
        rng = random.Random(99)
        datasets = [random_dataset(rng) for _ in range(80)]
        for _ in range(300):
            a = rng.choice(datasets)
            if rng.random() < 0.5:
                b = parse_nquads(relabeled_text(a, rng))  # 必等价
            else:
                b = rng.choice(datasets)
            self.check_pair(a, b)


class TestParserCrossCheck(unittest.TestCase):
    """自解析器与 rdflib (NORMALIZE_LITERALS=False) 解析结果应给出相同规范字节。"""

    def _from_rdflib(self, text=None, path=None):
        import warnings
        import rdflib
        rdflib.NORMALIZE_LITERALS = False
        g = rdflib.Dataset()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if path:
                g.parse(path, format="nquads")
            else:
                g.parse(data=text, format="nquads")
        default_id = g.default_context.identifier
        ds = model.Dataset()

        def conv(t):
            if isinstance(t, rdflib.URIRef):
                return model.iri(str(t))
            if isinstance(t, rdflib.BNode):
                return model.bnode(str(t))
            return model.literal(str(t), str(t.datatype) if t.datatype else None, t.language)

        for s, p, o, c in g.quads():
            gterm = model.DEFAULT_GRAPH if c == default_id else conv(c)
            ds.add((conv(s), conv(p), conv(o), gterm), 0, "")
        return ds

    def test_rdflib_agrees_on_fixtures(self):
        for name in ("base.nq", "renamed.nq", "changed.nq", "symmetric.nq",
                     "symmetric-renamed.nq", "nonisomorphic.nq"):
            path = os.path.join(FIXTURES, name)
            mine = canonicalize(parse_nquads_file(path)).canonical_bytes
            theirs = canonicalize(self._from_rdflib(path=path)).canonical_bytes
            self.assertEqual(mine, theirs, name)

    def test_rdflib_agrees_on_nasty_terms(self):
        rng = random.Random(42)
        lexicals = ['a\\b"c\nd\te\rf\bg\fh', 'é样本\U0001F600', '\x00\x01\x1f\x7f',
                    "quote'single", 'trailing\\', '"', '\\\\', '_:looksLikeLabel', '<iri-looking>']
        iris = ['https://ex.invalid/a b', 'https://ex.invalid/üñï',
                'https://ex.invalid/{}|^`"', 'https://ex.invalid/\x01\x7f']
        dts = ['http://www.w3.org/2001/XMLSchema#integer', 'https://ex.invalid/dt']
        for _ in range(120):
            ds = model.Dataset()
            for i in range(rng.randint(1, 8)):
                r = rng.random()
                if r < 0.35:
                    o = model.literal(rng.choice(lexicals), rng.choice(dts))
                elif r < 0.6:
                    o = model.literal(rng.choice(lexicals), lang=rng.choice(['en', 'ZH-cn']))
                elif r < 0.8:
                    o = model.literal(rng.choice(lexicals))
                else:
                    o = model.iri(rng.choice(iris))
                s = model.bnode(f"b{rng.randint(0, 3)}") if rng.random() < 0.6 else model.iri(rng.choice(iris))
                if rng.random() < 0.6:
                    g = model.DEFAULT_GRAPH
                else:
                    g = model.bnode('b0') if rng.random() < 0.3 else model.iri(rng.choice(iris))
                ds.add((s, model.iri(rng.choice(iris)), o, g), i, "")
            text = model.dataset_to_nquads(ds)
            mine = canonicalize(parse_nquads(text)).canonical_bytes
            theirs = canonicalize(self._from_rdflib(text=text)).canonical_bytes
            self.assertEqual(mine, theirs, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
