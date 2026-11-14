"""Independent window-frame derivation.

The engine never reads aggregate values from PostgreSQL.  It partitions and
sorts the raw rows itself, derives frame head/tail positions from the declared
window definition, and evaluates each supported function from the derived
frame members.  PostgreSQL is used only by ``verify`` to cross-check results
that this explanation declares deterministic.

Uncertainty is modelled explicitly and never papered over:

* **ROWS frames** are positional.  When the declared ordering leaves a peer
  group of size > 1, the result row's own slot inside that group is not
  determined by the SQL.  All possible slots are examined (slots, not
  permutations).  If different slots produce different frames, the row is
  reported nondeterministic.  The SQL is never rewritten and no synthetic
  ordering key is appended.
* A positional boundary can cut a peer group, so any row of that group may
  fill the boundary positions.  Aggregate extremes are then computed over all
  admissible subsets and reported as ranges plus a NULL-possibility flag.
* **GROUPS and RANGE** boundaries align with peer-group edges (RANGE offset
  comparisons include whole equal-value groups, and NULL comparisons only
  distinguish NULL from non-NULL), so their membership is determined by the
  partition and ordering alone.  Value functions still need a *within-group*
  position; if a boundary peer group has differing argument values the result
  is reported nondeterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key
from typing import Any, Optional

from .sqlparse import (
    FrameBound,
    FrameSpec,
    OrderKey,
    Query,
    WindowDefinition,
)

INT4_MIN = -(2**31)
INT4_MAX = 2**31 - 1


# --------------------------------------------------------------------------
# Rows, partitions, peer groups
# --------------------------------------------------------------------------


@dataclass
class Row:
    event_id: int
    values: dict[str, Any]
    input_index: int = 0


@dataclass
class PeerGroup:
    index: int
    start: int
    end: int
    rows: list[Row]
    order_values: dict[str, Any]

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def event_ids(self) -> list[int]:
        return [row.event_id for row in self.rows]


@dataclass
class Partition:
    index: int
    key_values: dict[str, Any]
    rows: list[Row]
    groups: list[PeerGroup]
    position_of: dict[int, int]
    group_of: dict[int, int]

    @classmethod
    def build(cls, index: int, key_values: dict, rows: list[Row], groups: list[PeerGroup]):
        part = cls(index, key_values, rows, groups, {}, {})
        part.position_of = {row.event_id: pos for pos, row in enumerate(rows)}
        for group in groups:
            for pos in range(group.start, group.end):
                part.group_of[rows[pos].event_id] = group.index
        return part


def _compare_values(a: Any, b: Any, key: OrderKey) -> int:
    if a is None and b is None:
        return 0
    if a is None:
        return -1 if key.nulls_first else 1
    if b is None:
        return 1 if key.nulls_first else -1
    if a < b:
        result = -1
    elif a > b:
        result = 1
    else:
        result = 0
    return -result if key.descending else result


def _row_comparator(order_by: list[OrderKey]):
    def compare(left: Row, right: Row) -> int:
        for key in order_by:
            result = _compare_values(
                left.values.get(key.column), right.values.get(key.column), key
            )
            if result:
                return result
        # Canonical tie-break for reproducible evidence only.  It is *not* part
        # of the SQL ordering and never establishes determinism.
        return (left.event_id > right.event_id) - (left.event_id < right.event_id)

    return compare


def _peer_key(row: Row, order_by: list[OrderKey]) -> tuple:
    # Peers compare equal on every ORDER BY expression; NULL groups with NULL.
    return tuple(row.values.get(key.column) for key in order_by)


def build_partitions(window: WindowDefinition, rows: list[Row]) -> list[Partition]:
    buckets: dict[tuple, list[Row]] = {}
    order: list[tuple] = []
    for row in rows:
        bucket_key = tuple(row.values.get(col) for col in window.partition_by)
        if bucket_key not in buckets:
            buckets[bucket_key] = []
            order.append(bucket_key)
        buckets[bucket_key].append(row)

    comparator = _row_comparator(window.order_by)
    partitions: list[Partition] = []
    for bucket_key in order:
        bucket = sorted(buckets[bucket_key], key=cmp_to_key(comparator))
        key_values = {col: bucket[0].values.get(col) for col in window.partition_by}
        groups: list[PeerGroup] = []
        if window.order_by:
            start = 0
            index = 0
            while start < len(bucket):
                end = start + 1
                peer_key = _peer_key(bucket[start], window.order_by)
                while end < len(bucket) and _peer_key(bucket[end], window.order_by) == peer_key:
                    end += 1
                groups.append(
                    PeerGroup(
                        index=index,
                        start=start,
                        end=end,
                        rows=bucket[start:end],
                        order_values={
                            key.column: bucket[start].values.get(key.column)
                            for key in window.order_by
                        },
                    )
                )
                start = end
                index += 1
        else:
            # PostgreSQL: without ORDER BY every row in the partition is a peer.
            groups.append(PeerGroup(0, 0, len(bucket), bucket, {}))
        partitions.append(Partition.build(len(partitions), key_values, bucket, groups))

    partitions.sort(key=lambda part: min((row.event_id for row in part.rows), default=0))
    for new_index, partition in enumerate(partitions):
        partition.index = new_index
    return partitions


# --------------------------------------------------------------------------
# Frame resolution and bounds
# --------------------------------------------------------------------------


def resolved_frame(window: WindowDefinition) -> FrameSpec:
    if window.frame is not None:
        return window.frame
    if window.order_by:
        return FrameSpec(
            mode="RANGE",
            start=FrameBound(kind="UNBOUNDED", direction="PRECEDING"),
            end=FrameBound(kind="CURRENT_ROW"),
            explicit=False,
        )
    return FrameSpec(
        mode="RANGE",
        start=FrameBound(kind="UNBOUNDED", direction="PRECEDING"),
        end=FrameBound(kind="UNBOUNDED", direction="FOLLOWING"),
        explicit=False,
    )


@dataclass
class FrameBounds:
    head: int
    tail: int
    head_reason: str
    tail_reason: str
    mode: str
    start: FrameBound
    end: FrameBound

    @property
    def empty(self) -> bool:
        return self.tail <= self.head

    @property
    def size(self) -> int:
        return max(0, self.tail - self.head)


def _in_range_int4(value: int, base: int, offset: int, sub: bool, less: bool) -> bool:
    """Mirror PostgreSQL's ``in_range_int4_int4`` for an int4 ordering key."""
    signed = -offset if sub else offset
    total = base + signed
    if total < INT4_MIN or total > INT4_MAX:
        # Overflow: true sum is surely beyond val, matching PostgreSQL.
        return (not less) if sub else less
    return value <= total if less else value >= total


