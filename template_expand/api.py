"""Package-level entry points.

The top-level :func:`expand_records` opens the source template read-only,
renders one independent DOCX per record and writes a JSON manifest describing
field provenance and per-record outcomes.  A failure for one record never
affects the others and never produces a partially-rendered file: output is
written only after a record renders and validates cleanly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from docx import Document
from lxml import etree

from .engine import Contract, parse_template, render_record
from .errors import TemplateError
from .ooxml import W
from .validate import inspect_package, validate_output


@dataclass
class ExpandResult:
    statuses: list[Any] = field(default_factory=list)
    manifest_path: Optional[str] = None
    template_error: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "manifest": self.manifest_path,
            "template_error": self.template_error,
            "records": [s.to_dict() for s in self.statuses],
        }


def _record_id_of(record: Any, index: int) -> str:
    if isinstance(record, dict):
        rid = record.get("record_id")
        if isinstance(rid, str) and rid:
            return rid
    return f"record-{index}"


def _safe_filename(record_id: str, index: int) -> str:
    keep = [c if (c.isalnum() or c in "-_.") else "_" for c in record_id]
    stem = "".join(keep).strip("._") or "record"
    return f"{index:04d}_{stem}.docx"


def _success(record_id: str, output: str, field_uses, loop_rows: int,
             report: dict) -> Any:
    from .validate import RecordStatus

    return RecordStatus(
        record_id=record_id, output=output, ok=True,
        fields=field_uses, loop_rows=loop_rows, checks=report, errors=[])


def _failure(record_id: str, exc: TemplateError) -> Any:
    from .validate import RecordStatus

    status = exc.to_dict()
    status.setdefault("record_id", record_id)
    return RecordStatus(record_id=record_id, output=None, ok=False,
                        fields=[], loop_rows=0, checks={}, errors=[status])


def _save_rendered(template_path: Path, rendered_root: etree._Element,
                   out_path: Path) -> None:
    """Write ``rendered_root`` as a DOCX reusing the template package parts.

    Opening a fresh ``Document`` from the template preserves relationships,
    styles and media; only the main document body tree is swapped for the
    rendered one.  The source file is opened read-only.
    """
    doc = Document(str(template_path))
    new_body = rendered_root.find(W("body"))
    old_body = doc.element.find(W("body"))
    if new_body is None or old_body is None:
        raise TemplateError("rendered document lost its w:body")
    for child in list(old_body):
        old_body.remove(child)
    for child in new_body:
        old_body.append(etree.fromstring(etree.tostring(child)))
    doc.save(str(out_path))


def expand_records(template_path: str | Path, records_path: str | Path,
                   output_dir: str | Path,
                   manifest_path: Optional[str | Path] = None,
                   contract_path: Optional[str | Path] = None) -> ExpandResult:
    """Expand ``template_path`` once per record in ``records_path``."""
    template_path = Path(template_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(records_path, "r", encoding="utf-8") as fh:
        records = json.load(fh)
    if not isinstance(records, list):
        raise TemplateError("records file must contain a JSON array")

    contract = None
    if contract_path is not None:
        with open(contract_path, "r", encoding="utf-8") as fh:
            contract = Contract.from_mapping(json.load(fh))

    statuses = []
    template_error = None
    template_exc: Optional[TemplateError] = None
    try:
        plan = parse_template(Document(str(template_path)).element, contract)
    except TemplateError as exc:
        template_exc = exc
        template_error = exc.to_dict()
        plan = None
    except Exception as exc:  # unreadable/corrupt template
        template_exc = TemplateError(f"could not open template: {exc}")
        template_error = template_exc.to_dict()
        plan = None

    for index, record in enumerate(records, start=1):
        record_id = _record_id_of(record, index)
        if plan is None:
            assert template_exc is not None
            statuses.append(_failure(record_id, TemplateError(
                f"template is not renderable: {template_exc.message}",
                location=template_exc.location, field=template_exc.field)))
            continue
        try:
            if not isinstance(record, dict):
                raise TemplateError("record is not a JSON object",
                                    record_id=record_id)
            root, field_uses, loop_rows = render_record(plan, record, record_id)
            report = validate_output(root, record_id)
        except TemplateError as exc:
            if exc.record_id is None:
                exc.record_id = record_id
            statuses.append(_failure(record_id, exc))
            continue
        except Exception as exc:  # isolate unexpected failures per record
            statuses.append(_failure(record_id, TemplateError(
                f"internal error while rendering: {exc}", record_id=record_id)))
            continue

        out_path = output_dir / _safe_filename(record_id, index)
        tmp_path = out_path.with_suffix(".docx.tmp")
        try:
            _save_rendered(template_path, root, tmp_path)
            tmp_path.replace(out_path)
        except Exception as exc:
            if tmp_path.exists():
                tmp_path.unlink()
            statuses.append(_failure(record_id, TemplateError(
                f"failed to write output: {exc}", record_id=record_id)))
            continue

        statuses.append(_success(record_id, str(out_path), field_uses,
                                 loop_rows, report))
        # Independent ZIP/XML check of the file actually written.  If this
        # fails we remove the file so no partial output survives.
        try:
            package_report = inspect_package(out_path, record_id)
        except Exception as exc:  # noqa: BLE001 - TemplateError or zip errors
            out_path.unlink(missing_ok=True)
            statuses[-1] = _failure(record_id, TemplateError(
                f"output failed independent validation: {exc}",
                record_id=record_id))
            continue
        statuses[-1].checks.update(package_report)

    manifest = {
        "template": str(template_path),
        "template_error": template_error,
        "records": [s.to_dict() for s in statuses],
    }
    if manifest_path is None:
        manifest_path = output_dir / "manifest.json"
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return ExpandResult(statuses=statuses, manifest_path=str(manifest_path),
                        template_error=template_error)
