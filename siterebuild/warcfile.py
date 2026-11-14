"""WARC record loading: record identity, original URL, capture time, HTTP response."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from warcio.archiveiterator import ArchiveIterator

from .urls import canonicalize


def sha1_b32(data: bytes) -> str:
    """sha1 digest, base32 encoded (WARC-Payload-Digest style)."""
    return base64.b32encode(hashlib.sha1(data).digest()).decode("ascii")


def full_digest(data: bytes) -> str:
    """Digest in the normalized stated form, e.g. 'sha1:ABCD...'."""
    return "sha1:" + sha1_b32(data)


def norm_digest(value: str | None) -> str | None:
    if not value:
        return None
    algo, sep, val = value.partition(":")
    if not sep:
        return None
    return algo.strip().lower() + ":" + val.strip().upper()


def parse_warc_date(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def norm_record_id(raw: str) -> str:
    return raw.strip().strip("<>").strip()


@dataclass
class Record:
    record_id: str          # urn:uuid:... without angle brackets
    url: str                # original WARC-Target-URI
    canon: str              # canonical identity of url
    date: str               # raw WARC-Date
    dt: datetime
    rtype: str              # 'response' | 'revisit'
    digest: str | None      # stated WARC-Payload-Digest, normalized
    refers_to: str | None   # WARC-Refers-To record id, if any
    profile: str | None     # WARC-Profile, if any
    status: int             # HTTP status code
    reason: str             # HTTP reason phrase
    headers: list[tuple[str, str]]  # HTTP response headers of THIS record
    payload: bytes          # bytes after the HTTP header block
    content_type: str       # HTTP Content-Type of THIS record
    label: str              # X-Capture-Label (fixture readability aid)
    computed_digest: str    # sha1-b32 of payload bytes actually present

    def header(self, name: str) -> str | None:
        lname = name.lower()
        for key, val in self.headers:
            if key.lower() == lname:
                return val
        return None


class Store:
    def __init__(self, records: list[Record]):
        self.records = records
        self.by_id: dict[str, Record] = {r.record_id: r for r in records}
        self.by_canon: dict[str, list[Record]] = {}
        for rec in records:
            self.by_canon.setdefault(rec.canon, []).append(rec)
        for lst in self.by_canon.values():
            lst.sort(key=lambda r: (r.dt, r.record_id))


def load_store(warc_path: str) -> Store:
    records: list[Record] = []
    with open(warc_path, "rb") as fh:
        for rec in ArchiveIterator(fh):
            if rec.rec_type not in ("response", "revisit"):
                continue
            http = rec.http_headers
            if http is None:
                continue
            status = int(http.get_statuscode())
            headers = [(k, v) for k, v in http.headers]
            payload = rec.content_stream().read()
            hdr = rec.rec_headers
            url = hdr.get_header("WARC-Target-URI")
            date = hdr.get_header("WARC-Date")
            refers_to = hdr.get_header("WARC-Refers-To")
            content_type = ""
            for k, v in headers:
                if k.lower() == "content-type":
                    content_type = v
                    break
            records.append(
                Record(
                    record_id=norm_record_id(hdr.get_header("WARC-Record-ID")),
                    url=url,
                    canon=canonicalize(url),
                    date=date,
                    dt=parse_warc_date(date),
                    rtype=rec.rec_type,
                    digest=norm_digest(hdr.get_header("WARC-Payload-Digest")),
                    refers_to=norm_record_id(refers_to) if refers_to else None,
                    profile=hdr.get_header("WARC-Profile"),
                    status=status,
                    reason=http.statusline.split(" ", 2)[-1] if " " in http.statusline else "",
                    headers=headers,
                    payload=payload,
                    content_type=content_type,
                    label=next(
                        (v for k, v in headers if k.lower() == "x-capture-label"), ""
                    ),
                    computed_digest=full_digest(payload),
                )
            )
    return Store(records)