def _rows_bounds(pos: int, n: int, frame: FrameSpec) -> tuple[int, int, str, str]:
    def start_pos(bound: FrameBound) -> tuple[int, str]:
        if bound.kind == "UNBOUNDED":
            return 0, "UNBOUNDED PRECEDING -> partition start (position 0)"
        if bound.kind == "CURRENT_ROW":
            return pos, f"CURRENT ROW -> position {pos}"
        if bound.direction == "PRECEDING":
            raw = pos - bound.offset
            value = max(0, raw)
            return value, (
                f"{bound.offset} PRECEDING -> {pos} - {bound.offset} = {raw}; "
                f"clamped to [0, {n}] -> {value}"
            )
        raw = pos + bound.offset
        value = min(raw, n)
        return value, (
            f"{bound.offset} FOLLOWING -> {pos} + {bound.offset} = {raw}; "
            f"clamped to [0, {n}] -> {value}"
        )

    def end_pos(bound: FrameBound) -> tuple[int, str]:
        if bound.kind == "UNBOUNDED":
            return n, f"UNBOUNDED FOLLOWING -> partition end (position {n})"
        if bound.kind == "CURRENT_ROW":
            return pos + 1, f"CURRENT ROW -> exclusive end {pos + 1}"
        if bound.direction == "PRECEDING":
            raw = pos - bound.offset + 1
            value = max(0, raw)
            return value, (
                f"{bound.offset} PRECEDING -> exclusive {pos} - {bound.offset} + 1 "
                f"= {raw}; clamped to [0, {n}] -> {value}"
            )
        raw = pos + bound.offset + 1
        value = min(raw, n)
        return value, (
            f"{bound.offset} FOLLOWING -> exclusive {pos} + {bound.offset} + 1 = "
            f"{raw}; clamped to [0, {n}] -> {value}"
        )

    head, head_reason = start_pos(frame.start)
    tail, tail_reason = end_pos(frame.end)
    return head, tail, head_reason, tail_reason


