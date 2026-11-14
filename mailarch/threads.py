"""Conversation threading from Message-ID / References / In-Reply-To evidence.

Rules (fixtures/rules.json):

- parent candidate: last valid References id, otherwise a single
  In-Reply-To id; the complete ordered References chain is retained as
  evidence on the node.
- conflicting In-Reply-To vs References: both headers and the conflict
  are retained, never silently discarded.
- same Message-ID: collapse only byte-identical sources; differing
  versions remain conflict variants and inbound references to that id
  become ambiguous instead of picking one representative.
- missing parents get placeholder nodes; reference cycles and
  self-references are shown explicitly (no edge is dropped to pretend
  the evidence is complete); subject never establishes a parent.

Attached message/rfc822 messages are not threaded here at all.
"""
from __future__ import annotations

import hashlib
import json


def _sort_key(node):
    return (node.get("date_iso") is None, node.get("date_iso") or "", node["node_id"])


def build_threads(nodes: list[dict]) -> dict:
    by_msgid: dict[str, list[str]] = {}
    for n in nodes:
        by_msgid.setdefault(n["message_id"], []).append(n["node_id"])
    for ids in by_msgid.values():
        ids.sort()

    diagnostics: list[dict] = []
    edges: list[dict] = []
    parent_of: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    ghost_id_by_msgid: dict[str, str] = {}

    for mid in sorted(by_msgid):
        if len(by_msgid[mid]) > 1:
            diagnostics.append({"type": "id-conflict", "message_id": mid,
                                "nodes": by_msgid[mid]})

    for n in sorted(nodes, key=lambda x: x["node_id"]):
        nid = n["node_id"]
        if n.get("message_id_synthesized"):
            diagnostics.append({"type": "missing-message-id", "node": nid,
                                "synthesized": n["message_id"]})
        refs = n.get("references") or []
        irt = n.get("in_reply_to") or []
        chosen = None
        evidence = None
        if refs:
            chosen = refs[-1]
            evidence = "references-last"
            if irt and irt[-1] != chosen:
                diagnostics.append({"type": "irt-conflict", "node": nid,
                                    "references_last": chosen,
                                    "in_reply_to": list(irt)})
        elif len(irt) == 1:
            chosen = irt[0]
            evidence = "in-reply-to"
        elif len(irt) > 1:
            diagnostics.append({"type": "multiple-in-reply-to", "node": nid,
                                "in_reply_to": list(irt)})
        if chosen is None:
            continue
        cands = by_msgid.get(chosen, [])
        edge = {"child": nid, "parent_msgid": chosen, "evidence": evidence,
                "parent": None, "ambiguous": []}
        if cands == [nid]:
            edge["parent"] = nid
            parent_of[nid] = nid
            diagnostics.append({"type": "self-reference", "node": nid})
        elif not cands:
            gid = ghost_id_by_msgid.get(chosen)
            if gid is None:
                gid = "g_" + hashlib.sha256(chosen.encode("utf-8")).hexdigest()[:16]
                ghost_id_by_msgid[chosen] = gid
            edge["parent"] = gid
            parent_of[nid] = gid
            diagnostics.append({"type": "missing-parent", "node": nid,
                                "parent_msgid": chosen, "placeholder": gid})
        elif len(cands) == 1:
            edge["parent"] = cands[0]
            parent_of[nid] = cands[0]
        else:
            edge["ambiguous"] = list(cands)
            ambiguous[nid] = list(cands)
            diagnostics.append({"type": "ambiguous-parent", "node": nid,
                                "parent_msgid": chosen, "candidates": list(cands)})
        edges.append(edge)

    # cycle detection on resolved parent edges (self-loops already
    # reported as self-reference; edges are kept, not dropped)
    state: dict[str, int] = {}
    cycle_members: set[str] = set()
    for start in sorted(parent_of):
        if state.get(start):
            continue
        stack = []
        cur = start
        while cur is not None and not state.get(cur):
            state[cur] = 1
            stack.append(cur)
            cur = parent_of.get(cur)
        if cur is not None and state.get(cur) == 1:
            cyc = stack[stack.index(cur):]
            cycle_members.update(cyc)
            if len(cyc) > 1:
                diagnostics.append({"type": "reference-cycle",
                                    "members": sorted(cyc)})
        for x in stack:
            state[x] = 2

    # connected components over resolved edges -> conversations
    uf = {n["node_id"]: n["node_id"] for n in nodes}
    for gid in ghost_id_by_msgid.values():
        uf[gid] = gid

    def find(x):
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            if rb < ra:
                ra, rb = rb, ra
            uf[rb] = ra

    for child, par in parent_of.items():
        union(child, par)

    comps: dict[str, list[str]] = {}
    for x in uf:
        comps.setdefault(find(x), []).append(x)

    ghosts: dict[str, dict] = {}
    for mid, gid in ghost_id_by_msgid.items():
        ghosts[gid] = {"node_id": gid, "message_id": mid, "placeholder": True,
                       "subject": None, "from": None, "date_iso": None}
    all_nodes = {n["node_id"]: n for n in nodes}
    all_nodes.update(ghosts)

    children_map: dict[str, list[str]] = {}
    for child, par in parent_of.items():
        children_map.setdefault(par, []).append(child)
    for par in children_map:
        children_map[par].sort(key=lambda c: _sort_key(all_nodes[c]))

    for n in nodes:
        nid = n["node_id"]
        n["parent"] = parent_of.get(nid)
        n["children"] = children_map.get(nid, [])
        n["ambiguous_parents"] = ambiguous.get(nid, [])
        n["in_cycle"] = nid in cycle_members
        n["id_conflicts"] = [x for x in by_msgid.get(n["message_id"], []) if x != nid]

    conversations = []
    for members in comps.values():
        members.sort()
        conv_id = "c_" + hashlib.sha256(
            "\n".join(members).encode("utf-8")).hexdigest()[:12]
        roots = sorted((m for m in members if m not in parent_of),
                       key=lambda m: _sort_key(all_nodes[m]))
        real_roots = [m for m in roots if m not in ghosts]
        if real_roots:
            entry = real_roots[0]
        elif roots:
            entry = roots[0]
        else:  # pure cycle: deterministic entry point, edges untouched
            entry = min(members, key=lambda m: _sort_key(all_nodes[m]))
        flags = set()
        if cycle_members & set(members):
            flags.add("cycle")
        if any(parent_of.get(m) == m for m in members):
            flags.add("self-reference")
        if any(m in ghosts for m in members):
            flags.add("missing-parent")
        if any(all_nodes[m].get("id_conflicts") for m in members if m not in ghosts):
            flags.add("id-conflict")
        if any(m in ambiguous for m in members):
            flags.add("ambiguous-parent")
        if any(all_nodes[m].get("message_id_synthesized")
               for m in members if m not in ghosts):
            flags.add("missing-message-id")
        subj = all_nodes[entry].get("subject")
        if not subj:  # placeholder entry: fall back to a real member's subject
            for m in sorted(members, key=lambda m: _sort_key(all_nodes[m])):
                if m not in ghosts and all_nodes[m].get("subject"):
                    subj = all_nodes[m]["subject"]
                    break
        conversations.append({
            "conv_id": conv_id,
            "subject": subj or "(no subject)",
            "entry": entry,
            "roots": roots,
            "members": members,
            "flags": sorted(flags),
            "message_count": len([m for m in members if m not in ghosts]),
        })
        for m in members:
            all_nodes[m]["conversation"] = conv_id

    conversations.sort(key=lambda c: c["conv_id"])
    diagnostics.sort(key=lambda d: (d["type"],
                                    json.dumps(d, ensure_ascii=False, sort_keys=True)))
    edges.sort(key=lambda e: (e["child"], e["parent_msgid"]))

    return {
        "conversations": conversations,
        "edges": edges,
        "diagnostics": diagnostics,
        "placeholders": [ghosts[g] for g in sorted(ghosts)],
        "children_map": children_map,
    }
