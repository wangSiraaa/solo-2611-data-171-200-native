"""Capture selection, revisit resolution and redirect following for one cutoff.

Selection: latest WARC-Date <= cutoff; ties broken by lexicographically
smallest Record-ID.  A failed selection is never silently replaced by an
older successful capture.

Revisit resolution: an explicit WARC-Refers-To is authoritative and must
resolve (no silent digest fallback when it is broken); without Refers-To the
payload may be recovered by digest among response payloads with date <=
cutoff.  Reused payload bytes are verified against the revisit's stated
sha1-base32 digest.  Only the payload is reused -- the revisit's own HTTP
headers keep the current capture identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .urls import origin_of, resolve_reference
from .warcfile import Record, Store, full_digest

REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class Reason:
    NO_CAPTURE = "no-capture-before-cutoff"
    MISSING_REFERENCE = "missing-revisit-reference"
    REFERENCE_CYCLE = "revisit-reference-cycle"
    DIGEST_MISMATCH = "revisit-digest-mismatch"
    NO_DIGEST_SOURCE = "revisit-no-digest-source"
    RECORD_DIGEST_MISMATCH = "record-digest-mismatch"
    REDIRECT_CYCLE = "redirect-cycle"
    MISSING_FRAGMENT = "missing-fragment-target"


@dataclass
class Gap:
    reason: str
    url: str
    detail: str
    chain: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "reason": self.reason,
            "url": self.url,
            "detail": self.detail,
            "chain": self.chain,
        }


@dataclass
class PayloadResult:
    data: bytes
    digest: str                 # computed sha1-b32 of data
    provenance_kind: str        # 'self' | 'refers-to' | 'digest-match'
    supports: list[str]         # record ids backing the payload bytes
    revisit_chain: list[str]    # revisit record ids traversed to reach bytes


@dataclass
class Resource:
    canon: str                  # final canonical URL (after any redirects)
    record: Record              # selected record of the final URL
    payload: PayloadResult
    content_type: str
    main_type: str
    kind: str                   # 'html' | 'css' | 'binary'
    status: int


@dataclass
class ChainResult:
    requested: str
    hops: list[dict]            # redirect hops, in order
    final_canon: str | None
    resource: Resource | None
    gap: Gap | None


def effective_fragment(chain: ChainResult, incoming: str | None) -> str | None:
    """Fragment after redirects: a Location fragment replaces the incoming
    one; a Location without fragment preserves it."""
    frag = incoming
    for hop in chain.hops:
        if hop.get("fragment") is not None:
            frag = hop["fragment"]
    return frag


class Resolver:
    def __init__(self, store: Store, cutoff: datetime, allowed_origins: list[str]):
        self.store = store
        self.cutoff = cutoff
        self.allowed = {origin_of(o) for o in allowed_origins}
        self._payload_cache: dict[str, PayloadResult | Gap] = {}
        self._chain_cache: dict[str, ChainResult] = {}

    # -- capture selection -------------------------------------------------

    def select(self, canon: str) -> Record | None:
        cands = [r for r in self.store.by_canon.get(canon, []) if r.dt <= self.cutoff]
        if not cands:
            return None
        latest = max(r.dt for r in cands)
        tied = [r for r in cands if r.dt == latest]
        return min(tied, key=lambda r: r.record_id)

    # -- payload resolution ------------------------------------------------

    def resolve_payload(self, record: Record) -> PayloadResult | Gap:
        if record.record_id in self._payload_cache:
            return self._payload_cache[record.record_id]
        result = self._payload_for(record, [record.record_id])
        self._payload_cache[record.record_id] = result
        return result

    def _payload_for(self, record: Record, trail: list[str]) -> PayloadResult | Gap:
        if record.rtype == "response":
            if record.digest and record.computed_digest != record.digest:
                return Gap(
                    Reason.RECORD_DIGEST_MISMATCH,
                    record.canon,
                    f"response record {record.record_id} states {record.digest} "
                    f"but actual bytes hash to {record.computed_digest}",
                    list(trail),
                )
            return PayloadResult(
                record.payload, record.computed_digest, "self",
                [record.record_id], [],
            )
        # revisit record: only the payload is reused, headers stay current
        if record.refers_to:
            ref = record.refers_to
            if ref in trail:
                return Gap(
                    Reason.REFERENCE_CYCLE,
                    record.canon,
                    "WARC-Refers-To chain loops: " + " -> ".join(trail + [ref]),
                    trail + [ref],
                )
            target = self.store.by_id.get(ref)
            if target is None:
                return Gap(
                    Reason.MISSING_REFERENCE,
                    record.canon,
                    f"explicit WARC-Refers-To {ref} is not present in the "
                    f"archive; digest fallback is not allowed for explicit "
                    f"references",
                    trail + [ref],
                )
            sub = self._payload_for(target, trail + [ref])
            if isinstance(sub, Gap):
                return sub
            if record.digest and sub.digest != record.digest:
                return Gap(
                    Reason.DIGEST_MISMATCH,
                    record.canon,
                    f"revisit {record.record_id} states digest {record.digest} "
                    f"but the referenced payload from {ref} hashes to "
                    f"{sub.digest}",
                    trail + [ref],
                )
            return PayloadResult(
                sub.data, sub.digest, "refers-to", sub.supports,
                [record.record_id] + sub.revisit_chain,
            )
        # no WARC-Refers-To: resolve by digest among response payloads
        # captured at or before the cutoff
        supports = [
            r for r in self.store.records
            if r.rtype == "response"
            and r.dt <= self.cutoff
            and r.digest is not None
            and r.digest == record.digest
            and r.computed_digest == r.digest
        ]
        if not supports:
            return Gap(
                Reason.NO_DIGEST_SOURCE,
                record.canon,
                f"revisit {record.record_id} has no WARC-Refers-To and no "
                f"response payload with digest {record.digest} exists at or "
                f"before the cutoff",
                list(trail),
            )
        data = supports[0].payload
        digest = full_digest(data)
        if record.digest and digest != record.digest:
            return Gap(
                Reason.DIGEST_MISMATCH,
                record.canon,
                f"revisit {record.record_id} states digest {record.digest} "
                f"but the digest-matched payload hashes to {digest}",
                list(trail),
            )
        return PayloadResult(
            data, digest, "digest-match",
            [r.record_id for r in supports],
            [record.record_id],
        )

    # -- redirect + resource resolution ------------------------------------

    def resolve_url(self, canon: str) -> ChainResult:
        if canon in self._chain_cache:
            return self._chain_cache[canon]
        hops: list[dict] = []
        seen: list[str] = []
        cur = canon
        result: ChainResult | None = None
        while True:
            if cur in seen:
                result = ChainResult(
                    canon, hops, None, None,
                    Gap(
                        Reason.REDIRECT_CYCLE,
                        cur,
                        "redirect loop detected by canonical URL: "
                        + " -> ".join(seen + [cur]),
                        seen + [cur],
                    ),
                )
                break
            seen.append(cur)
            record = self.select(cur)
            if record is None:
                result = ChainResult(
                    canon, hops, None, None,
                    Gap(
                        Reason.NO_CAPTURE,
                        cur,
                        f"no capture of {cur} at or before the cutoff; the "
                        f"selected state is not replaced by older content",
                        list(seen),
                    ),
                )
                break
            location = record.header("Location")
            if record.status in REDIRECT_STATUSES and location:
                target_canon, target_frag = resolve_reference(cur, location)
                hops.append({
                    "canon": cur,
                    "record_id": record.record_id,
                    "warc_date": record.date,
                    "status": record.status,
                    "location": location,
                    "resolved_canon": target_canon,
                    "fragment": target_frag,
                    "payload_digest_verified": (
                        record.digest is not None
                        and record.digest == record.computed_digest
                    ),
                })
                cur = target_canon
                continue
            payload = self.resolve_payload(record)
            if isinstance(payload, Gap):
                # report the gap against the URL whose capture was selected,
                # not an intermediate record of a revisit chain
                payload.url = cur
                result = ChainResult(canon, hops, cur, None, payload)
            else:
                main_type = record.content_type.split(";")[0].strip().lower()
                if main_type == "text/html":
                    kind = "html"
                elif main_type == "text/css":
                    kind = "css"
                else:
                    kind = "binary"
                result = ChainResult(
                    canon, hops, cur,
                    Resource(cur, record, payload, record.content_type,
                             main_type, kind, record.status),
                    None,
                )
            break
        self._chain_cache[canon] = result
        return result