def _groups_bounds(
    partition: Partition, pos: int, frame: FrameSpec
) -> tuple[int, int, str, str]:
    group = partition.groups[partition.group_of[partition.rows[pos].event_id]]
    gi = group.index
    count = len(partition.groups)
    n = len(partition.rows)

    def start_pos(bound: FrameBound) -> tuple[int, str]:
        if bound.kind == "UNBOUNDED":
            return 0, "UNBOUNDED PRECEDING -> first peer group (position 0)"
        if bound.kind == "CURRENT_ROW":
            return group.start, (
                f"CURRENT ROW -> start of peer group {gi} (position {group.start})"
            )
        if bound.direction == "PRECEDING":
            target = gi - bound.offset
            if target < 0:
                return 0, (
                    f"{bound.offset} PRECEDING -> group {gi} - {bound.offset} is "
                    "before the partition; clamped to position 0"
                )
            target_group = partition.groups[target]
            return target_group.start, (
                f"{bound.offset} PRECEDING -> group {target}; head at its first "
                f"row (position {target_group.start})"
            )
        target = gi + bound.offset
        if target >= count:
            return n, (
                f"{bound.offset} FOLLOWING -> group {gi} + {bound.offset} is past "
                f"the last group; head at partition end (position {n})"
            )
        target_group = partition.groups[target]
        return target_group.start, (
            f"{bound.offset} FOLLOWING -> group {target}; head at its first row "
            f"(position {target_group.start})"
        )

    def end_pos(bound: FrameBound) -> tuple[int, str]:
        if bound.kind == "UNBOUNDED":
            return n, f"UNBOUNDED FOLLOWING -> partition end (position {n})"
        if bound.kind == "CURRENT_ROW":
            return group.end, (
                f"CURRENT ROW -> end of peer group {gi} (exclusive position "
                f"{group.end})"
            )
        if bound.direction == "PRECEDING":
            target = gi - bound.offset
            if target < 0:
                return 0, (
                    f"{bound.offset} PRECEDING -> group {gi} - {bound.offset} is "
                    "before the partition; tail at position 0"
                )
            target_group = partition.groups[target]
            return target_group.end, (
                f"{bound.offset} PRECEDING -> group {target}; tail after its last "
                f"row (position {target_group.end})"
            )
        target = gi + bound.offset
        if target >= count:
            return n, (
                f"{bound.offset} FOLLOWING -> group {gi} + {bound.offset} is past "
                f"the last group; tail at partition end (position {n})"
            )
        target_group = partition.groups[target]
        return target_group.end, (
            f"{bound.offset} FOLLOWING -> group {target}; tail after its last row "
            f"(position {target_group.end})"
        )

    head, head_reason = start_pos(frame.start)
    tail, tail_reason = end_pos(frame.end)
    return head, tail, head_reason, tail_reason


