"""Model declaration loading, reachability analysis and structural validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ValidationError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str) -> str:
    """Quote an identifier for SQLite after checking it is a plain name."""
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValidationError(f"illegal SQL identifier in model: {name!r}")
    return '"' + name + '"'


@dataclass
class Fact:
    name: str
    key: tuple[str, ...]
    entity_key: str


@dataclass
class Relation:
    src: str
    src_key: str
    dst: str
    dst_key: str
    cardinality: str


@dataclass
class Dimension:
    name: str
    table: str
    column: str


@dataclass
class Metric:
    name: str
    kind: str                       # "sum" | "count" | "ratio"
    fact: str | None = None
    column: str | None = None
    numerator: str | None = None
    denominator: str | None = None


@dataclass
class Semantics:
    group_domain: str
    empty_measure: object
    zero_denominator: object
    absent_all_metrics: str
    money: str
    reachable_dimension: str
    filters: str
    predicate_ops: tuple[str, ...]


@dataclass
class DimensionPath:
    """How a fact reaches a dimension: fact -> ... -> dimension table."""

    fact: str
    dimension: str
    hops: list[Relation]

    def describe(self) -> str:
        parts = [self.fact]
        for hop in self.hops:
            parts.append(f"-{hop.cardinality}->{hop.dst}")
        return "".join(parts)


@dataclass
class Model:
    raw: dict
    entity: Fact
    facts: dict[str, Fact]
    relations: list[Relation]
    dimensions: dict[str, Dimension]
    metrics: dict[str, Metric]
    semantics: Semantics
    _paths: dict[tuple[str, str], DimensionPath] = field(default_factory=dict, repr=False)
    _catalog: dict[str, set[str]] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: str | Path) -> "Model":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValidationError(f"model file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValidationError(f"model file is not valid JSON: {path}: {exc}") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Model":
        try:
            entity = Fact(
                name=raw["entity"]["table"],
                key=tuple(raw["entity"]["key"]),
                entity_key=raw["entity"]["key"][0],
            )
            facts = {
                name: Fact(name=name, key=tuple(decl["key"]), entity_key=decl["entity_key"])
                for name, decl in raw["facts"].items()
            }
            relations = [
                Relation(
                    src=r["from"], src_key=r["from_key"],
                    dst=r["to"], dst_key=r["to_key"], cardinality=r["cardinality"],
                )
                for r in raw.get("relations", [])
            ]
            dimensions = {
                name: Dimension(name=name, table=d["table"], column=d["column"])
                for name, d in raw.get("dimensions", {}).items()
            }
            metrics = {
                name: Metric(
                    name=name,
                    kind=m.get("op", "sum"),
                    fact=m.get("fact"),
                    column=m.get("column"),
                    numerator=m.get("numerator"),
                    denominator=m.get("denominator"),
                )
                for name, m in raw.get("metrics", {}).items()
            }
            sem = raw["semantics"]
            semantics = Semantics(
                group_domain=sem["group_domain"],
                empty_measure=sem["empty_measure"],
                zero_denominator=sem["zero_denominator"],
                absent_all_metrics=sem["absent_all_metrics"],
                money=sem["money"],
                reachable_dimension=sem["reachable_dimension"],
                filters=sem["filters"],
                predicate_ops=tuple(sem["predicate_ops"]),
            )
        except (KeyError, TypeError) as exc:
            raise ValidationError(f"model declaration is malformed: missing {exc}") from exc
        model = cls(
            raw=raw, entity=entity, facts=facts, relations=relations,
            dimensions=dimensions, metrics=metrics, semantics=semantics,
        )
        model._validate_structure()
        model._build_paths()
        return model

    # ----------------------------------------------------------- validation
    def _validate_structure(self) -> None:
        names = set(self.facts)
        for f in self.facts.values():
            _ = [quote_ident(k) for k in f.key]
            quote_ident(f.entity_key)
        for rel in self.relations:
            if rel.src not in names:
                raise ValidationError(f"relation source fact is not declared: {rel.src}")
            if rel.dst not in names and rel.dst not in self.dimensions:
                # destination may be a dimension table (customers/products)
                dim_tables = {d.table for d in self.dimensions.values()}
                if rel.dst not in dim_tables:
                    raise ValidationError(f"relation destination is not declared: {rel.dst}")
            if rel.cardinality != "many_to_one":
                raise ValidationError(
                    f"unsupported cardinality {rel.cardinality!r} on "
                    f"{rel.src}->{rel.dst}; only many_to_one is reachable"
                )
        for m in self.metrics.values():
            if m.kind in ("sum", "count"):
                if m.fact not in self.facts:
                    raise ValidationError(f"metric {m.name!r} references unknown fact {m.fact!r}")
                if m.kind == "sum" and not m.column:
                    raise ValidationError(f"sum metric {m.name!r} needs a column")
                if m.column:
                    quote_ident(m.column)
            elif m.kind == "ratio":
                for part in (m.numerator, m.denominator):
                    if part not in self.metrics:
                        raise ValidationError(
                            f"ratio {m.name!r} references unknown metric {part!r}"
                        )
                    if self.metrics[part].kind == "ratio":
                        raise ValidationError(
                            f"ratio {m.name!r} references ratio {part!r}; nested "
                            f"ratios are not supported (only base quantities)"
                        )
            else:
                raise ValidationError(f"unsupported metric op {m.kind!r} on {m.name!r}")

    def _build_paths(self) -> None:
        """Enumerate, per fact, the forward many_to_one paths to dimension tables."""
        dim_by_table: dict[str, list[Dimension]] = {}
        for dim in self.dimensions.values():
            dim_by_table.setdefault(dim.table, []).append(dim)

        for fact_name, fact in self.facts.items():
            # BFS over tables starting at the fact table.
            frontier: list[tuple[str, list[Relation]]] = [(fact_name, [])]
            seen = {fact_name}
            while frontier:
                table, hops = frontier.pop(0)
                for dim in dim_by_table.get(table, []):
                    self._paths[(fact_name, dim.name)] = DimensionPath(
                        fact=fact_name, dimension=dim.name, hops=list(hops)
                    )
                for rel in self.relations:
                    if rel.src == table and rel.dst not in seen:
                        # Only declared many_to_one links are walked, in their
                        # forward direction. A "join back down" (one_to_many) is
                        # never a dimension path, so no fan-out can enter a plan.
                        seen.add(rel.dst)
                        frontier.append((rel.dst, hops + [rel]))
        # entity (orders) is a fact too, but dimensions live on the entity table;
        # make sure the entity is represented under its declared name.
        if self.entity.name not in self.facts:
            raise ValidationError("entity table must also be declared as a fact")

    # ------------------------------------------------------------- lookups
    def path(self, fact: str, dimension: str) -> DimensionPath | None:
        return self._paths.get((fact, dimension))

    def paths_from(self, fact: str) -> list[DimensionPath]:
        return [p for (f, _), p in self._paths.items() if f == fact]

    def paths_to(self, dimension: str) -> list[DimensionPath]:
        return [p for (_, d), p in self._paths.items() if d == dimension]

    def fact_of_dimension(self, dimension: str) -> list[str]:
        return [f for f in self.facts if self.path(f, dimension) is not None]

    def metric_fact(self, name: str) -> str | None:
        m = self.metrics[name]
        return m.fact if m.kind != "ratio" else None

    def leaf_metrics(self, names: list[str]) -> list[str]:
        """Expand ratios into their base metrics (deduplicated, declaration order)."""
        out: list[str] = []
        for name in names:
            m = self.metrics[name]
            if m.kind == "ratio":
                for part in (m.numerator, m.denominator):
                    if self.metrics[part].kind == "ratio":
                        raise ValidationError(
                            f"nested ratio {name!r}->{part!r} is not supported"
                        )
                    if part not in out:
                        out.append(part)
            elif name not in out:
                out.append(name)
        return out

    # -------------------------------------------------------- verification
    def available_columns(self, connection) -> dict[str, set[str]]:
        if self._catalog:
            return self._catalog
        catalog: dict[str, set[str]] = {}
        tables = set(self.facts) | {d.table for d in self.dimensions.values()}
        for table in tables:
            rows = connection.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
            if not rows:
                raise ValidationError(f"table declared in model does not exist: {table}")
            catalog[table] = {r[1] for r in rows}
        self._catalog = catalog
        return catalog

    def validate_against_db(self, connection) -> None:
        """Static checks (tables/columns exist) plus declared cardinality checks."""
        catalog = self.available_columns(connection)

        def need(table: str, column: str, where: str) -> None:
            if column not in catalog.get(table, set()):
                raise ValidationError(
                    f"{where} refers to missing column {table}.{column}"
                )

        for fact in self.facts.values():
            for k in fact.key:
                need(fact.name, k, f"fact {fact.name!r} key")
            need(fact.name, fact.entity_key, f"fact {fact.name!r} entity_key")
        for rel in self.relations:
            need(rel.src, rel.src_key, f"relation {rel.src}->{rel.dst}")
            need(rel.dst, rel.dst_key, f"relation {rel.src}->{rel.dst}")
        for dim in self.dimensions.values():
            need(dim.table, dim.column, f"dimension {dim.name!r}")
        for m in self.metrics.values():
            if m.kind == "sum" and m.column:
                need(m.fact, m.column, f"metric {m.name!r}")

        self.verify_cardinality(connection)

    def verify_cardinality(self, connection) -> None:
        """Fail before execution if a declared many_to_one is violated."""
        from .errors import CardinalityError

        for rel in self.relations:
            # many_to_one(src -> dst): each src row must resolve to exactly ONE
            # dst row. Violations are (a) duplicate dst keys (the join would
            # fan out source rows) and (b) src keys with no dst row.
            src_table, src_key = rel.src, rel.src_key
            dst_table, dst_key = rel.dst, rel.dst_key

            dup = connection.execute(
                f"SELECT {quote_ident(dst_key)} AS k, COUNT(*) AS n "
                f"FROM {quote_ident(dst_table)} "
                f"GROUP BY {quote_ident(dst_key)} HAVING COUNT(*) > 1 "
                f"ORDER BY n DESC LIMIT 20"
            ).fetchall()
            if dup:
                violations = [
                    {"relation": f"{rel.src}.{rel.src_key}->{rel.dst}.{rel.dst_key}",
                     "kind": "destination_key_not_unique", "key": r[0],
                     "count": r[1], "expected": 1}
                    for r in dup
                ]
                raise CardinalityError(
                    f"declared {rel.cardinality} {rel.src}.{rel.src_key}->"
                    f"{rel.dst}.{rel.dst_key} is violated: destination key is not "
                    f"unique in {dst_table!r}, so one {src_table!r} row would match "
                    f"multiple rows and aggregate values would fan out "
                    f"({len(dup)} offending key(s)); refusing to aggregate.",
                    violations,
                )

            missing = connection.execute(
                f"SELECT s.{quote_ident(src_key)} AS k, "
                f"COUNT(DISTINCT d.{quote_ident(dst_key)}) AS n "
                f"FROM {quote_ident(src_table)} s "
                f"LEFT JOIN {quote_ident(dst_table)} d "
                f"ON s.{quote_ident(src_key)} = d.{quote_ident(dst_key)} "
                f"WHERE d.{quote_ident(dst_key)} IS NULL "
                f"GROUP BY s.{quote_ident(src_key)} "
                f"ORDER BY n DESC LIMIT 20"
            ).fetchall()
            if missing:
                violations = [
                    {"relation": f"{rel.src}.{rel.src_key}->{rel.dst}.{rel.dst_key}",
                     "kind": "missing_destination", "key": r[0], "count": r[1]}
                    for r in missing
                ]
                raise CardinalityError(
                    f"declared {rel.cardinality} {rel.src}.{rel.src_key}->"
                    f"{rel.dst}.{rel.dst_key} is violated: {len(missing)} source "
                    f"key(s) in {src_table!r} have no destination row in "
                    f"{dst_table!r}; refusing to aggregate.",
                    violations,
                )

        # The entity key linkage: every fact row must reference an entity.
        ent = self.entity
        for fact in self.facts.values():
            if fact.entity_key == ent.entity_key and fact.name == ent.name:
                continue
            broken = connection.execute(
                f"SELECT f.{quote_ident(fact.entity_key)} AS k, COUNT(*) AS n "
                f"FROM {quote_ident(fact.name)} f "
                f"LEFT JOIN {quote_ident(ent.name)} e "
                f"ON f.{quote_ident(fact.entity_key)} = e.{quote_ident(ent.entity_key)} "
                f"WHERE e.{quote_ident(ent.entity_key)} IS NULL "
                f"GROUP BY f.{quote_ident(fact.entity_key)} LIMIT 20"
            ).fetchall()
            if broken:
                violations = [
                    {"relation": f"{fact.name}.{fact.entity_key}->{ent.name}",
                     "kind": "orphan_entity_key", "key": r[0], "count": r[1]}
                    for r in broken
                ]
                raise CardinalityError(
                    f"fact {fact.name!r} has {len(broken)} row group(s) whose "
                    f"entity key does not exist in {ent.name!r}.",
                    violations,
                )
