"""Independent tests for the OSM -> directed turn graph builder.

These tests parse/verify the produced graph without re-using the builder's
internal indexes: candidate turns are re-enumerated from the emitted edges and
restriction outcomes are written out by hand from the fixture XML.
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build as builder  # noqa: E402

FIXTURES = ROOT / "fixtures"


def load(name: str):
    return FIXTURES / name


def edge_index(graph: dict) -> dict[str, dict]:
    return {edge["id"]: edge for edge in graph["directed_edges"]}


def turn_set(turns: list[dict]) -> set[tuple[str, str, int]]:
    return {(turn["from_edge"], turn["to_edge"], turn["via_node_id"]) for turn in turns}


def enumerate_candidates(graph: dict) -> set[tuple[str, str, int]]:
    """Independent: a turn exists iff previous edge end == next edge start."""
    edges = graph["directed_edges"]
    result = set()
    for first in edges:
        for second in edges:
            if first["end_node_id"] == second["start_node_id"]:
                result.add((first["id"], second["id"], first["end_node_id"]))
    return result


def write_osm(path: Path, nodes: list[dict], ways: list[dict], relations: list[dict] | None = None):
    root = ET.Element("osm", version="0.6")
    for node in nodes:
        attrs = {"id": str(node["id"])}
        if "lat" in node:
            attrs["lat"] = str(node["lat"])
        if "lon" in node:
            attrs["lon"] = str(node["lon"])
        element = ET.SubElement(root, "node", attrs)
        for key, value in node.get("tags", {}).items():
            ET.SubElement(element, "tag", k=key, v=str(value))
    for way in ways:
        element = ET.SubElement(root, "way", id=str(way["id"]))
        for ref in way["nds"]:
            ET.SubElement(element, "nd", ref=str(ref))
        for key, value in way.get("tags", {}).items():
            ET.SubElement(element, "tag", k=key, v=str(value))
    for relation in relations or []:
        element = ET.SubElement(root, "relation", id=str(relation["id"]))
        for member in relation["members"]:
            ET.SubElement(
                element,
                "member",
                type=member["type"],
                ref=str(member["ref"]),
                role=member["role"],
            )
        for key, value in relation.get("tags", {}).items():
            ET.SubElement(element, "tag", k=key, v=str(value))
    tree = ET.ElementTree(root)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def make_plan(path: Path, **overrides):
    plan = json.loads((FIXTURES / "plan.json").read_text())
    plan.update(overrides)
    path.write_text(json.dumps(plan), encoding="utf-8")
    return plan


class RoadsFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = builder.build(load("roads.osm"), load("plan.json"))
        cls.edges = edge_index(cls.graph)

    def test_summary_counts(self):
        summary = self.graph["summary"]
        self.assertEqual(summary["accepted_way_count"], 9)
        self.assertEqual(summary["physical_segment_count"], 17)
        self.assertEqual(summary["directed_edge_count"], 28)
        self.assertEqual(summary["candidate_turn_count"], 49)
        self.assertEqual(summary["allowed_turn_count"], 44)
        self.assertEqual(summary["restricted_turn_count"], 5)
        self.assertEqual(summary["filtered_way_count"], 1)
        self.assertTrue(summary["default_u_turn_allowed"])

    def test_verification_passes(self):
        self.assertTrue(self.graph["verification"]["ok"], self.graph["verification"]["errors"])

    def test_filtered_footway(self):
        self.assertEqual(
            [(item["way_id"], item["reason"]) for item in self.graph["filtered_ways"]],
            [(90, "highway_not_in_plan")],
        )
        self.assertFalse(any(edge["way_id"] == 90 for edge in self.graph["directed_edges"]))
        diag = [d for d in self.graph["diagnostics"] if d.get("way_id") == 90]
        self.assertEqual(len(diag), 1)
        self.assertEqual(diag[0]["code"], "highway_not_in_plan")

    def test_every_edge_traces_to_way_and_adjacent_nd_pair(self):
        ways = {way["way_id"]: way for way in self.graph["provenance"]["ways"]}
        for edge in self.graph["directed_edges"]:
            source = self.graph["edge_sources"][edge["id"]]
            nds = ways[source["way_id"]]["nds"]
            i = source["segment_index"]
            pair = [nds[i], nds[i + 1]]
            self.assertEqual(source["segment_nds"], pair)
            if edge["direction"] == "forward":
                self.assertEqual((edge["start_node_id"], edge["end_node_id"]), tuple(pair))
            else:
                self.assertEqual((edge["start_node_id"], edge["end_node_id"]), (pair[1], pair[0]))
            self.assertEqual(edge["physical_segment_id"], f"seg-{edge['way_id']}-{i}")

    def test_oneway_reverse_way_80(self):
        way80 = [edge for edge in self.graph["directed_edges"] if edge["way_id"] == 80]
        self.assertEqual(len(way80), 2)
        self.assertTrue(all(edge["direction"] == "reverse" for edge in way80))
        self.assertEqual(
            sorted((edge["start_node_id"], edge["end_node_id"]) for edge in way80),
            [(32, 31), (33, 32)],
        )

    def test_roundabout_default_forward(self):
        way60 = [edge for edge in self.graph["directed_edges"] if edge["way_id"] == 60]
        self.assertEqual(len(way60), 4)
        self.assertTrue(all(edge["direction"] == "forward" for edge in way60))
        self.assertTrue(all(edge.get("roundabout") is True for edge in way60))
        self.assertEqual(
            sorted((edge["start_node_id"], edge["end_node_id"]) for edge in way60),
            [(21, 22), (22, 23), (23, 24), (24, 21)],
        )

    def test_bridge_layer_preserved_but_no_cross_connection(self):
        way50 = [edge for edge in self.graph["directed_edges"] if edge["way_id"] == 50]
        self.assertTrue(way50)
        for edge in way50:
            self.assertEqual(edge.get("bridge"), "yes")
            self.assertEqual(edge.get("layer"), "1")
        # Way 50 crosses way 10 geometrically but shares no node id with it.
        node_ids_50 = {11, 12}
        edges_10 = [edge for edge in self.graph["directed_edges"] if edge["way_id"] == 10]
        self.assertFalse(node_ids_50 & {n for e in edges_10 for n in (e["start_node_id"], e["end_node_id"])})
        # No turn may join way 50 and way 10.
        for turn in self.graph["allowed_turns"]:
            from_edge = self.edges[turn["from_edge"]]
            to_edge = self.edges[turn["to_edge"]]
            if {from_edge["way_id"], to_edge["way_id"]} == {50, 10}:
                self.fail("invented a connection between crossing ways 50 and 10")

    def test_same_coordinate_different_id_not_merged(self):
        nodes = {node["id"]: node for node in self.graph["nodes"]}
        self.assertEqual((nodes[3]["lat"], nodes[3]["lon"]), (nodes[13]["lat"], nodes[13]["lon"]))
        self.assertIn(13, nodes)
        edges_13 = [e for e in self.graph["directed_edges"] if 13 in (e["start_node_id"], e["end_node_id"])]
        self.assertTrue(edges_13)
        self.assertFalse(any(3 in (e["start_node_id"], e["end_node_id"]) for e in edges_13))
        # no turn connects way 51 (node 13) with ways incident to node 3
        for turn in self.graph["allowed_turns"]:
            ways = {self.edges[turn["from_edge"]]["way_id"], self.edges[turn["to_edge"]]["way_id"]}
            self.assertFalse(51 in ways and ways & {10, 20, 30, 40, 50})

    def test_candidate_partition_and_connection_proof(self):
        candidates = enumerate_candidates(self.graph)
        allowed = turn_set(self.graph["allowed_turns"])
        restricted = turn_set(self.graph["restricted_turns"])
        self.assertEqual(len(candidates), 49)
        self.assertFalse(allowed & restricted)
        self.assertEqual(allowed | restricted, candidates)
        for turn in self.graph["allowed_turns"]:
            first = self.edges[turn["from_edge"]]
            second = self.edges[turn["to_edge"]]
            self.assertEqual(first["end_node_id"], second["start_node_id"])
            self.assertEqual(first["end_node_id"], turn["via_node_id"])
            self.assertTrue(turn["connection_proof"]["same_node_id"])
        for turn in self.graph["restricted_turns"]:
            first = self.edges[turn["from_edge"]]
            second = self.edges[turn["to_edge"]]
            self.assertEqual(first["end_node_id"], second["start_node_id"])

    def test_relation_100_no_left_turn(self):
        expected = {("e-10-1-f", "e-30-0-f", 3)}
        got = {
            (t["from_edge"], t["to_edge"], t["via_node_id"])
            for t in self.graph["restricted_turns"]
            if t["relation_id"] == 100
        }
        self.assertEqual(got, expected)
        self.assertIn(("e-10-1-f", "e-20-0-f", 3), turn_set(self.graph["allowed_turns"]))
        self.assertIn(("e-10-1-f", "e-40-0-f", 3), turn_set(self.graph["allowed_turns"]))

    def test_relation_101_only_right_turn_intersection(self):
        blocked = {
            (t["from_edge"], t["to_edge"])
            for t in self.graph["restricted_turns"]
            if t["relation_id"] == 101
        }
        self.assertEqual(
            blocked,
            {
                ("e-20-0-r", "e-10-1-r"),
                ("e-20-0-r", "e-20-0-f"),
                ("e-20-0-r", "e-30-0-f"),
            },
        )
        self.assertIn(("e-20-0-r", "e-40-0-f", 3), turn_set(self.graph["allowed_turns"]))
        # other incoming states at node 3 are unaffected
        self.assertIn(("e-30-0-r", "e-40-0-f", 3), turn_set(self.graph["allowed_turns"]))
        self.assertIn(("e-40-0-r", "e-10-1-r", 3), turn_set(self.graph["allowed_turns"]))

    def test_relation_102_same_way_no_u_turn_keeps_continuations(self):
        blocked = {
            (t["from_edge"], t["to_edge"])
            for t in self.graph["restricted_turns"]
            if t["relation_id"] == 102
        }
        self.assertEqual(blocked, {("e-10-1-f", "e-10-1-r")})
        allowed = turn_set(self.graph["allowed_turns"])
        # The reverse segment is a U-turn; forward continuations must survive.
        self.assertIn(("e-10-1-f", "e-20-0-f", 3), allowed)
        self.assertIn(("e-10-1-f", "e-40-0-f", 3), allowed)
        # It removes only that one edge, not every segment of way 10.
        self.assertIn(("e-10-0-f", "e-10-1-f", 2), allowed)
        self.assertIn(("e-10-1-f", "e-20-0-f", 3), allowed)

    def test_restricted_turns_have_relation_sources(self):
        for turn in self.graph["restricted_turns"]:
            self.assertIn("relation_source", turn)
            self.assertEqual(turn["relation_source"]["relation_id"], turn["relation_id"])
            self.assertEqual(
                turn["restriction"], turn["relation_source"]["tags"]["restriction"]
            )

    def test_restriction_classification(self):
        restrictions = self.graph["restrictions"]
        self.assertEqual([r["relation_id"] for r in restrictions["applied"]], [100, 101, 102])
        self.assertEqual(restrictions["unsupported"], [])
        self.assertEqual(restrictions["ineffective"], [])
        self.assertEqual(restrictions["ignored_non_restriction_relations"], [])

    def test_evidence_reports_coincident_ids_and_crossing(self):
        evidence = self.graph["evidence"]
        coincident = evidence["coincident_coordinates_unmerged"]
        self.assertEqual(len(coincident), 1)
        self.assertEqual(coincident[0]["node_ids"], [3, 13])
        self.assertFalse(coincident[0]["merged"])
        crossings = evidence["geometric_intersections_without_shared_node"]
        pairs = {
            (item["first_way_id"], item["second_way_id"]) for item in crossings
        }
        # Way 50 crosses the way-10 corridor without a shared node.
        self.assertIn((10, 50), pairs)
        for item in crossings:
            self.assertFalse(item["connected"])
            self.assertEqual(item["shared_node_ids"], [])
        bridges = {item["way_id"]: item for item in evidence["bridge_layer_tags"]}
        self.assertEqual(bridges[50]["bridge"], "yes")
        self.assertEqual(bridges[50]["layer"], "1")

    def test_nodes_only_contain_used_node_ids(self):
        used = {nd for way in self.graph["provenance"]["ways"] for nd in way["nds"]}
        self.assertEqual({node["id"] for node in self.graph["nodes"]}, used)
        self.assertNotIn(10, {node["id"] for node in self.graph["nodes"]})



class UnsupportedFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = builder.build(load("unsupported.osm"), load("plan.json"))

    def test_via_way_relation_is_unsupported_not_ignored(self):
        unsupported = self.graph["restrictions"]["unsupported"]
        self.assertEqual([entry["relation_id"] for entry in unsupported], [199])
        self.assertIn("unsupported_via_way_restriction", unsupported[0]["codes"])
        self.assertIn(199, [d.get("relation_id") for d in self.graph["diagnostics"]])
        self.assertTrue(self.graph["verification"]["ok"])

    def test_graph_identity_matches_roads_fixture(self):
        # The extra via-way relation is diagnosed but must not change the graph.
        roads = builder.build(load("roads.osm"), load("plan.json"))
        self.assertEqual(
            roads["graph_identity_hash"], self.graph["graph_identity_hash"]
        )
        self.assertNotEqual(roads["content_hash"], self.graph["content_hash"])
        self.assertFalse(self.graph["summary"]["complete_for_supported_scope"])
        self.assertTrue(roads["summary"]["complete_for_supported_scope"])

    def test_via_way_relation_has_no_effect(self):
        # Only relations 100-102 act; 199 must contribute no restriction.
        self.assertEqual(
            sorted({t["relation_id"] for t in self.graph["restricted_turns"]}),
            [100, 101, 102],
        )


class CliTests(unittest.TestCase):
    def test_strict_mode_flags_incomplete_scope(self):
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "build.py"),
                str(FIXTURES / "unsupported.osm"),
                str(FIXTURES / "plan.json"),
                "--strict",
                "-o",
                str(FIXTURES.parent / ".runtime-test-output.json"),
            ],
            capture_output=True,
            text=True,
        )
        try:
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("complete_for_supported_scope=False", result.stdout)
        finally:
            (FIXTURES.parent / ".runtime-test-output.json").unlink(missing_ok=True)


class DeterminismTests(unittest.TestCase):
    def test_shuffled_xml_element_order_does_not_change_graph_identity(self):
        original = builder.build(load("roads.osm"), load("plan.json"))
        tree = ET.parse(load("roads.osm"))
        root = tree.getroot()
        children = list(root)
        random.Random(20261115).shuffle(children)
        for child in list(root):
            root.remove(child)
        for child in children:
            root.append(child)

        with tempfile.TemporaryDirectory() as tmp:
            shuffled_path = Path(tmp) / "shuffled.osm"
            tree.write(shuffled_path, encoding="utf-8", xml_declaration=True)
            shuffled = builder.build(shuffled_path, load("plan.json"))

        self.assertEqual(original["content_hash"], shuffled["content_hash"])
        self.assertEqual(
            original["graph_identity_hash"], shuffled["graph_identity_hash"]
        )
        self.assertEqual(
            [edge["id"] for edge in original["directed_edges"]],
            [edge["id"] for edge in shuffled["directed_edges"]],
        )
        self.assertEqual(
            [t["from_edge"] + ">" + t["to_edge"] for t in original["allowed_turns"]],
            [t["from_edge"] + ">" + t["to_edge"] for t in shuffled["allowed_turns"]],
        )

    def test_repeat_build_is_stable(self):
        first = builder.build(load("roads.osm"), load("plan.json"))
        second = builder.build(load("roads.osm"), load("plan.json"))
        self.assertEqual(first["content_hash"], second["content_hash"])


class WayOrderTests(unittest.TestCase):
    def test_segment_identity_follows_nd_order_not_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            osm = tmp / "order.osm"
            # Coordinates deliberately decrease while nd order increases.
            write_osm(
                osm,
                [
                    {"id": 1, "lat": 0.0, "lon": 0.004},
                    {"id": 2, "lat": 0.0, "lon": 0.003},
                    {"id": 3, "lat": 0.0, "lon": 0.002},
                    {"id": 4, "lat": 0.0, "lon": 0.001},
                ],
                [
                    {
                        "id": 7,
                        "nds": [1, 2, 3, 4],
                        "tags": {"highway": "residential", "oneway": "yes"},
                    }
                ],
            )
            graph = builder.build(osm, load("plan.json"))
        sources = [graph["edge_sources"][edge["id"]] for edge in graph["directed_edges"]]
        self.assertEqual([source["segment_index"] for source in sources], [0, 1, 2])
        self.assertEqual(
            [source["segment_nds"] for source in sources],
            [[1, 2], [2, 3], [3, 4]],
        )
        self.assertEqual(
            [(edge["start_node_id"], edge["end_node_id"]) for edge in graph["directed_edges"]],
            [(1, 2), (2, 3), (3, 4)],
        )


class OnewayTests(unittest.TestCase):
    def test_oneway_value_handling(self):
        values = {
            "yes": 1,
            "1": 1,
            "true": 1,
            "-1": 1,
            "no": 2,
            "0": 2,
            "false": 2,
            None: 2,
        }
        nodes = [
            {"id": 1, "lat": 0.0, "lon": 0.0},
            {"id": 2, "lat": 0.0, "lon": 0.001},
        ]
        ways = []
        for index, value in enumerate(values):
            tags = {"highway": "residential"}
            if value is not None:
                tags["oneway"] = value
            ways.append({"id": 100 + index, "nds": [1, 2], "tags": tags})
        with tempfile.TemporaryDirectory() as tmp:
            osm = Path(tmp) / "oneway.osm"
            write_osm(osm, nodes, ways)
            graph = builder.build(osm, load("plan.json"))

        edges = edge_index(graph)
        for index, value in enumerate(values):
            way_id = 100 + index
            way_edges = [e for e in graph["directed_edges"] if e["way_id"] == way_id]
            self.assertEqual(len(way_edges), values[value], f"oneway={value!r}")
            if value == "-1":
                self.assertEqual(way_edges[0]["direction"], "reverse")
                self.assertEqual(
                    (way_edges[0]["start_node_id"], way_edges[0]["end_node_id"]), (2, 1)
                )
            else:
                self.assertEqual(way_edges[0]["direction"], "forward")
                self.assertEqual(
                    (way_edges[0]["start_node_id"], way_edges[0]["end_node_id"]), (1, 2)
                )

    def test_roundabout_default_is_plan_driven(self):
        nodes = [{"id": 1, "lat": 0.0, "lon": 0.0}, {"id": 2, "lat": 0.0, "lon": 0.001}]
        ways = [{"id": 1, "nds": [1, 2], "tags": {"highway": "residential", "junction": "roundabout"}}]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            osm = tmp / "roundabout.osm"
            write_osm(osm, nodes, ways)
            plan_true = tmp / "true.json"
            plan_false = tmp / "false.json"
            make_plan(plan_true, roundabout_default_oneway=True)
            make_plan(plan_false, roundabout_default_oneway=False)
            forward_only = builder.build(osm, plan_true)
            bidirectional = builder.build(osm, plan_false)
        self.assertEqual([e["direction"] for e in forward_only["directed_edges"]], ["forward"])
        self.assertEqual(
            sorted(e["direction"] for e in bidirectional["directed_edges"]),
            ["forward", "reverse"],
        )


class SmallJunctionEnumerationTests(unittest.TestCase):
    """Enumerate a small 4-arm junction by hand and compare with the turn table."""

    ARMS = {
        10: (-0.001, 0.0),
        20: (0.001, 0.0),
        30: (0.0, 0.001),
        40: (0.0, -0.001),
    }

    @classmethod
    def setUpClass(cls):
        cls.nodes = [{"id": 1, "lat": 0.0, "lon": 0.0}]
        cls.ways = []
        for arm_id, (lat, lon) in cls.ARMS.items():
            cls.nodes.append({"id": arm_id, "lat": lat, "lon": lon})
            cls.ways.append(
                {"id": arm_id, "nds": [arm_id, 1], "tags": {"highway": "residential"}}
            )
        # Every arm is nds [arm_node, 1] and bidirectional:
        #   entering the junction: e-<arm>-0-f (arm node -> 1)
        #   leaving  the junction: e-<arm>-0-r (1 -> arm node)
        cls.entering = {f"e-{arm}-0-f" for arm in cls.ARMS}
        cls.leaving = {f"e-{arm}-0-r" for arm in cls.ARMS}

    def _build(self, relations):
        with tempfile.TemporaryDirectory() as tmp:
            osm = Path(tmp) / "junction.osm"
            write_osm(osm, self.nodes, self.ways, relations)
            return builder.build(osm, load("plan.json"))

    def test_unrestricted_junction_has_full_cross_product(self):
        graph = self._build([])
        expected = {
            (entering, leaving, 1)
            for entering, leaving in product(self.entering, self.leaving)
        }
        # Each arm's free end is also a node where its own two directed edges
        # meet, so a U-turn at the dead end is one more candidate turn.
        for arm in self.ARMS:
            expected.add((f"e-{arm}-0-r", f"e-{arm}-0-f", arm))
        self.assertEqual(turn_set(graph["allowed_turns"]), expected)
        self.assertEqual(len(expected), 20)  # 4x4 at the via + 4 dead-end U-turns
        self.assertEqual(graph["restricted_turns"], [])
        self.assertEqual(
            {edge["id"] for edge in graph["directed_edges"]},
            self.entering | self.leaving,
        )

    def test_only_rule_reduces_one_incoming_state_only(self):
        relation = {
            "id": 1,
            "members": [
                {"type": "way", "ref": 10, "role": "from"},
                {"type": "node", "ref": 1, "role": "via"},
                {"type": "way", "ref": 20, "role": "to"},
            ],
            "tags": {"type": "restriction", "restriction": "only_straight_on"},
        }
        graph = self._build([relation])
        allowed = turn_set(graph["allowed_turns"])
        restricted = turn_set(graph["restricted_turns"])
        # Entering from arm 10 only e-10-0-f is directed towards the via.
        self.assertEqual(
            {key for key in allowed if key[0] == "e-10-0-f"},
            {("e-10-0-f", "e-20-0-r", 1)},
        )
        self.assertEqual(
            {key for key in restricted if key[0] == "e-10-0-f"},
            {
                ("e-10-0-f", "e-10-0-r", 1),
                ("e-10-0-f", "e-30-0-r", 1),
                ("e-10-0-f", "e-40-0-r", 1),
            },
        )
        # The other three incoming states keep all four exits each.
        for entering in self.entering - {"e-10-0-f"}:
            self.assertEqual(
                {key for key in allowed if key[0] == entering},
                {(entering, leaving, 1) for leaving in self.leaving},
            )
        # 4x4 junction turns minus the 3 disallowed = 13, plus the 4 dead-end
        # U-turns that the only_* rule (scoped to the via node) cannot touch.
        self.assertEqual(len(allowed), 17)

    def test_reverse_oneway_arm_has_no_entering_or_leaving_state(self):
        ways = [
            {"id": 10, "nds": [10, 1], "tags": {"highway": "residential", "oneway": "-1"}},
            {"id": 20, "nds": [20, 1], "tags": {"highway": "residential"}},
        ]
        nodes = [
            {"id": 1, "lat": 0.0, "lon": 0.0},
            {"id": 10, "lat": -0.001, "lon": 0.0},
            {"id": 20, "lat": 0.001, "lon": 0.0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            osm = Path(tmp) / "reverse_arm.osm"
            write_osm(osm, nodes, ways)
            graph = builder.build(osm, load("plan.json"))
        edge_ids = {edge["id"] for edge in graph["directed_edges"]}
        # The reverse one-way arm contributes only e-10-0-r (node 1 -> 10);
        # nothing leaves the junction along arm 10 and nothing enters it.
        self.assertNotIn("e-10-0-f", edge_ids)
        allowed = turn_set(graph["allowed_turns"])
        self.assertIn(("e-20-0-f", "e-10-0-r", 1), allowed)
        self.assertNotIn(("e-10-0-r", "e-10-0-r", 1), allowed)


class RestrictionSemanticsTests(unittest.TestCase):
    def _star_graph(self, relations):
        """Node 1 is the via; ways A/B/C/D are bidirectional one-segment arms."""
        nodes = [{"id": 1, "lat": 0.0, "lon": 0.0}]
        arms = {10: ((-0.001, 0.0),), 20: ((0.001, 0.0),), 30: ((0.0, 0.001),), 40: ((0.0, -0.001),)}
        ways = []
        for way_id, (coord,) in arms.items():
            lat, lon = coord
            nodes.append({"id": way_id, "lat": lat, "lon": lon})
            ways.append({"id": way_id, "nds": [way_id, 1], "tags": {"highway": "residential"}})
        with tempfile.TemporaryDirectory() as tmp:
            osm = Path(tmp) / "star.osm"
            write_osm(osm, nodes, ways, relations)
            return builder.build(osm, load("plan.json"))

    @staticmethod
    def relation(rel_id, from_way, to_way, restriction, via=1):
        return {
            "id": rel_id,
            "members": [
                {"type": "way", "ref": from_way, "role": "from"},
                {"type": "node", "ref": via, "role": "via"},
                {"type": "way", "ref": to_way, "role": "to"},
            ],
            "tags": {"type": "restriction", "restriction": restriction},
        }

    def test_only_rules_intersect_to_empty(self):
        graph = self._star_graph(
            [
                self.relation(1, 10, 20, "only_left_turn"),
                self.relation(2, 10, 30, "only_right_turn"),
            ]
        )
        allowed = turn_set(graph["allowed_turns"])
        # Entering node 1 from way 10 has two contradictory only rules => nothing.
        self.assertFalse(any(key[0] == "e-10-0-f" for key in allowed))
        # Other entering states are untouched.
        self.assertIn(("e-20-0-f", "e-20-0-r", 1), allowed)
        self.assertTrue(graph["verification"]["ok"], graph["verification"]["errors"])

    def test_only_and_no_combine(self):
        graph = self._star_graph(
            [
                self.relation(1, 10, 20, "only_left_turn"),
                self.relation(2, 10, 20, "no_straight_on"),
            ]
        )
        allowed = turn_set(graph["allowed_turns"])
        self.assertFalse(any(key[0] == "e-10-0-f" for key in allowed))

    def test_no_u_turn_from_equals_to_spares_forward_continuation(self):
        # Extend way 10 through node 1 so there is a forward continuation.
        nodes = [
            {"id": 1, "lat": 0.0, "lon": 0.0},
            {"id": 2, "lat": 0.0, "lon": 0.001},
            {"id": 3, "lat": 0.0, "lon": 0.002},
            {"id": 4, "lat": 0.0, "lon": -0.001},
        ]
        ways = [
            {"id": 10, "nds": [2, 1, 3], "tags": {"highway": "residential"}},
            {"id": 20, "nds": [4, 1], "tags": {"highway": "residential"}},
        ]
        relations = [self.relation(5, 10, 10, "no_u_turn")]
        with tempfile.TemporaryDirectory() as tmp:
            osm = Path(tmp) / "uturn.osm"
            write_osm(osm, nodes, ways, relations)
            graph = builder.build(osm, load("plan.json"))
        allowed = turn_set(graph["allowed_turns"])
        restricted = turn_set(graph["restricted_turns"])
        # Incoming forward segment e-10-0-f ends at via; its reverse is e-10-0-r.
        self.assertIn(("e-10-0-f", "e-10-0-r", 1), restricted)
        self.assertNotIn(("e-10-0-f", "e-10-0-r", 1), allowed)
        # Forward continuation on the same way survives, as do other branches.
        self.assertIn(("e-10-0-f", "e-10-1-f", 1), allowed)
        # Way 20 is nds [4, 1], so its directed edge leaving node 1 is the reverse.
        self.assertIn(("e-10-0-f", "e-20-0-r", 1), allowed)
        self.assertEqual(graph["verification"]["ok"], True)

    def test_ineffective_restriction_due_to_oneway_entering(self):
        # Way 10 is one-way away from node 1, so nothing enters the via on it.
        nodes = [
            {"id": 1, "lat": 0.0, "lon": 0.0},
            {"id": 2, "lat": 0.0, "lon": 0.001},
            {"id": 3, "lat": 0.0, "lon": -0.001},
        ]
        ways = [
            {"id": 10, "nds": [1, 2], "tags": {"highway": "residential", "oneway": "yes"}},
            {"id": 20, "nds": [1, 3], "tags": {"highway": "residential"}},
        ]
        relations = [self.relation(7, 10, 20, "no_left_turn")]
        with tempfile.TemporaryDirectory() as tmp:
            osm = Path(tmp) / "ineffective.osm"
            write_osm(osm, nodes, ways, relations)
            graph = builder.build(osm, load("plan.json"))
        ineffective = graph["restrictions"]["ineffective"]
        self.assertEqual([entry["relation_id"] for entry in ineffective], [7])
        self.assertIn("restriction_has_no_entering_edge", ineffective[0]["codes"])
        self.assertEqual(graph["restrictions"]["applied"], [])
        self.assertTrue(graph["verification"]["ok"])

    def test_ineffective_restriction_due_to_oneway_exit(self):
        nodes = [
            {"id": 1, "lat": 0.0, "lon": 0.0},
            {"id": 2, "lat": 0.0, "lon": 0.001},
            {"id": 3, "lat": 0.0, "lon": -0.001},
        ]
        ways = [
            {"id": 10, "nds": [2, 1], "tags": {"highway": "residential"}},
            # the to-way's only permitted direction points into node 1, so
            # there is no directed exit on the to-way at the via.
            {"id": 20, "nds": [3, 1], "tags": {"highway": "residential", "oneway": "yes"}},
        ]
        relations = [self.relation(8, 10, 20, "no_left_turn")]
        with tempfile.TemporaryDirectory() as tmp:
            osm = Path(tmp) / "exit.osm"
            write_osm(osm, nodes, ways, relations)
            graph = builder.build(osm, load("plan.json"))
        ineffective = graph["restrictions"]["ineffective"]
        self.assertEqual([entry["relation_id"] for entry in ineffective], [8])
        self.assertIn("restriction_has_no_exit_on_to_way", ineffective[0]["codes"])
        self.assertTrue(graph["verification"]["ok"])


class UnsupportedStructureTests(unittest.TestCase):
    def _build(self, relations, ways=None, nodes=None):
        nodes = nodes or [
            {"id": 1, "lat": 0.0, "lon": 0.0},
            {"id": 2, "lat": 0.0, "lon": 0.001},
            {"id": 3, "lat": 0.0, "lon": -0.001},
            {"id": 4, "lat": 0.001, "lon": 0.0},
        ]
        ways = ways or [
            {"id": 10, "nds": [2, 1], "tags": {"highway": "residential"}},
            {"id": 20, "nds": [1, 3], "tags": {"highway": "residential"}},
            {"id": 30, "nds": [1, 4], "tags": {"highway": "residential"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            osm = Path(tmp) / "structures.osm"
            write_osm(osm, nodes, ways, relations)
            return builder.build(osm, load("plan.json"))

    @staticmethod
    def rel(rel_id, from_way=None, to_way=None, via=1, restriction="no_left_turn", **extra):
        members = []
        if from_way is not None:
            members.append({"type": "way", "ref": from_way, "role": "from"})
        members.append({"type": "node", "ref": via, "role": "via"})
        if to_way is not None:
            members.append({"type": "way", "ref": to_way, "role": "to"})
        tags = {"type": "restriction"}
        if restriction is not None:
            tags["restriction"] = restriction
        tags.update(extra)
        return {"id": rel_id, "members": members, "tags": tags}

    def test_missing_from_way(self):
        graph = self._build([self.rel(1, 999, 20)])
        unsupported = graph["restrictions"]["unsupported"]
        self.assertEqual([e["relation_id"] for e in unsupported], [1])
        self.assertIn("missing_way_reference", unsupported[0]["codes"])
        self.assertIn(999, [d.get("from_way_id") for d in graph["diagnostics"]])

    def test_missing_via_node(self):
        graph = self._build([self.rel(2, 10, 20, via=999)])
        self.assertIn("missing_node_reference", graph["restrictions"]["unsupported"][0]["codes"])
        self.assertIn(999, [d.get("via_node_id") for d in graph["diagnostics"]])

    def test_conditional_restriction(self):
        graph = self._build([self.rel(3, 10, 20, **{"restriction:conditional": "no_left_turn @ (Mo-Fr 08:00-18:00)"})])
        self.assertIn(
            "unsupported_conditional_restriction",
            graph["restrictions"]["unsupported"][0]["codes"],
        )

    def test_except_tag(self):
        graph = self._build([self.rel(4, 10, 20, **{"except": "bus"})])
        self.assertIn("unsupported_except", graph["restrictions"]["unsupported"][0]["codes"])

    def test_unsupported_restriction_value(self):
        graph = self._build([self.rel(5, 10, 20, restriction="no_entry")])
        self.assertIn(
            "unsupported_restriction_type",
            graph["restrictions"]["unsupported"][0]["codes"],
        )

    def test_from_to_same_way_for_non_uturn(self):
        graph = self._build([self.rel(6, 10, 10, restriction="no_left_turn")])
        self.assertIn(
            "unsupported_same_way_restriction",
            graph["restrictions"]["unsupported"][0]["codes"],
        )

    def test_interior_via_on_to_way(self):
        ways = [
            {"id": 10, "nds": [2, 1], "tags": {"highway": "residential"}},
            {"id": 20, "nds": [1, 3, 4], "tags": {"highway": "residential"}},
        ]
        # via node 3 is interior to way 20, not an endpoint.
        graph = self._build([self.rel(9, 10, 20, via=3)], ways=ways)
        self.assertIn(
            "via_not_endpoint_of_to_way",
            graph["restrictions"]["unsupported"][0]["codes"],
        )

    def test_restriction_referencing_filtered_way(self):
        ways = [
            {"id": 10, "nds": [2, 1], "tags": {"highway": "residential"}},
            {"id": 20, "nds": [1, 3], "tags": {"highway": "footway"}},
        ]
        graph = self._build([self.rel(11, 10, 20)], ways=ways)
        self.assertIn(
            "restriction_references_filtered_way",
            graph["restrictions"]["unsupported"][0]["codes"],
        )
        self.assertEqual([w["way_id"] for w in graph["filtered_ways"]], [20])

    def test_relation_without_type_is_ignored_but_not_silently_counted(self):
        relation = self.rel(12, 10, 20)
        relation["tags"].pop("type")
        graph = self._build([relation])
        # It carries restriction-like structure, so it is diagnosed, not ignored.
        self.assertTrue(graph["diagnostics"])
        self.assertIn(
            "unsupported_relation_type",
            [d["code"] for d in graph["diagnostics"]],
        )
        self.assertTrue(graph["verification"]["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