def _range_bounds(
    partition: Partition, pos: int, frame: FrameSpec, window: WindowDefinition
) -> tuple[int, int, str, str]:
    n = len(partition.rows)
    key = window.order_by[0] if window.order_by else None
    current = partition.rows[pos].values.get(key.column) if key else None
    peer = (
        partition.groups[partition.group_of[partition.rows[pos].event_id]]
        if window.order_by
        else None
    )

    # PostgreSQL scans from the cheap known head/tail each time.  We reproduce
    # the *predicate* it uses in nodeWindowAgg.c exactly; starting at 0 is
    # equivalent because the frames only move forward.
    def start_bound(bound: FrameBound) -> tuple[int, str]:
        if bound.kind == "UNBOUNDED":
            return 0, "UNBOUNDED PRECEDING -> partition start (position 0)"
        if bound.kind == "CURRENT_ROW":
            value = peer.start if peer else 0
            return value, (
                "CURRENT ROW -> first row of the current peer group "
                f"(position {value})"
            )
        offset = bound.offset
        sub = bound.direction == "PRECEDING"
        less = False
        if key.descending:
            sub = not sub
            less = True
        candidate = n
        for index in range(n):
            head_value = partition.rows[index].values.get(key.column)
            if head_value is None or current is None:
                if key.nulls_first:
                    if head_value is not None or current is None:
                        candidate = index
                        break
                else:
                    if head_value is None or current is not None:
                        candidate = index
                        break
            elif _in_range_int4(head_value, current, offset, sub, less):
                candidate = index
                break
        if current is None:
            reason = (
                f"RANGE {bound.describe()}: current {key.column} is NULL; head is "
                f"decided only by NULLS {'FIRST' if key.nulls_first else 'LAST'} "
                f"-> position {candidate}"
            )
        else:
            threshold = current + (-offset if sub else offset)
            op = "<=" if less else ">="
            reason = (
                f"RANGE {bound.describe()} on {key.column} "
                f"({'DESC' if key.descending else 'ASC'} NULLS "
                f"{'FIRST' if key.nulls_first else 'LAST'}): head is the first "
                f"row whose value {op} {threshold} (current {current}) -> "
                f"position {candidate}"
            )
        return candidate, reason

    def end_bound(bound: FrameBound) -> tuple[int, str]:
        if bound.kind == "UNBOUNDED":
            return n, f"UNBOUNDED FOLLOWING -> partition end (position {n})"
        if bound.kind == "CURRENT_ROW":
            value = peer.end if peer else n
            return value, (
                "CURRENT ROW -> after the current peer group "
                f"(exclusive position {value})"
            )
        offset = bound.offset
        sub = bound.direction == "PRECEDING"
        less = True
        if key.descending:
            sub = not sub
            less = False
        candidate = n
        for index in range(n):
            tail_value = partition.rows[index].values.get(key.column)
            if tail_value is None or current is None:
                if key.nulls_first:
                    if tail_value is not None:
                        candidate = index
                        break
                else:
                    if current is not None:
                        candidate = index
                        break
            elif not _in_range_int4(tail_value, current, offset, sub, less):
                candidate = index
                break
        if current is None:
            reason = (
                f"RANGE {bound.describe()}: current {key.column} is NULL; tail is "
                f"decided only by NULLS {'FIRST' if key.nulls_first else 'LAST'} "
                f"-> position {candidate}"
            )
        else:
            threshold = current + (-offset if sub else offset)
            rule = (
                f"tail is the first row whose value > {threshold}"
                if less
                else f"tail is the first row whose value < {threshold}"
            )
            reason = (
                f"RANGE {bound.describe()} on {key.column} "
                f"({'DESC' if key.descending else 'ASC'} NULLS "
                f"{'FIRST' if key.nulls_first else 'LAST'}): {rule} "
                f"(current {current}) -> position {candidate}"
            )
        return candidate, reason

    head, head_reason = start_bound(frame.start)
    tail, tail_reason = end_bound(frame.end)
    return head, tail, head_reason, tail_reason


def compute_bounds(partition: Partition, pos: int, window: WindowDefinition) -> FrameBounds:
    frame = resolved_frame(window)
    if frame.mode == "ROWS":
        head, tail, head_reason, tail_reason = _rows_bounds(pos, len(partition.rows), frame)
    elif frame.mode == "GROUPS":
        head, tail, head_reason, tail_reason = _groups_bounds(partition, pos, frame)
    elif frame.mode == "RANGE":
        head, tail, head_reason, tail_reason = _range_bounds(partition, pos, frame, window)
    else:  # pragma: no cover
        raise ValueError(f"unsupported frame mode {frame.mode!r}")
    return FrameBounds(head, tail, head_reason, tail_reason, frame.mode, frame.start, frame.end)


# ------------------------------------------------------------------
# Variants: possible positions of the result row inside its peer group
# ------------------------------------------------------------------


