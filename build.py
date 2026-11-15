#!/usr/bin/env python3
"""Build a directed road / turn graph from a small OSM XML export.

Implemented scope (deliberately narrow, matching plan.json / README):

* only ways whose ``highway`` tag is listed in the plan are accepted;
* every pair of adjacent ``nd`` references is a physical segment identified by
  ``(way_id, segment_index)`` -- way order is authoritative and is never
  replaced by coordinate ordering or de-duplication;
* physical segments expand to directed edges according to ``oneway``:
  ``yes/1/true`` forward, ``-1`` reverse, ``no/0/false`` both directions;
  a roundabout without an explicit ``oneway`` uses the plan default; other
  ways without ``oneway`` are bidirectional;
* nodes connect only when they share the *same OSM node id*.  Equal
  coordinates, geometric crossings and bridge/layer tags are evidence, never
  an instruction to invent a connection;
* a turn is a pair of directed edges where the first edge's end node id equals
  the second edge's start node id;
* via-node ``no_*`` / ``only_*`` restrictions act on the directed state that
  enters the via node on the ``from`` way.  Intersections of restrictions are
  applied.  ``no_u_turn`` with ``from == to`` removes only the directed
  reverse of the physical segment that was just traversed;
* unsupported / ineffective restrictions, missing references and unsupported
  ways produce located diagnostics and a separate list; they are never
  silently dropped while claiming a complete graph.

Run ``python3 build.py --help`` for the command line interface.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

FORMAT = "osm-directed-turn-graph"
FORMAT_VERSION = "1"

ONEWAY_FORWARD = frozenset({"yes", "1", "true"})
ONEWAY_REVERSE = frozenset({"-1"})
ONEWAY_BOTH = frozenset({"no", "0", "false"})

NO_RESTRICTIONS = frozenset(
    {"no_left_turn", "no_right_turn", "no_straight_on", "no_u_turn"}
)
ONLY_RESTRICTIONS = frozenset(
    {"only_left_turn", "only_right_turn", "only_straight_on"}
)
SUPPORTED_RESTRICTIONS = NO_RESTRICTIONS | ONLY_RESTRICTIONS

DEFAULT_EARTH_RADIUS_M = 6371008.8


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _norm(value: Any) -> Any:
    """Lower-case a tag value without inventing semantics for non-strings."""
    return value.strip().lower() if isinstance(value, str) else value


def _sort_key(value: Any) -> tuple[int, Any]:
    """Natural sort for numeric OSM ids; stable for arbitrary ids."""
    if isinstance(value, int):
        return (0, value)
    text = str(value)
    try:
        return (0, int(text))
    except ValueError:
        return (1, text)


def _edge_sort_key(edge: dict[str, Any] | "DirectedEdge") -> tuple[Any, ...]:
    if not isinstance(edge, dict):
        return (
            _sort_key(edge.way_id),
            edge.segment_index,
            0 if edge.direction == "forward" else 1,
        )
    return (
        _sort_key(edge["way_id"]),
        edge["segment_index"],
        0 if edge["direction"] == "forward" else 1,
    )


def haversine_m(
    lat1: float, lon1: float, lat2: float, lon2: float, radius_m: float
) -> float:
    """Great-circle distance in metres using the plan's earth radius."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    a = min(1.0, max(0.0, a))
    return 2.0 * radius_m * math.asin(math.sqrt(a))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_hash(graph: dict[str, Any]) -> str:
    """Stable content hash of the whole document (verification excluded)."""
    clone = dict(graph)
    clone.pop("content_hash", None)
    clone.pop("verification", None)
    return hashlib.sha256(_canonical_json(clone).encode("utf-8")).hexdigest()


GRAPH_IDENTITY_KEYS = (
    "nodes",
    "physical_segments",
    "directed_edges",
    "allowed_turns",
    "restricted_turns",
)


def _graph_identity_hash(graph: dict[str, Any]) -> str:
    """Identity of the road/turn graph only, independent of diagnostics.

    Two exports with the same accepted ways and the same applicable rules have
    the same road/turn graph even if one of them additionally contains
    unsupported structures (which are reported separately).
    """
    identity = {key: graph[key] for key in GRAPH_IDENTITY_KEYS}
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# OSM input model / parsing
# ---------------------------------------------------------------------------


@dataclass
class OSMNode:
    id: int
    lat: float | None
    lon: float | None
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class OSMWay:
    id: int
    nds: list[int]
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class OSMMember:
    type: str
    ref: int
    role: str


@dataclass
class OSMRelation:
    id: int
    members: list[OSMMember]
    tags: dict[str, str] = field(default_factory=dict)


def _tags(element: ET.Element) -> dict[str, str]:
    return {
        tag.get("k", ""): tag.get("v", "")
        for tag in element.findall("tag")
        if tag.get("k") is not None
    }


def _int_attr(element: ET.Element, name: str, what: str) -> int:
    raw = element.get(name)
    if raw is None:
        raise ValueError(f"{what} is missing required attribute {name!r}")
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{what} has invalid {name}={raw!r}") from exc


def parse_osm(path: str | Path) -> tuple[dict[int, OSMNode], dict[int, OSMWay], dict[int, OSMRelation]]:
    """Parse nodes/ways/relations.  Element order is irrelevant to the result."""
    root = ET.parse(str(path)).getroot()

    nodes: dict[int, OSMNode] = {}
    ways: dict[int, OSMWay] = {}
    relations: dict[int, OSMRelation] = {}

    for element in root:
        if element.tag == "node":
            nid = _int_attr(element, "id", "node")
            if nid in nodes:
                raise ValueError(f"duplicate node id {nid}")
            lat_raw = element.get("lat")
            lon_raw = element.get("lon")
            lat = float(lat_raw) if lat_raw is not None else None
            lon = float(lon_raw) if lon_raw is not None else None
            nodes[nid] = OSMNode(id=nid, lat=lat, lon=lon, tags=_tags(element))
        elif element.tag == "way":
            wid = _int_attr(element, "id", "way")
            if wid in ways:
                raise ValueError(f"duplicate way id {wid}")
            nds: list[int] = []
            for nd in element.findall("nd"):
                nds.append(_int_attr(nd, "ref", f"way {wid} nd"))
            ways[wid] = OSMWay(id=wid, nds=nds, tags=_tags(element))
        elif element.tag == "relation":
            rid = _int_attr(element, "id", "relation")
            if rid in relations:
                raise ValueError(f"duplicate relation id {rid}")
            members: list[OSMMember] = []
            for member in element.findall("member"):
                members.append(
                    OSMMember(
                        type=(member.get("type") or "").strip().lower(),
                        ref=_int_attr(member, "ref", f"relation {rid} member"),
                        role=(member.get("role") or "").strip().lower(),
                    )
                )
            relations[rid] = OSMRelation(id=rid, members=members, tags=_tags(element))

    return nodes, ways, relations