@dataclass
class GroupSlice:
    group: PeerGroup
    overlap: int
    current_included: bool

    @property
    def kind(self) -> str:
        if self.overlap <= 0:
            return "none"
        if self.overlap >= self.group.size:
            return "full"
        return "partial"


@dataclass
class Variant:
    slot: Optional[int]
    position: int
    bounds: FrameBounds
    slices: list[GroupSlice]
    current_group_index: int

    @property
    def current_in_frame(self) -> bool:
        return any(s.current_included for s in self.slices)

    @property
    def partial_groups(self) -> list[GroupSlice]:
        return [s for s in self.slices if s.kind == "partial"]


def make_variants(partition: Partition, row: Row, window: WindowDefinition) -> list[Variant]:
    frame = resolved_frame(window)
    group = partition.groups[partition.group_of[row.event_id]]
    if frame.mode == "ROWS":
        slots: list[Optional[int]] = list(range(group.size))
    else:
        # GROUPS/RANGE bounds depend on peer-group edges only.
        slots = [None]
    variants: list[Variant] = []
    for slot in slots:
        if slot is None:
            pos = partition.position_of[row.event_id]
        else:
            pos = group.start + slot
        bounds = compute_bounds(partition, pos, window)
        slices: list[GroupSlice] = []
        for other in partition.groups:
            overlap = max(0, min(bounds.tail, other.end) - max(bounds.head, other.start))
            if overlap <= 0:
                continue
            slices.append(
                GroupSlice(
                    group=other,
                    overlap=overlap,
                    current_included=(
                        other.index == group.index and bounds.head <= pos < bounds.tail
                    ),
                )
            )
        variants.append(
            Variant(
                slot=slot,
                position=pos,
                bounds=bounds,
                slices=slices,
                current_group_index=group.index,
            )
        )
    return variants


# ------------------------------------------------------------------
# Aggregate options for one variant
# ------------------------------------------------------------------


def _subset_count(values: list[Optional[int]], k: int) -> tuple[int, int]:
    non_null = sum(1 for v in values if v is not None)
    nulls = len(values) - non_null
    return max(0, k - nulls), min(k, non_null)


def _subset_sum(values: list[Optional[int]], k: int) -> tuple[int, int, bool, bool]:
    """(min_sum, max_sum, can_be_all_null, can_be_numeric)."""
    non_null = sorted(v for v in values if v is not None)
    nulls = len(values) - len(non_null)
    min_count = max(0, k - nulls)
    max_count = min(k, len(non_null))
    min_sum = sum(non_null[:min_count])
    max_sum = sum(non_null[len(non_null) - max_count :]) if max_count else 0
    return min_sum, max_sum, k <= nulls, max_count > 0


def variant_options(
    function: str, argument: Optional[str], variant: Variant, current: Row
) -> tuple[int, int, bool, bool]:
    """Return ``(min, max, can_be_null, can_be_nonnull)`` for one variant.

    For COUNT this is a count range; for COUNT(*) it is the fixed position
    count; the NULL flags only matter for SUM.  An empty frame is recorded as
    ``(0, 0, True, False)`` and interpreted by :func:`evaluate` (empty SUM is
    NULL, never 0).
    """
    if function == "COUNT_STAR":
        size = variant.bounds.size
        return size, size, False, True

    if variant.bounds.empty:
        return 0, 0, True, False

    low = 0
    high = 0
    can_all_null = True
    can_numeric = False
    for item in variant.slices:
        values = [row.values.get(argument) for row in item.group.rows]
        k = item.overlap
        forced = False
        if item.kind == "full":
            if function == "COUNT":
                count = sum(1 for v in values if v is not None)
                low += count
                high += count
            else:
                non_null = [v for v in values if v is not None]
                low += sum(non_null)
                high += sum(non_null)
                can_all_null = can_all_null and not non_null
                can_numeric = can_numeric or bool(non_null)
            continue

        is_current = item.group.index == variant.current_group_index
        if is_current and item.current_included:
            current_index = next(
                idx for idx, row in enumerate(item.group.rows) if row.event_id == current.event_id
            )
            others = [v for idx, v in enumerate(values) if idx != current_index]
            forced = True
            current_value = values[current_index]
            inner_k = k - 1
        elif is_current:
            current_index = next(
                idx for idx, row in enumerate(item.group.rows) if row.event_id == current.event_id
            )
            others = [v for idx, v in enumerate(values) if idx != current_index]
            forced = False
            current_value = None
            inner_k = k
        else:
            others = values
            forced = False
            current_value = None
            inner_k = k

        if function == "COUNT":
            low_o, high_o = _subset_count(others, inner_k)
            low += low_o
            high += high_o
            if forced:
                forced_non_null = 1 if current_value is not None else 0
                low += forced_non_null
                high += forced_non_null
        else:
            min_o, max_o, null_o, numeric_o = _subset_sum(others, inner_k)
            low += min_o
            high += max_o
            if forced:
                low += current_value if current_value is not None else 0
                high += current_value if current_value is not None else 0
                can_all_null = can_all_null and (current_value is None) and null_o
                can_numeric = can_numeric or (current_value is not None) or numeric_o
            else:
                can_all_null = can_all_null and null_o
                can_numeric = can_numeric or numeric_o
    return low, high, can_all_null, can_numeric


def variant_value_candidates(
    function: str, argument: str, variant: Variant, current: Row, row_order: list[Row],
    mode: str,
) -> list[Any]:
    """Possible FIRST_VALUE/LAST_VALUE results for one variant."""
    boundary = variant.bounds.head if function == "FIRST_VALUE" else variant.bounds.tail - 1
    if boundary < 0 or boundary >= len(row_order):
        return []
    group = None
    for slice_item in variant.slices:
        if slice_item.group.start <= boundary < slice_item.group.end:
            group = slice_item.group
            break
    if group is None:
        return []
    if mode != "ROWS":
        # The boundary group is fully included; any of its rows may occupy the
        # first/last frame slot.
        return [row.values.get(argument) for row in group.rows]
    if group.index == variant.current_group_index:
        if boundary == variant.position:
            return [current.values.get(argument)]
        return [
            row.values.get(argument)
            for row in group.rows
            if row.event_id != current.event_id
        ]
    return [row.values.get(argument) for row in group.rows]


# ------------------------------------------------------------------
# Choices across all variants
# ------------------------------------------------------------------


def _merge_candidates(values: list[Any]) -> tuple[bool, Any, list[Any]]:
    unique: list[Any] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if len(unique) == 1:
        return True, unique[0], unique
    return False, None, unique


def evaluate(
    function: str,
    argument: Optional[str],
    variants: list[Variant],
    current: Row,
    partition: Partition,
    mode: str,
) -> dict:
    frame = variants[0].bounds
    if all(variant.bounds.empty for variant in variants):
        if function in ("SUM", "FIRST_VALUE", "LAST_VALUE"):
            return {
                "deterministic": True,
                "value": None,
                "reason": "empty frame -> aggregate/value function returns NULL",
            }
        return {
            "deterministic": True,
            "value": 0,
            "reason": "empty frame -> COUNT returns 0",
        }

    if function in ("FIRST_VALUE", "LAST_VALUE"):
        candidates: list[Any] = []
        for variant in variants:
            if variant.bounds.empty:
                candidates.append(None)
                continue
            candidates.extend(
                variant_value_candidates(
                    function, argument, variant, current, partition.rows, mode
                )
            )
        deterministic, value, unique = _merge_candidates(candidates)
        if deterministic:
            return {
                "deterministic": True,
                "value": value,
                "possible_values": unique,
                "reason": (
                    f"{function} selects the {'first' if function == 'FIRST_VALUE' else 'last'} "
                    "row of each admissible frame; all candidates agree on the value"
                ),
            }
        return {
            "deterministic": False,
            "possible_values": unique,
            "null_possible": any(v is None for v in unique),
            "reason": (
                f"{function} selects the {'first' if function == 'FIRST_VALUE' else 'last'} "
                "row of the frame, but the declared ordering does not determine "
                "which row occupies that frame slot; candidates are "
                f"{unique}"
            ),
        }

    options = [variant_options(function, argument, variant, current) for variant in variants]
    if function in ("COUNT", "COUNT_STAR"):
        low = min(option[0] for option in options)
        high = max(option[1] for option in options)
        if low == high:
            return {
                "deterministic": True,
                "value": low,
                "reason": (
                    "the frame position count / non-NULL count is fixed for every "
                    "admissible ordering"
                ),
            }
        return {
            "deterministic": False,
            "possible_min": low,
            "possible_max": high,
            "reason": (
                "frame membership depends on the undetermined within-peer-group "
                f"positions; possible counts range from {low} to {high}"
            ),
        }

    # SUM: empty variants contribute NULL, never 0.
    non_empty = [
        option for option, variant in zip(options, variants) if not variant.bounds.empty
    ]
    if not non_empty:
        return {
            "deterministic": True,
            "value": None,
            "reason": "empty frame -> SUM returns NULL",
        }
    low = min(option[0] for option in non_empty)
    high = max(option[1] for option in non_empty)
    can_be_null = any(variant.bounds.empty for variant in variants) or any(
        option[2] for option in non_empty
    )
    if not any(option[3] for option in non_empty):
        return {
            "deterministic": True,
            "value": None,
            "reason": "SUM ignores NULL and every row selectable in the frame is NULL -> NULL",
        }
    if low == high and not can_be_null:
        return {
            "deterministic": True,
            "value": low,
            "reason": (
                "SUM ignores NULL; every admissible frame selection contributes the "
                f"same total ({low})"
            ),
        }
    return {
        "deterministic": False,
        "possible_min": low,
        "possible_max": high,
        "null_possible": can_be_null,
        "reason": (
            "SUM depends on which rows occupy frame positions that the declared "
            f"ordering leaves undetermined; possible totals range from {low} to "
            f"{high}" + (" and NULL is possible" if can_be_null else "")
        ),
    }