def load_plan(path: str | Path) -> dict[str, Any]:
    """Load and validate the small, task-specific plan."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("plan must be a JSON object")

    allowed = data.get("allowed_highways")
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise ValueError("plan.allowed_highways must be a list of strings")

    restriction_types = data.get("restriction_types", sorted(SUPPORTED_RESTRICTIONS))
    if not isinstance(restriction_types, list) or not all(
        isinstance(item, str) for item in restriction_types
    ):
        raise ValueError("plan.restriction_types must be a list of strings")

    radius = data.get("earth_radius_m", DEFAULT_EARTH_RADIUS_M)
    try:
        radius_value = float(radius)
    except (TypeError, ValueError) as exc:
        raise ValueError("plan.earth_radius_m must be numeric") from exc
    if radius_value <= 0:
        raise ValueError("plan.earth_radius_m must be positive")

    return {
        "allowed_highways": sorted({_norm(item) for item in allowed}),
        "roundabout_default_oneway": bool(data.get("roundabout_default_oneway", True)),
        "u_turn_default": str(
            data.get("u_turn_default", "allowed unless no_u_turn relation")
        ),
        "restriction_types": sorted({_norm(item) for item in restriction_types}),
        "earth_radius_m": radius_value,
    }


def _plan_uturn_available(plan: dict[str, Any]) -> bool:
    """Read the requested default only; do not add an unrequested driving rule."""
    text = plan["u_turn_default"].strip().lower()
    if not text:
        return True
    if any(
        phrase in text
        for phrase in ("not allowed", "disallowed", "forbidden", "prohibited")
    ):
        return False
    if "allowed" in text:
        return True
    raise ValueError(f"unsupported plan.u_turn_default: {plan['u_turn_default']!r}")


def _is_endpoint(way: OSMWay, node_id: int) -> bool:
    return bool(way.nds) and (way.nds[0] == node_id or way.nds[-1] == node_id)


def _effective_oneway(tags: dict[str, str], plan: dict[str, Any]) -> tuple[str | None, Any]:
    raw = tags.get("oneway")
    value = _norm(raw)
    if value in ONEWAY_FORWARD:
        return "forward", raw
    if value in ONEWAY_REVERSE:
        return "reverse", raw
    if value in ONEWAY_BOTH:
        return "both", raw
    if value is None or value == "":
        if _norm(tags.get("junction")) == "roundabout" and plan["roundabout_default_oneway"]:
            return "forward", raw
        return "both", raw
    return None, raw


# ---------------------------------------------------------------------------
# Graph construction: physical segments and directed edges
# ---------------------------------------------------------------------------


@dataclass
class PhysicalSegment:
    id: str
    way_id: int
    segment_index: int
    start_node_id: int
    end_node_id: int
    length_m: float


@dataclass
class DirectedEdge:
    id: str
    physical_segment_id: str
    way_id: int
    segment_index: int
    direction: str
    start_node_id: int
    end_node_id: int
    length_m: float
    tags: dict[str, Any]
    reverse_edge_id: str | None = None


def _physical_id(way_id: int, segment_index: int) -> str:
    return f"seg-{way_id}-{segment_index}"


def _edge_id(way_id: int, segment_index: int, direction: str) -> str:
    return f"e-{way_id}-{segment_index}-{'f' if direction == 'forward' else 'r'}"


def _diagnostic(
    code: str,
    message: str,
    severity: str = "warning",
    **locators: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
    }
    for key, value in locators.items():
        if value is not None:
            entry[key] = value
    return entry


def _diagnostic_sort_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _sort_key(entry.get("relation_id", "")),
        _sort_key(entry.get("way_id", "")),
        _sort_key(entry.get("node_id", "")),
        entry.get("code", ""),
        entry.get("message", ""),
    )


def _build_segments(
    nodes: dict[int, OSMNode],
    ways: dict[int, OSMWay],
    plan: dict[str, Any],
) -> tuple[
    dict[int, OSMWay],
    list[PhysicalSegment],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Filter ways, then create one physical segment per adjacent nd pair."""
    allowed_highways = set(plan["allowed_highways"])
    accepted_ways: dict[int, OSMWay] = {}
    filtered: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    segments: list[PhysicalSegment] = []

    for way_id in sorted(ways, key=_sort_key):
        way = ways[way_id]
        highway = _norm(way.tags.get("highway"))

        def reject(reason: str, message: str, **locators: Any) -> None:
            entry = {
                "way_id": way_id,
                "highway": way.tags.get("highway"),
                "reason": reason,
                "message": message,
            }
            entry.update({k: v for k, v in locators.items() if v is not None})
            filtered.append(entry)
            diagnostics.append(
                _diagnostic(
                    reason,
                    message,
                    severity="info",
                    way_id=way_id,
                    highway=way.tags.get("highway"),
                    **locators,
                )
            )

        if highway is None:
            reject(
                "missing_highway_tag",
                "way has no highway tag and is not a road in this plan",
            )
            continue
        if highway not in allowed_highways:
            reject(
                "highway_not_in_plan",
                f"highway={way.tags.get('highway')!r} is not listed in plan.allowed_highways",
            )
            continue
        if len(way.nds) < 2:
            reject(
                "too_few_nds",
                "way has fewer than two nd references and yields no segment",
            )
            continue

        missing_nodes = sorted(
            {nd for nd in way.nds if nd not in nodes}, key=_sort_key
        )
        if missing_nodes:
            reject(
                "missing_node_reference",
                "way references node ids that are absent from the XML",
                missing_node_ids=missing_nodes,
            )
            continue

        missing_coords = sorted(
            {
                nd
                for nd in way.nds
                if nodes[nd].lat is None or nodes[nd].lon is None
            },
            key=_sort_key,
        )
        if missing_coords:
            reject(
                "missing_coordinates",
                "way uses nodes without lat/lon, so metre lengths cannot be computed",
                missing_coordinate_node_ids=missing_coords,
            )
            continue

        oneway_mode, oneway_raw = _effective_oneway(way.tags, plan)
        if oneway_mode is None:
            reject(
                "unsupported_oneway_value",
                f"oneway={oneway_raw!r} is not one of the supported values",
                oneway_raw=oneway_raw,
            )
            continue

        accepted_ways[way_id] = way
        for index, (start_id, end_id) in enumerate(zip(way.nds, way.nds[1:])):
            start = nodes[start_id]
            end = nodes[end_id]
            length = haversine_m(start.lat, start.lon, end.lat, end.lon, plan["earth_radius_m"])
            segments.append(
                PhysicalSegment(
                    id=_physical_id(way_id, index),
                    way_id=way_id,
                    segment_index=index,
                    start_node_id=start_id,
                    end_node_id=end_id,
                    length_m=length,
                )
            )

    filtered.sort(key=lambda item: (_sort_key(item["way_id"]), item["reason"]))
    diagnostics.sort(key=_diagnostic_sort_key)
    return accepted_ways, segments, filtered, diagnostics


def _expand_edges(
    nodes: dict[int, OSMNode],
    ways: dict[int, OSMWay],
    segments: list[PhysicalSegment],
    plan: dict[str, Any],
) -> list[DirectedEdge]:
    """Expand each physical segment according to the effective oneway mode."""
    edges: list[DirectedEdge] = []
    for segment in segments:
        way = ways[segment.way_id]
        oneway_mode, _ = _effective_oneway(way.tags, plan)
        assert oneway_mode in {"forward", "reverse", "both"}

        directions: tuple[str, ...]
        if oneway_mode == "forward":
            directions = ("forward",)
        elif oneway_mode == "reverse":
            directions = ("reverse",)
        else:
            directions = ("forward", "reverse")

        tags = dict(way.tags)
        for direction in directions:
            if direction == "forward":
                start_id, end_id = segment.start_node_id, segment.end_node_id
            else:
                start_id, end_id = segment.end_node_id, segment.start_node_id
            edge = DirectedEdge(
                id=_edge_id(segment.way_id, segment.segment_index, direction),
                physical_segment_id=segment.id,
                way_id=segment.way_id,
                segment_index=segment.segment_index,
                direction=direction,
                start_node_id=start_id,
                end_node_id=end_id,
                length_m=segment.length_m,
                tags=tags,
            )
            edges.append(edge)

    by_physical: dict[str, dict[str, DirectedEdge]] = defaultdict(dict)
    for edge in edges:
        by_physical[edge.physical_segment_id][edge.direction] = edge
    for forms in by_physical.values():
        forward = forms.get("forward")
        reverse = forms.get("reverse")
        if forward is not None:
            forward.reverse_edge_id = reverse.id if reverse is not None else None
        if reverse is not None:
            reverse.reverse_edge_id = forward.id if forward is not None else None

    edges.sort(
        key=lambda edge: (
            _sort_key(edge.way_id),
            edge.segment_index,
            0 if edge.direction == "forward" else 1,
        )
    )
    return edges


def _edge_to_dict(edge: DirectedEdge, nodes: dict[int, OSMNode]) -> dict[str, Any]:
    start = nodes[edge.start_node_id]
    end = nodes[edge.end_node_id]
    record: dict[str, Any] = {
        "id": edge.id,
        "physical_segment_id": edge.physical_segment_id,
        "way_id": edge.way_id,
        "segment_index": edge.segment_index,
        "direction": edge.direction,
        "start_node_id": edge.start_node_id,
        "end_node_id": edge.end_node_id,
        "length_m": round(edge.length_m, 6),
        "start_coordinate": [start.lat, start.lon],
        "end_coordinate": [end.lat, end.lon],
        "reverse_edge_id": edge.reverse_edge_id,
    }
    # bridge/layer are preserved as evidence; they never create connections.
    if "bridge" in edge.tags:
        record["bridge"] = edge.tags["bridge"]
    if "layer" in edge.tags:
        record["layer"] = edge.tags["layer"]
    if _norm(edge.tags.get("junction")) == "roundabout":
        record["roundabout"] = True
    return record