def membership(variants: list[Variant], current: Row, partition: Partition) -> dict:
    concrete_sets: list[set[int]] = []
    guaranteed_sets: list[set[int]] = []
    all_exact = True
    ambiguous: dict[int, dict] = {}
    for variant in variants:
        if variant.partial_groups:
            all_exact = False
        guaranteed: set[int] = set()
        concrete: set[int] = set()
        for item in variant.slices:
            if item.kind == "full":
                guaranteed.update(item.group.event_ids)
                concrete.update(item.group.event_ids)
            elif item.kind == "partial":
                if item.current_included:
                    guaranteed.add(current.event_id)
                entry = ambiguous.setdefault(
                    item.group.index,
                    {
                        "group_index": item.group.index,
                        "group_size": item.group.size,
                        "event_ids": sorted(item.group.event_ids),
                        "order_values": item.group.order_values,
                        "included_counts": [],
                        "reason": (
                            "a positional ROWS boundary cuts this peer group; the "
                            "declared ordering does not say which rows fill the "
                            "included positions"
                        ),
                    },
                )
                if item.overlap not in entry["included_counts"]:
                    entry["included_counts"].append(item.overlap)
        guaranteed_sets.append(guaranteed)
        concrete_sets.append(concrete)
    definite = set.intersection(*guaranteed_sets) if guaranteed_sets else set()
    deterministic = (
        all_exact
        and len(concrete_sets) == len(variants)
        and all(concrete_sets[0] == other for other in concrete_sets[1:])
    )
    result = {"deterministic": deterministic, "definite_event_ids": sorted(definite)}
    if deterministic:
        result["event_ids"] = sorted(concrete_sets[0])
    else:
        result["ambiguous_groups"] = [
            {
                **_drop_counts(entry),
                "included_count_min": min(entry["included_counts"]),
                "included_count_max": max(entry["included_counts"]),
            }
            for entry in ambiguous.values()
        ]
    return result


def _drop_counts(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key != "included_counts"}


# ------------------------------------------------------------------
# Top level
# ------------------------------------------------------------------


def explain(query: Query, rows: list[Row]) -> dict:
    partition_cache: dict[tuple, list[Partition]] = {}

    def partitions_for(window: WindowDefinition) -> list[Partition]:
        cache_key = (
            tuple(window.partition_by),
            tuple((key.column, key.descending, key.nulls_first) for key in window.order_by),
        )
        if cache_key not in partition_cache:
            partition_cache[cache_key] = build_partitions(window, rows)
        return partition_cache[cache_key]

    results: list[dict] = []
    nondeterministic: list[dict] = []

    for expression in query.windows:
        window = expression.window
        frame = resolved_frame(window)
        partitions = partitions_for(window)
        for partition in partitions:
            for row in partition.rows:
                variants = make_variants(partition, row, window)
                group = partition.groups[partition.group_of[row.event_id]]
                outcome = evaluate(
                    expression.function,
                    expression.argument,
                    variants,
                    row,
                    partition,
                    frame.mode,
                )
                membership_info = membership(variants, row, partition)
                item = {
                    "event_id": row.event_id,
                    "expression": expression.output_name,
                    "function": expression.function,
                    "argument": expression.argument,
                    "partition_index": partition.index,
                    "partition_key": partition.key_values,
                    "group_index": group.index,
                    "source_row": {name: row.values.get(name) for name in row.values},
                    "peer_group": {
                        "index": group.index,
                        "size": group.size,
                        "start_position": group.start,
                        "end_position_exclusive": group.end,
                        "order_values": group.order_values,
                        "event_ids": sorted(group.event_ids),
                    },
                    "frame": {
                        "mode": frame.mode,
                        "explicit": frame.explicit,
                        "start_bound": frame.start.describe(),
                        "end_bound": frame.end.describe(),
                        "variants": [
                            {
                                "current_slot_in_peer_group": variant.slot,
                                "current_position": variant.position,
                                "head_position": variant.bounds.head,
                                "tail_position_exclusive": variant.bounds.tail,
                                "size": variant.bounds.size,
                                "empty": variant.bounds.empty,
                                "head_basis": variant.bounds.head_reason,
                                "tail_basis": variant.bounds.tail_reason,
                                "groups": [
                                    {
                                        "group_index": slice_item.group.index,
                                        "inclusion": slice_item.kind,
                                        "included_count": slice_item.overlap,
                                        "group_size": slice_item.group.size,
                                        "event_ids": sorted(slice_item.group.event_ids),
                                    }
                                    for slice_item in variant.slices
                                ],
                            }
                            for variant in variants
                        ],
                    },
                    "members": membership_info,
                    "value": outcome,
                }
                results.append(item)
                if not outcome["deterministic"]:
                    nondeterministic.append(
                        {
                            "event_id": row.event_id,
                            "expression": expression.output_name,
                            "function": expression.function,
                            "possible_min": outcome.get("possible_min"),
                            "possible_max": outcome.get("possible_max"),
                            "possible_values": outcome.get("possible_values"),
                            "null_possible": outcome.get("null_possible"),
                            "reason": outcome["reason"],
                        }
                    )

    results.sort(key=lambda item: (item["event_id"], item["expression"]))

    return {
        "query_id": query.query_id,
        "sql": query.sql,
        "table": query.table,
        "identity": query.identity,
        "row_count": len(rows),
        "expressions": [
            {
                "name": expression.output_name,
                "function": expression.function,
                "argument": expression.argument,
                "window_definition": window_json(
                    expression.window, resolved_frame(expression.window)
                ),
            }
            for expression in query.windows
        ],
        "results": results,
        "nondeterministic": nondeterministic,
        "summary": {
            "result_rows": len(results),
            "deterministic_values": sum(1 for item in results if item["value"]["deterministic"]),
            "nondeterministic_values": len(nondeterministic),
            "nondeterministic_membership_rows": sum(
                1 for item in results if not item["members"]["deterministic"]
            ),
        },
    }


def window_json(window: WindowDefinition, frame: FrameSpec) -> dict:
    return {
        "name": window.name,
        "references": window.referenced,
        "partition_by": list(window.partition_by),
        "order_by": [
            {
                "column": key.column,
                "direction": "DESC" if key.descending else "ASC",
                "nulls": "FIRST" if key.nulls_first else "LAST",
            }
            for key in window.order_by
        ],
        "frame": {
            "mode": frame.mode,
            "explicit": frame.explicit,
            "start": frame.start.describe(),
            "end": frame.end.describe(),
        },
    }