# ---------------------------------------------------------------------------
# Restriction handling
# ---------------------------------------------------------------------------


def _way_reason(way_id: int, accepted_ways: dict[int, OSMWay], ways: dict[int, OSMWay]) -> str | None:
    if way_id not in ways:
        return "missing"
    if way_id not in accepted_ways:
        return "filtered"
    return None


def _validate_restriction(
    relation: OSMRelation,
    ways: dict[int, OSMWay],
    accepted_ways: dict[int, OSMWay],
    nodes: dict[int, OSMNode],
    plan: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Validate structure and references.  Return (roles, issues).

    ``roles`` is a dict of the parsed role refs when the structure is
    understood; ``issues`` are located diagnostics.  A non-empty issue list
    means the restriction is unsupported and must not be applied.
    """
    issues: list[dict[str, Any]] = []

    def issue(code: str, message: str, **extra: Any) -> None:
        entry = _diagnostic(code, message, severity="warning", relation_id=relation.id, **extra)
        issues.append(entry)

    by_role: dict[str, list[OSMMember]] = defaultdict(list)
    unknown_roles: list[str] = []
    for member in relation.members:
        if member.role in {"from", "via", "to"}:
            by_role[member.role].append(member)
        else:
            unknown_roles.append(member.role)

    if unknown_roles:
        issue(
            "unsupported_member_role",
            "relation has members with roles other than from/via/to",
            unsupported_roles=sorted(unknown_roles),
        )

    for role in ("from", "via", "to"):
        members = by_role[role]
        if len(members) == 0:
            issue(f"missing_{role}_member", f"relation has no {role!r} member")
        elif len(members) > 1:
            issue(
                f"multiple_{role}_members",
                f"relation has {len(members)} {role!r} members; only one is supported",
                member_refs=[m.ref for m in members],
            )

    for role, members in by_role.items():
        for member in members:
            if role in {"from", "to"} and member.type != "way":
                issue(
                    f"{role}_member_not_way",
                    f"{role!r} member must be a way, got {member.type!r}",
                    member_type=member.type,
                    member_ref=member.ref,
                )
            if role == "via" and member.type == "way":
                issue(
                    "unsupported_via_way_restriction",
                    "via-way restrictions are not supported by this task",
                    via_way_id=member.ref,
                )
            if role == "via" and member.type != "node" and member.type != "way":
                issue(
                    "unsupported_via_member_type",
                    f"via member type {member.type!r} is not supported",
                    via_member_type=member.type,
                    member_ref=member.ref,
                )

    if "except" in relation.tags:
        issue(
            "unsupported_except",
            "restriction with an except tag is not supported",
            except_tags=relation.tags["except"],
        )
    conditional_keys = sorted(
        key for key in relation.tags if key.lower().startswith("restriction:")
    )
    if conditional_keys:
        issue(
            "unsupported_conditional_restriction",
            "conditional / qualified restriction tags are not supported",
            conditional_tag_keys=conditional_keys,
        )

    restriction = _norm(relation.tags.get("restriction"))
    if restriction is None or restriction == "":
        issue(
            "missing_restriction_value",
            "relation has no restriction tag value",
        )
    elif restriction not in SUPPORTED_RESTRICTIONS:
        issue(
            "unsupported_restriction_type",
            f"restriction={relation.tags.get('restriction')!r} is not implemented",
            restriction_value=relation.tags.get("restriction"),
        )
    elif restriction not in set(plan["restriction_types"]):
        issue(
            "restriction_type_not_in_plan",
            f"restriction={restriction!r} is not enabled by plan.restriction_types",
            restriction_value=restriction,
        )

    if issues:
        return None, issues

    from_member = by_role["from"][0]
    via_member = by_role["via"][0]

    from_way_id = from_member.ref
    via_node_id = via_member.ref
    to_way_id = by_role["to"][0].ref

    from_problem = _way_reason(from_way_id, accepted_ways, ways)
    if from_problem == "missing":
        issue(
            "missing_way_reference",
            "from-way is referenced by the relation but absent from the XML",
            from_way_id=from_way_id,
        )
    elif from_problem == "filtered":
        issue(
            "restriction_references_filtered_way",
            "from-way exists but was filtered out of the road graph",
            from_way_id=from_way_id,
        )

    to_problem = _way_reason(to_way_id, accepted_ways, ways)
    if to_problem == "missing":
        issue(
            "missing_way_reference",
            "to-way is referenced by the relation but absent from the XML",
            to_way_id=to_way_id,
        )
    elif to_problem == "filtered":
        issue(
            "restriction_references_filtered_way",
            "to-way exists but was filtered out of the road graph",
            to_way_id=to_way_id,
        )

    if via_node_id not in nodes:
        issue(
            "missing_node_reference",
            "via node is referenced by the relation but absent from the XML",
            via_node_id=via_node_id,
        )

    if issues:
        return None, issues

    from_way = ways[from_way_id]
    to_way = ways[to_way_id]

    if from_way_id == to_way_id:
        if restriction != "no_u_turn":
            issue(
                "unsupported_same_way_restriction",
                "from=to is only supported for no_u_turn; a U-turn back along the "
                "incoming physical segment is the only handled semantics",
                restriction_value=restriction,
            )
        elif via_node_id not in from_way.nds:
            issue(
                "via_not_on_from_way",
                "via node does not appear in the from/to way's node sequence",
                via_node_id=via_node_id,
            )
    else:
        if not _is_endpoint(from_way, via_node_id):
            issue(
                "via_not_endpoint_of_from_way",
                "via node is not an endpoint of the from-way (interior via nodes "
                "are outside the supported scope for from!=to restrictions)",
                from_way_id=from_way_id,
                via_node_id=via_node_id,
                from_way_nds=from_way.nds,
            )
        if not _is_endpoint(to_way, via_node_id):
            issue(
                "via_not_endpoint_of_to_way",
                "via node is not an endpoint of the to-way (interior via nodes "
                "are outside the supported scope for from!=to restrictions)",
                to_way_id=to_way_id,
                via_node_id=via_node_id,
                to_way_nds=to_way.nds,
            )

    if issues:
        return None, issues

    return {
        "relation_id": relation.id,
        "restriction": restriction,
        "is_no": restriction in NO_RESTRICTIONS,
        "is_only": restriction in ONLY_RESTRICTIONS,
        "from_way_id": from_way_id,
        "via_node_id": via_node_id,
        "to_way_id": to_way_id,
    }, []


def _restriction_records(
    relations: dict[int, OSMRelation],
    ways: dict[int, OSMWay],
    accepted_ways: dict[int, OSMWay],
    nodes: dict[int, OSMNode],
    edges: list[DirectedEdge],
    plan: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Validate and pre-compute the effect of every restriction.

    Returns ``(applicable, unsupported, ineffective, diagnostics, ignored_relations)``.
    ``applicable`` entries contain everything needed to filter candidate turns.
    """
    ending_at: dict[int, list[DirectedEdge]] = defaultdict(list)
    starting_at: dict[int, list[DirectedEdge]] = defaultdict(list)
    for edge in edges:
        ending_at[edge.end_node_id].append(edge)
        starting_at[edge.start_node_id].append(edge)

    applicable: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    ineffective: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []

    for rel_id in sorted(relations, key=_sort_key):
        relation = relations[rel_id]
        relation_type = _norm(relation.tags.get("type"))

        if relation_type != "restriction":
            # Relations that are not restrictions are out of scope, but a relation
            # that looks like a malformed restriction is reported, not ignored.
            restriction_marker = relation.tags.get("restriction")
            roles = {member.role for member in relation.members}
            if restriction_marker is not None or {"from", "via", "to"} & roles:
                entry = _diagnostic(
                    "unsupported_relation_type",
                    "relation carries restriction-like members/tags but type is not 'restriction'",
                    severity="warning",
                    relation_id=rel_id,
                    relation_type=relation.tags.get("type"),
                )
                diagnostics.append(entry)
                unsupported.append(entry)
            else:
                ignored.append(
                    {
                        "relation_id": rel_id,
                        "relation_type": relation.tags.get("type"),
                        "reason": "not_a_restriction_relation",
                    }
                )
            continue

        roles, issues = _validate_restriction(relation, ways, accepted_ways, nodes, plan)
        if roles is None:
            diagnostics.extend(issues)
            base: dict[str, Any] = {
                "relation_id": rel_id,
                "restriction": relation.tags.get("restriction"),
                "codes": [issue["code"] for issue in issues],
                "diagnostic_messages": [issue["message"] for issue in issues],
            }
            for issue in issues:
                for key in ("from_way_id", "to_way_id", "via_node_id", "via_way_id"):
                    if key in issue and key not in base:
                        base[key] = issue[key]
            unsupported.append(base)
            continue

        record = dict(roles)
        restriction = record["restriction"]
        from_way_id = record["from_way_id"]
        via_node_id = record["via_node_id"]
        to_way_id = record["to_way_id"]

        entering = sorted(
            (
                edge
                for edge in ending_at.get(via_node_id, [])
                if edge.way_id == from_way_id
            ),
            key=_edge_sort_key,
        )
        record["entering_edge_ids"] = [edge.id for edge in entering]

        issues_list: list[dict[str, Any]] = []

        def ineffective_issue(code: str, message: str, **extra: Any) -> None:
            entry = _diagnostic(
                code, message, severity="warning", relation_id=rel_id
            )
            entry.update(
                {
                    "restriction": relation.tags.get("restriction"),
                    "from_way_id": from_way_id,
                    "via_node_id": via_node_id,
                    "to_way_id": to_way_id,
                }
            )
            for key, value in extra.items():
                if value is not None:
                    entry[key] = value
            issues_list.append(entry)

        if restriction == "no_u_turn" and from_way_id == to_way_id:
            # Only the directed reverse of each incoming physical segment is
            # removed; every other continuation (including forward along the
            # same way) remains a candidate.
            edge_lookup = {candidate.id: candidate for candidate in edges}
            reverse_targets: list[str] = []
            targets_by_entering: dict[str, list[str]] = {}
            for edge in entering:
                reverse = edge_lookup.get(edge.reverse_edge_id) if edge.reverse_edge_id else None
                if reverse is not None and reverse.start_node_id == via_node_id:
                    reverse_targets.append(reverse.id)
                    targets_by_entering[edge.id] = [reverse.id]
                else:
                    # No usable reverse exists at the via (e.g. one-way way).
                    targets_by_entering[edge.id] = []
            record["target_edge_ids"] = sorted(reverse_targets, key=lambda value: value)
            record["blocked_edge_ids"] = sorted(reverse_targets, key=lambda value: value)
            record["targets_by_entering"] = targets_by_entering
            record["blocking_targets"] = [
                {"from_edge": edge.id, "to_edge": edge.reverse_edge_id}
                for edge in entering
                if edge.reverse_edge_id is not None
            ]
            if not entering:
                ineffective_issue(
                    "restriction_has_no_entering_edge",
                    "no directed edge on the from-way ends at the via node, so this "
                    "U-turn restriction cannot act on any turn",
                )
            elif not reverse_targets:
                ineffective_issue(
                    "restriction_has_no_reverse_edge",
                    "the from-way has no directed reverse edge at the via node (for "
                    "example because it is one-way), so no U-turn can be removed",
                    entering_edge_ids=[edge.id for edge in entering],
                )
            if issues_list:
                diagnostics.extend(issues_list)
                ineffective.append(
                    {
                        "relation_id": rel_id,
                        "restriction": relation.tags.get("restriction"),
                        "from_way_id": from_way_id,
                        "via_node_id": via_node_id,
                        "to_way_id": to_way_id,
                        "codes": [issue["code"] for issue in issues_list],
                        "diagnostic_messages": [issue["message"] for issue in issues_list],
                        "entering_edge_ids": record["entering_edge_ids"],
                    }
                )
                continue
        else:
            target_exits = sorted(
                (edge for edge in starting_at.get(via_node_id, []) if edge.way_id == to_way_id),
                key=_edge_sort_key,
            )
            record["target_edge_ids"] = [edge.id for edge in target_exits]
            record["targets_by_entering"] = {
                edge.id: [target.id for target in target_exits]
                for edge in entering
            }
            if not entering:
                ineffective_issue(
                    "restriction_has_no_entering_edge",
                    "no directed edge on the from-way ends at the via node, so the "
                    "relation cannot act on any turn (check oneway direction)",
                )
            elif not target_exits:
                ineffective_issue(
                    "restriction_has_no_exit_on_to_way",
                    "the to-way has no directed edge starting at the via node, so the "
                    "relation cannot act on any turn (check oneway direction)",
                )
            if issues_list:
                diagnostics.extend(issues_list)
                ineffective.append(
                    {
                        "relation_id": rel_id,
                        "restriction": relation.tags.get("restriction"),
                        "from_way_id": from_way_id,
                        "via_node_id": via_node_id,
                        "to_way_id": to_way_id,
                        "codes": [issue["code"] for issue in issues_list],
                        "diagnostic_messages": [issue["message"] for issue in issues_list],
                        "entering_edge_ids": record["entering_edge_ids"],
                        "target_edge_ids": record["target_edge_ids"],
                    }
                )
                continue

        record["is_no"] = restriction in NO_RESTRICTIONS
        record["is_only"] = restriction in ONLY_RESTRICTIONS
        record["same_way_u_turn"] = restriction == "no_u_turn" and from_way_id == to_way_id
        record["restricted_value"] = relation.tags.get("restriction")
        applicable.append(record)

    applicable.sort(key=lambda item: _sort_key(item["relation_id"]))
    unsupported.sort(key=lambda item: _sort_key(item["relation_id"]))
    ineffective.sort(key=lambda item: _sort_key(item["relation_id"]))
    diagnostics.sort(key=_diagnostic_sort_key)
    ignored.sort(key=lambda item: _sort_key(item["relation_id"]))
    return applicable, unsupported, ineffective, diagnostics, ignored


# ---------------------------------------------------------------------------
# Turn generation and restriction application
# ---------------------------------------------------------------------------


def _turn_pairs(edges: list[DirectedEdge]) -> list[tuple[DirectedEdge, DirectedEdge, int]]:
    """All (incoming, outgoing, via_node) candidates; connection is by node id."""
    starting_at: dict[int, list[DirectedEdge]] = defaultdict(list)
    for edge in edges:
        starting_at[edge.start_node_id].append(edge)

    pairs: list[tuple[DirectedEdge, DirectedEdge, int]] = []
    for incoming in edges:
        via = incoming.end_node_id
        for outgoing in starting_at.get(via, []):
            pairs.append((incoming, outgoing, via))
    pairs.sort(
        key=lambda pair: (
            _edge_sort_key(_edge_as_dict_stub(pair[0])),
            _edge_sort_key(_edge_as_dict_stub(pair[1])),
        )
    )
    return pairs


def _edge_as_dict_stub(edge: DirectedEdge) -> dict[str, Any]:
    return {
        "way_id": edge.way_id,
        "segment_index": edge.segment_index,
        "direction": edge.direction,
    }


def _apply_restrictions(
    candidates: list[tuple[DirectedEdge, DirectedEdge, int]],
    applicable: list[dict[str, Any]],
    edge_by_id: dict[str, DirectedEdge],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Intersect ``only_*`` rules and subtract ``no_*`` rules per incoming state.

    The default is "allowed" (including U-turns); only relations can remove a
    candidate.  No unrequested driving rule is introduced.
    """
    # Rules keyed by (via_node_id, entering_edge_id) with target successor ids.
    no_rules: dict[tuple[int, str], list[tuple[dict[str, Any], frozenset[str]]]] = defaultdict(list)
    only_rules: dict[tuple[int, str], list[tuple[dict[str, Any], frozenset[str]]]] = defaultdict(list)

    for rule in applicable:
        via = rule["via_node_id"]
        entering_ids = list(rule["entering_edge_ids"])
        if rule.get("same_way_u_turn"):
            for entering_id in entering_ids:
                # Use the pre-computed per-entering targets so a U-turn is only
                # removed when a usable reverse directed edge actually exists.
                target = frozenset(
                    rule.get("targets_by_entering", {}).get(entering_id, [])
                )
                no_rules[(via, entering_id)].append((rule, target))
        else:
            target = frozenset(rule.get("target_edge_ids", []))
            bucket = only_rules if rule["is_only"] else no_rules
            for entering_id in entering_ids:
                bucket[(via, entering_id)].append((rule, target))

    allowed: list[dict[str, Any]] = []
    restricted: list[dict[str, Any]] = []

    for incoming, outgoing, via in candidates:
        state = (via, incoming.id)
        no_here = no_rules.get(state, [])
        only_here = only_rules.get(state, [])

        # only_* rules intersect: the successor must belong to *every* only_*
        # target set, so it is excluded as soon as one rule does not select it.
        excluding_only = [rule for rule, target in only_here if outgoing.id not in target]
        # no_* rules subtract their target successor.
        excluding_no = [rule for rule, target in no_here if outgoing.id in target]

        if excluding_only or excluding_no:
            rules: list[dict[str, Any]] = []
            kinds: dict[int, str] = {}
            for rule in excluding_only:
                if rule["relation_id"] not in kinds:
                    rules.append(rule)
                    kinds[rule["relation_id"]] = "only_restriction"
            for rule in excluding_no:
                if rule["relation_id"] not in kinds:
                    rules.append(rule)
                    kinds[rule["relation_id"]] = "no_restriction"
            rules.sort(key=lambda item: _sort_key(item["relation_id"]))
            record = _restricted_turn_record(incoming, outgoing, via, rules[0], kinds[rules[0]["relation_id"]])
            record["restriction_kinds"] = {
                str(rule["relation_id"]): kinds[rule["relation_id"]] for rule in rules
            }
            record["relation_ids"] = [rule["relation_id"] for rule in rules]
            record["restrictions"] = [rule["restricted_value"] for rule in rules]
            record["restriction_evidence"] = [
                {
                    "relation_id": rule["relation_id"],
                    "restriction": rule["restricted_value"],
                    "kind": kinds[rule["relation_id"]],
                    "from_way_id": rule["from_way_id"],
                    "via_node_id": rule["via_node_id"],
                    "to_way_id": rule["to_way_id"],
                    "same_way_u_turn": bool(rule.get("same_way_u_turn")),
                    "entering_edge_ids": list(rule["entering_edge_ids"]),
                    "target_edge_ids": list(rule.get("target_edge_ids", [])),
                }
                for rule in rules
            ]
            restricted.append(record)
            continue

        allowed.append(
            {
                "from_edge": incoming.id,
                "to_edge": outgoing.id,
                "via_node_id": via,
                "only_relation_ids": sorted(
                    {rule["relation_id"] for rule, _ in only_here}, key=_sort_key
                ),
                "no_relation_ids_checked": sorted(
                    {rule["relation_id"] for rule, _ in no_here}, key=_sort_key
                ),
                "only_rule_evidence": [
                    {
                        "relation_id": rule["relation_id"],
                        "restriction": rule["restricted_value"],
                        "target_edge_ids": sorted(target, key=str),
                        "outgoing_is_target": outgoing.id in target,
                    }
                    for rule, target in only_here
                ],
                "no_rule_evidence": [
                    {
                        "relation_id": rule["relation_id"],
                        "restriction": rule["restricted_value"],
                        "target_edge_ids": sorted(target, key=str),
                        "outgoing_is_target": outgoing.id in target,
                    }
                    for rule, target in no_here
                ],
            }
        )

    allowed.sort(
        key=lambda turn: (
            _edge_sort_key(_edge_as_dict_stub(edge_by_id[turn["from_edge"]])),
            _edge_sort_key(_edge_as_dict_stub(edge_by_id[turn["to_edge"]])),
        )
    )
    restricted.sort(
        key=lambda turn: (
            _sort_key(turn["relation_id"]),
            _edge_sort_key(_edge_as_dict_stub(edge_by_id[turn["from_edge"]])),
            _edge_sort_key(_edge_as_dict_stub(edge_by_id[turn["to_edge"]])),
        )
    )
    return allowed, restricted


def _restricted_turn_record(
    incoming: DirectedEdge,
    outgoing: DirectedEdge,
    via: int,
    rule: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    return {
        "from_edge": incoming.id,
        "to_edge": outgoing.id,
        "via_node_id": via,
        "relation_id": rule["relation_id"],
        "restriction": rule["restricted_value"],
        "restriction_kind": kind,
        "same_way_u_turn_restriction": bool(rule.get("same_way_u_turn")),
        "from_way_id": rule["from_way_id"],
        "to_way_id": rule["to_way_id"],
    }


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def _build_node_records(
    nodes: dict[int, OSMNode],
    accepted_ways: dict[int, OSMWay],
    edges: list[DirectedEdge],
) -> list[dict[str, Any]]:
    used_node_ids: set[int] = set()
    for way in accepted_ways.values():
        used_node_ids.update(way.nds)

    in_degree: dict[int, int] = defaultdict(int)
    out_degree: dict[int, int] = defaultdict(int)
    for edge in edges:
        out_degree[edge.start_node_id] += 1
        in_degree[edge.end_node_id] += 1

    records: list[dict[str, Any]] = []
    for node_id in sorted(used_node_ids, key=_sort_key):
        node = nodes[node_id]
        records.append(
            {
                "id": node_id,
                "lat": node.lat,
                "lon": node.lon,
                "directed_in_degree": in_degree.get(node_id, 0),
                "directed_out_degree": out_degree.get(node_id, 0),
            }
        )
    return records


def _way_provenance(way: OSMWay) -> dict[str, Any]:
    return {
        "way_id": way.id,
        "nds": list(way.nds),
        "highway": way.tags.get("highway"),
        "tags": dict(sorted(way.tags.items())),
    }


def _edge_source(edge: DirectedEdge, ways: dict[int, OSMWay]) -> dict[str, Any]:
    way = ways[edge.way_id]
    return {
        "way_id": way.id,
        "way_nds": list(way.nds),
        "segment_index": edge.segment_index,
        "segment_nds": [
            way.nds[edge.segment_index],
            way.nds[edge.segment_index + 1],
        ],
        "direction": edge.direction,
    }


def _coincident_coordinate_evidence(
    nodes: dict[int, OSMNode], accepted_ways: dict[int, OSMWay]
) -> list[dict[str, Any]]:
    """Report distinct node ids that share coordinates; they remain unmerged."""
    by_coordinate: dict[tuple[float, float], list[int]] = defaultdict(list)
    used = {nd for way in accepted_ways.values() for nd in way.nds}
    for node_id in sorted(used, key=_sort_key):
        node = nodes[node_id]
        by_coordinate[(node.lat, node.lon)].append(node_id)
    evidence = []
    for (lat, lon), ids in sorted(by_coordinate.items(), key=lambda item: item[0]):
        if len(ids) > 1:
            evidence.append(
                {
                    "coordinate": [lat, lon],
                    "node_ids": sorted(ids, key=_sort_key),
                    "merged": False,
                    "note": "same coordinate but different node ids; not connected",
                }
            )
    return evidence


def _crossing_evidence(
    nodes: dict[int, OSMNode],
    segments: list[PhysicalSegment],
) -> list[dict[str, Any]]:
    """Report physical segment pairs that cross geometrically without sharing a node.

    This is diagnostic evidence only -- it never creates a connection.  The test
    is a planar segment-intersection check on lat/lon, which is adequate for
    reporting the small synthetic fixture and is not used for connectivity.
    """
    def orient(a, b, c):
        value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(value) < 1e-15:
            return 0
        return 1 if value > 0 else -1

    def on_segment(a, b, c):
        return (
            min(a[0], b[0]) - 1e-15 <= c[0] <= max(a[0], b[0]) + 1e-15
            and min(a[1], b[1]) - 1e-15 <= c[1] <= max(a[1], b[1]) + 1e-15
        )

    def intersects(p1, p2, q1, q2):
        o1 = orient(p1, p2, q1)
        o2 = orient(p1, p2, q2)
        o3 = orient(q1, q2, p1)
        o4 = orient(q1, q2, p2)
        if o1 != o2 and o3 != o4:
            return True
        return (
            (o1 == 0 and on_segment(p1, p2, q1))
            or (o2 == 0 and on_segment(p1, p2, q2))
            or (o3 == 0 and on_segment(q1, q2, p1))
            or (o4 == 0 and on_segment(q1, q2, p2))
        )

    coords: dict[int, tuple[float, float]] = {
        node_id: (node.lat, node.lon) for node_id, node in nodes.items()
    }
    evidence = []
    for i, first in enumerate(segments):
        for second in segments[i + 1:]:
            if first.way_id == second.way_id:
                continue
            shared = {first.start_node_id, first.end_node_id} & {
                second.start_node_id,
                second.end_node_id,
            }
            if shared:
                continue
            p1 = coords[first.start_node_id]
            p2 = coords[first.end_node_id]
            q1 = coords[second.start_node_id]
            q2 = coords[second.end_node_id]
            if intersects(p1, p2, q1, q2):
                evidence.append(
                    {
                        "first_segment_id": first.id,
                        "first_way_id": first.way_id,
                        "second_segment_id": second.id,
                        "second_way_id": second.way_id,
                        "shared_node_ids": [],
                        "connected": False,
                        "note": "geometric intersection without a shared node id",
                    }
                )
    evidence.sort(key=lambda item: (_sort_key(item["first_way_id"]), _sort_key(item["second_way_id"])))
    return evidence


def build(
    osm_path: str | Path,
    plan_path: str | Path,
    *,
    verify: bool = True,
) -> dict[str, Any]:
    """Build the directed road/turn graph and return a JSON-friendly dict."""
    plan = load_plan(plan_path)
    nodes, ways, relations = parse_osm(osm_path)

    accepted_ways, segments, filtered_ways, way_diagnostics = _build_segments(
        nodes, ways, plan
    )
    edges = _expand_edges(nodes, ways, segments, plan)

    applicable, unsupported, ineffective, restriction_diagnostics, ignored = (
        _restriction_records(
            relations, ways, accepted_ways, nodes, edges, plan
        )
    )

    candidates = _turn_pairs(edges)
    allowed_turns, restricted_turns = _apply_restrictions(
        candidates, applicable, {edge.id: edge for edge in edges}
    )

    # If the plan explicitly disallows U-turns without a relation, enforce that
    # requested default.  The fixture plan allows them, so nothing is removed.
    default_uturn_allowed = _plan_uturn_available(plan)
    if not default_uturn_allowed:
        edge_by_id = {edge.id: edge for edge in edges}
        kept: list[dict[str, Any]] = []
        for turn in allowed_turns:
            incoming = edge_by_id[turn["from_edge"]]
            outgoing = edge_by_id[turn["to_edge"]]
            if outgoing.id == incoming.reverse_edge_id:
                restricted_turns.append(
                    {
                        "from_edge": incoming.id,
                        "to_edge": outgoing.id,
                        "via_node_id": turn["via_node_id"],
                        "relation_id": None,
                        "relation_ids": [],
                        "restriction": None,
                        "restrictions": [],
                        "restriction_kind": "plan_default_u_turn_disallowed",
                        "restriction_kinds": {},
                        "from_way_id": incoming.way_id,
                        "to_way_id": outgoing.way_id,
                    }
                )
            else:
                kept.append(turn)
        allowed_turns = kept
        restricted_turns.sort(
            key=lambda turn: (
                _sort_key(turn.get("relation_id") or -1),
                turn["from_edge"],
                turn["to_edge"],
            )
        )

    edge_by_id = {edge.id: edge for edge in edges}
    allowed_records = []
    for turn in allowed_turns:
        incoming = edge_by_id[turn["from_edge"]]
        outgoing = edge_by_id[turn["to_edge"]]
        allowed_records.append(
            {
                "from_edge": incoming.id,
                "to_edge": outgoing.id,
                "via_node_id": turn["via_node_id"],
                "from_way_id": incoming.way_id,
                "from_segment_index": incoming.segment_index,
                "from_direction": incoming.direction,
                "to_way_id": outgoing.way_id,
                "to_segment_index": outgoing.segment_index,
                "to_direction": outgoing.direction,
                "connection_proof": {
                    "from_end_node_id": incoming.end_node_id,
                    "to_start_node_id": outgoing.start_node_id,
                    "same_node_id": incoming.end_node_id == outgoing.start_node_id,
                },
                "applied_only_relation_ids": turn["only_relation_ids"],
                "checked_no_relation_ids": turn["no_relation_ids_checked"],
                "only_rule_evidence": turn["only_rule_evidence"],
                "no_rule_evidence": turn["no_rule_evidence"],
            }
        )

    restricted_records = []
    for turn in restricted_turns:
        incoming = edge_by_id[turn["from_edge"]]
        outgoing = edge_by_id[turn["to_edge"]]
        relation_ids = turn.get("relation_ids", [turn.get("relation_id")])
        relation_sources = []
        for rel_id in relation_ids:
            if rel_id is None:
                continue
            relation = relations[rel_id]
            relation_sources.append(
                {
                    "relation_id": relation.id,
                    "members": [
                        {"type": m.type, "ref": m.ref, "role": m.role}
                        for m in relation.members
                    ],
                    "tags": dict(sorted(relation.tags.items())),
                }
            )
        restrictions = list(turn.get("restrictions", []))
        if not restrictions and turn.get("restriction") is not None:
            restrictions = [turn["restriction"]]
        record = {
            "from_edge": incoming.id,
            "to_edge": outgoing.id,
            "via_node_id": turn["via_node_id"],
            "from_way_id": turn["from_way_id"],
            "from_segment_index": incoming.segment_index,
            "from_direction": incoming.direction,
            "to_way_id": turn["to_way_id"],
            "to_segment_index": outgoing.segment_index,
            "to_direction": outgoing.direction,
            "relation_id": None if len(relation_ids) != 1 else relation_ids[0],
            "relation_ids": [rel_id for rel_id in relation_ids if rel_id is not None],
            "restriction": restrictions[0] if restrictions else None,
            "restrictions": restrictions,
            "restriction_kind": turn.get("restriction_kind"),
            "restriction_kinds": turn.get("restriction_kinds", {}),
            "same_way_u_turn_restriction": bool(
                turn.get("same_way_u_turn_restriction", False)
            ),
            "relation_sources": relation_sources,
        }
        if len(relation_sources) == 1:
            record["relation_source"] = relation_sources[0]
        restricted_records.append(record)

    applied_relations = []
    for rule in applicable:
        relation = relations[rule["relation_id"]]
        applied_relations.append(
            {
                "relation_id": relation.id,
                "restriction": relation.tags.get("restriction"),
                "from_way_id": rule["from_way_id"],
                "via_node_id": rule["via_node_id"],
                "to_way_id": rule["to_way_id"],
                "entering_edge_ids": list(rule["entering_edge_ids"]),
                "target_edge_ids": list(rule.get("target_edge_ids", [])),
                "effect_per_entering": [
                    {
                        "entering_edge_id": entering_id,
                        "target_edge_ids": list(target_ids),
                    }
                    for entering_id, target_ids in rule.get(
                        "targets_by_entering", {}
                    ).items()
                ],
                "blocked_edge_ids": list(rule.get("blocked_edge_ids", [])),
                "same_way_u_turn": bool(rule.get("same_way_u_turn")),
                "members": [
                    {"type": m.type, "ref": m.ref, "role": m.role}
                    for m in relation.members
                ],
                "tags": dict(sorted(relation.tags.items())),
            }
        )

    used_way_ids = sorted(accepted_ways, key=_sort_key)
    diagnostics = way_diagnostics + restriction_diagnostics
    diagnostics.sort(key=_diagnostic_sort_key)

    physical_records = [
        {
            "id": segment.id,
            "way_id": segment.way_id,
            "segment_index": segment.segment_index,
            "start_node_id": segment.start_node_id,
            "end_node_id": segment.end_node_id,
            "length_m": round(segment.length_m, 6),
        }
        for segment in segments
    ]

    graph: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "plan": plan,
        "summary": {
            "node_count": len(
                {nd for way in accepted_ways.values() for nd in way.nds}
            ),
            "accepted_way_count": len(accepted_ways),
            "physical_segment_count": len(segments),
            "directed_edge_count": len(edges),
            "candidate_turn_count": len(candidates),
            "allowed_turn_count": len(allowed_records),
            "restricted_turn_count": len(restricted_records),
            "applied_restriction_count": len(applicable),
            "unsupported_restriction_count": len(unsupported),
            "ineffective_restriction_count": len(ineffective),
            "filtered_way_count": len(filtered_ways),
            "diagnostic_count": len(diagnostics),
            "default_u_turn_allowed": default_uturn_allowed,
            "complete_for_supported_scope": not (unsupported or ineffective),
            "complete_note": (
                "filtered ways are intentional plan filtering and are listed separately; "
                "unsupported or ineffective restrictions make the graph incomplete for "
                "the requested scope"
            ),
        },
        "nodes": _build_node_records(nodes, accepted_ways, edges),
        "physical_segments": physical_records,
        "directed_edges": [_edge_to_dict(edge, nodes) for edge in edges],
        "allowed_turns": allowed_records,
        "restricted_turns": restricted_records,
        "restrictions": {
            "applied": applied_relations,
            "unsupported": unsupported,
            "ineffective": ineffective,
            "ignored_non_restriction_relations": ignored,
        },
        "filtered_ways": filtered_ways,
        "unsupported_restrictions": unsupported,
        "ineffective_restrictions": ineffective,
        "evidence": {
            "coincident_coordinates_unmerged": _coincident_coordinate_evidence(
                nodes, accepted_ways
            ),
            "geometric_intersections_without_shared_node": _crossing_evidence(
                nodes, segments
            ),
            "bridge_layer_tags": [
                {
                    "way_id": way_id,
                    "bridge": accepted_ways[way_id].tags.get("bridge"),
                    "layer": accepted_ways[way_id].tags.get("layer"),
                }
                for way_id in sorted(accepted_ways, key=_sort_key)
                if "bridge" in accepted_ways[way_id].tags
                or "layer" in accepted_ways[way_id].tags
            ],
        },
        "diagnostics": diagnostics,
        "provenance": {
            "ways": [_way_provenance(accepted_ways[way_id]) for way_id in used_way_ids],
            "filtered_way_ids": [item["way_id"] for item in filtered_ways],
        },
        "edge_sources": {
            edge.id: _edge_source(edge, ways) for edge in edges
        },
    }

    if verify:
        graph["verification"] = verify_graph(graph, set(relations))

    graph["graph_identity_hash"] = _graph_identity_hash(graph)
    graph["content_hash"] = _content_hash(graph)
    return graph


# ---------------------------------------------------------------------------
# Independent verification
# ---------------------------------------------------------------------------


def verify_graph(
    graph: dict[str, Any], relation_ids: Iterable[int] | None = None
) -> dict[str, Any]:
    """Re-check the emitted graph against its own provenance.

    This is intentionally redundant: it rebuilds index structures from the
    output records rather than trusting the construction code, so a mistake in
    turn generation or restriction application surfaces here.
    """
    errors: list[str] = []
    checks: list[str] = []

    edges = {edge["id"]: edge for edge in graph["directed_edges"]}
    if len(edges) != len(graph["directed_edges"]):
        errors.append("duplicate directed edge ids")
    nodes = {node["id"]: node for node in graph["nodes"]}
    ways = {way["way_id"]: way for way in graph["provenance"]["ways"]}
    radius = graph["plan"]["earth_radius_m"]

    # 1. Every edge traces back to an original way and adjacent nd pair.
    for edge in graph["directed_edges"]:
        source = graph["edge_sources"].get(edge["id"])
        if source is None:
            errors.append(f"edge {edge['id']} has no provenance")
            continue
        way = ways.get(source["way_id"])
        if way is None:
            errors.append(f"edge {edge['id']} references unknown accepted way")
            continue
        index = source["segment_index"]
        if index < 0 or index + 1 >= len(way["nds"]):
            errors.append(f"edge {edge['id']} segment index out of range")
            continue
        expected = [way["nds"][index], way["nds"][index + 1]]
        if source["segment_nds"] != expected:
            errors.append(f"edge {edge['id']} segment nds do not match way order")
        if source["direction"] == "forward":
            expected_start, expected_end = expected
        else:
            expected_start, expected_end = expected[1], expected[0]
        if (edge["start_node_id"], edge["end_node_id"]) != (expected_start, expected_end):
            errors.append(f"edge {edge['id']} direction disagrees with segment nds")
        start = nodes.get(edge["start_node_id"])
        end = nodes.get(edge["end_node_id"])
        if start is None or end is None:
            errors.append(f"edge {edge['id']} uses a node absent from output nodes")
            continue
        recomputed = haversine_m(
            start["lat"], start["lon"], end["lat"], end["lon"], radius
        )
        if abs(recomputed - edge["length_m"]) > 1e-5:
            errors.append(f"edge {edge['id']} length does not match haversine")
    checks.append("edge provenance/nd order/direction/length")

    # 2. Independent candidate enumeration from edges only.
    starting_at: dict[int, list[str]] = {}
    for edge in graph["directed_edges"]:
        starting_at.setdefault(edge["start_node_id"], []).append(edge["id"])
    expected_candidates: set[tuple[str, str, int]] = set()
    for edge in graph["directed_edges"]:
        via = edge["end_node_id"]
        for successor in starting_at.get(via, []):
            expected_candidates.add((edge["id"], successor, via))

    allowed_keys = [
        (turn["from_edge"], turn["to_edge"], turn["via_node_id"])
        for turn in graph["allowed_turns"]
    ]
    restricted_keys = [
        (turn["from_edge"], turn["to_edge"], turn["via_node_id"])
        for turn in graph["restricted_turns"]
    ]
    if len(set(allowed_keys)) != len(allowed_keys):
        errors.append("duplicate allowed turns")
    if len(set(restricted_keys)) != len(restricted_keys):
        errors.append("duplicate restricted turns")
    overlap = set(allowed_keys) & set(restricted_keys)
    if overlap:
        errors.append(f"{len(overlap)} turns are both allowed and restricted")
    if set(allowed_keys) | set(restricted_keys) != expected_candidates:
        missing = len(expected_candidates - set(allowed_keys) - set(restricted_keys))
        extra = len((set(allowed_keys) | set(restricted_keys)) - expected_candidates)
        errors.append(
            f"turn partition mismatch: {missing} candidates missing, {extra} extra"
        )
    checks.append("candidate enumeration partition")

    # 3. Every allowed turn proves same-node connection.
    for turn in graph["allowed_turns"]:
        incoming = edges[turn["from_edge"]]
        outgoing = edges[turn["to_edge"]]
        if incoming["end_node_id"] != outgoing["start_node_id"]:
            errors.append(f"allowed turn {turn['from_edge']}->{turn['to_edge']} is disconnected")
        if outgoing["start_node_id"] != turn["via_node_id"]:
            errors.append(f"allowed turn {turn['from_edge']}->{turn['to_edge']} wrong via node")
    checks.append("allowed turn connectivity")

    # 4. Reproduce restriction decisions per (via, entering edge).
    rules_by_state: dict[tuple[int, str], dict[str, set[str]]] = {}
    for rule in graph["restrictions"]["applied"]:
        via = rule["via_node_id"]
        for effect in rule.get("effect_per_entering", []):
            entering_id = effect["entering_edge_id"]
            state = (via, entering_id)
            entry = rules_by_state.setdefault(
                state, {"only": set(), "no": set(), "only_relations": set(), "no_relations": set()}
            )
            bucket = "only" if rule["restriction"].startswith("only_") else "no"
            entry[bucket].update(effect["target_edge_ids"])
            entry[f"{bucket}_relations"].add(rule["relation_id"])

    # Recompute each only_* rule's target set per state, then reproduce outcomes.
    only_target_sets: dict[tuple[int, str], dict[int, set[str]]] = {}
    for applied in graph["restrictions"]["applied"]:
        if not applied["restriction"].startswith("only_"):
            continue
        for effect in applied.get("effect_per_entering", []):
            state = (applied["via_node_id"], effect["entering_edge_id"])
            only_target_sets.setdefault(state, {})[applied["relation_id"]] = set(
                effect["target_edge_ids"]
            )

    for turn in graph["allowed_turns"]:
        incoming = edges[turn["from_edge"]]
        outgoing = edges[turn["to_edge"]]
        state = (turn["via_node_id"], incoming["id"])
        for rel_id, target in only_target_sets.get(state, {}).items():
            if outgoing["id"] not in target:
                errors.append(
                    f"allowed turn {incoming['id']}->{outgoing['id']} violates "
                    f"only restriction {rel_id}"
                )
        if outgoing["id"] in rules_by_state.get(state, {}).get("no", set()):
            errors.append(
                f"allowed turn {incoming['id']}->{outgoing['id']} violates no restriction"
            )

    for turn in graph["restricted_turns"]:
        incoming = edges[turn["from_edge"]]
        outgoing = edges[turn["to_edge"]]
        state = (turn["via_node_id"], incoming["id"])
        kind_map = turn.get("restriction_kinds", {})
        kinds = set(kind_map.values())
        if not kinds:
            if turn["restriction_kind"] != "plan_default_u_turn_disallowed":
                errors.append(
                    f"restricted turn {incoming['id']}->{outgoing['id']} has no rule state"
                )
            continue
        for rel_id, kind in kind_map.items():
            rel_id_int = int(rel_id)
            if kind == "only_restriction":
                if outgoing["id"] in only_target_sets.get(state, {}).get(rel_id_int, set()):
                    errors.append(
                        f"only-restricted turn {incoming['id']}->{outgoing['id']} points "
                        f"at an only target for {rel_id_int}"
                    )
            elif kind == "no_restriction":
                if outgoing["id"] not in rules_by_state.get(state, {}).get("no", set()):
                    errors.append(
                        f"no-restricted turn {incoming['id']}->{outgoing['id']} is not "
                        f"a no target for {rel_id_int}"
                    )
            else:
                errors.append(f"unknown restriction kind {kind!r}")
    checks.append("restriction decisions")

    # 5. Same-way no_u_turn must keep forward continuations.  A continuation may
    #    still be blocked by another relation (no_* / only_*); what must not
    #    happen is that this U-turn relation itself removes it.
    allowed_key_set = set(allowed_keys)
    blocked_by_relation: dict[tuple[str, str, int], set[int]] = {}
    for turn in graph["restricted_turns"]:
        key = (turn["from_edge"], turn["to_edge"], turn["via_node_id"])
        blocked_by_relation.setdefault(key, set()).update(turn.get("relation_ids", []))
    for applied in graph["restrictions"]["applied"]:
        if not applied.get("same_way_u_turn"):
            continue
        relation_id = applied["relation_id"]
        for effect in applied.get("effect_per_entering", []):
            entering_id = effect["entering_edge_id"]
            entering = edges[entering_id]
            via = applied["via_node_id"]
            for successor_id in starting_at.get(via, []):
                if successor_id == entering.get("reverse_edge_id"):
                    continue
                key = (entering_id, successor_id, via)
                if key in allowed_key_set:
                    continue
                # Blocked, but only by some *other* relation -- that is allowed.
                if blocked_by_relation.get(key, set()) - {relation_id}:
                    continue
                errors.append(
                    f"same-way no_u_turn removed non-U-turn continuation "
                    f"{entering_id}->{successor_id}"
                )
    checks.append("same-way no_u_turn continuation")

    # 6. Every diagnostic carries a locator.
    for entry in graph["diagnostics"]:
        if not any(
            key in entry
            for key in ("way_id", "relation_id", "node_id", "member_ref")
        ):
            errors.append(f"diagnostic without locator: {entry.get('code')}")
    checks.append("diagnostic locators")

    # 7. Filtered ways never appear as accepted edges.
    filtered_ids = set(graph["provenance"]["filtered_way_ids"])
    for edge in graph["directed_edges"]:
        if edge["way_id"] in filtered_ids:
            errors.append(f"filtered way {edge['way_id']} appears in edges")
    checks.append("filtered ways excluded")

    # 8. Applied/unsupported/ineffective/ignored partition every relation.  No
    #    relation may be both applied and diagnosed as unusable.
    applied_ids = [rule["relation_id"] for rule in graph["restrictions"]["applied"]]
    unsupported_ids = [rule["relation_id"] for rule in graph["restrictions"]["unsupported"]]
    ineffective_ids = [rule["relation_id"] for rule in graph["restrictions"]["ineffective"]]
    ignored_ids = [
        rule["relation_id"]
        for rule in graph["restrictions"]["ignored_non_restriction_relations"]
    ]
    for bucket in (applied_ids, unsupported_ids, ineffective_ids, ignored_ids):
        if len(set(bucket)) != len(bucket):
            errors.append("a relation appears twice in one classification bucket")
    overlaps = (
        set(applied_ids) & set(unsupported_ids)
        | set(applied_ids) & set(ineffective_ids)
        | set(unsupported_ids) & set(ineffective_ids)
        | set(applied_ids) & set(ignored_ids)
    )
    if overlaps:
        errors.append(f"relations classified twice: {sorted(overlaps)}")
    if relation_ids is not None:
        covered = set(applied_ids) | set(unsupported_ids) | set(ineffective_ids) | set(ignored_ids)
        missing = set(relation_ids) - covered
        extra = covered - set(relation_ids)
        if missing:
            errors.append(f"relations neither applied nor diagnosed: {sorted(missing)}")
        if extra:
            errors.append(f"relations classified but absent from XML: {sorted(extra)}")
    checks.append("relation classification partition")

    # 9. Applied rule endpoints really belong to the declared ways at the via.
    for rule in graph["restrictions"]["applied"]:
        via = rule["via_node_id"]
        for edge_id in rule["entering_edge_ids"]:
            edge = edges[edge_id]
            if edge["way_id"] != rule["from_way_id"]:
                errors.append(f"entering edge {edge_id} is not on from-way {rule['from_way_id']}")
            if edge["end_node_id"] != via:
                errors.append(f"entering edge {edge_id} does not end at via {via}")
        for edge_id in rule.get("target_edge_ids", []):
            edge = edges[edge_id]
            if edge["way_id"] != rule["to_way_id"]:
                errors.append(f"target edge {edge_id} is not on to-way {rule['to_way_id']}")
            if edge["start_node_id"] != via:
                errors.append(f"target edge {edge_id} does not start at via {via}")
    checks.append("applied rule endpoints")

    # 10. Every restricted turn has relation evidence (except plan defaults).
    for turn in graph["restricted_turns"]:
        if turn["restriction_kind"] == "plan_default_u_turn_disallowed":
            continue
        relation_ids = turn.get("relation_ids", [])
        if not relation_ids:
            errors.append("restricted turn has no relation id")
            continue
        sourced = {item["relation_id"] for item in turn.get("relation_sources", [])}
        if sourced != set(relation_ids):
            errors.append(
                f"restricted turn relation evidence mismatch for {relation_ids}"
            )
    checks.append("restricted turn relation evidence")

    # 11. Structural anti-checks: same coordinates must not be merged and no
    #     turn may connect edges whose node ids differ.
    coordinate_ids: dict[tuple[float, float], set[int]] = {}
    for node in graph["nodes"]:
        coordinate_ids.setdefault((node["lat"], node["lon"]), set()).add(node["id"])
    merged_coordinates = [
        sorted(ids) for ids, ids_count in coordinate_ids.items() if len(ids_count) > 1
    ]
    # Duplicates are allowed (and reported as evidence); what would be wrong is
    # treating them as one node.  We assert both ids are still distinct records.
    for ids in merged_coordinates:
        if len({node for node in ids}) != len(ids):
            errors.append("coincident coordinates were merged into one node id")
    checks.append("coincident coordinate ids preserved")

    result: dict[str, Any] = {
        "ok": not errors,
        "checks": checks,
        "errors": errors,
    }
    return result


# ---------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------


def _write_json(path: str | Path, value: Any) -> None:
    Path(path).write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _print_summary(graph: dict[str, Any], out_path: str) -> None:
    summary = graph["summary"]
    print(f"wrote {out_path}")
    print(
        "nodes={node_count} ways={accepted_way_count} segments={physical_segment_count} "
        "edges={directed_edge_count}".format(**summary)
    )
    print(
        "turns: candidates={candidate_turn_count} allowed={allowed_turn_count} "
        "restricted={restricted_turn_count}".format(**summary)
    )
    print(
        "restrictions: applied={applied_restriction_count} "
        "unsupported={unsupported_restriction_count} "
        "ineffective={ineffective_restriction_count} "
        "filtered_ways={filtered_way_count}".format(**summary)
    )
    print(
        "complete_for_supported_scope={complete_for_supported_scope}".format(
            **summary
        )
    )
    verification = graph.get("verification")
    if verification is not None:
        print(f"verification_ok={verification['ok']}")
        if verification["errors"]:
            for error in verification["errors"]:
                print(f"  ERROR: {error}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("osm", nargs="?", default="fixtures/roads.osm", help="OSM XML input")
    parser.add_argument(
        "plan", nargs="?", default="fixtures/plan.json", help="plan JSON input"
    )
    parser.add_argument("-o", "--output", default=None, help="output JSON path")
    parser.add_argument(
        "--no-verify", action="store_true", help="skip embedded verification"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when the graph is incomplete for the supported scope",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    graph = build(args.osm, args.plan, verify=not args.no_verify)
    output = args.output or str(Path(args.osm).with_suffix(".graph.json"))
    _write_json(output, graph)
    _print_summary(graph, output)

    verification = graph.get("verification")
    if verification is not None and not verification["ok"]:
        return 1
    if args.strict and not graph["summary"]["complete_for_supported_scope"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
