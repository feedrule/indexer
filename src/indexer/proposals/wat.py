"""Reading Common Crawl: pick a crawl and one of its WAT files, list the `<link>` elements of its
HTML pages. Nothing here knows about feeds worth checking: callers apply their own rules.

A WAT file holds metadata for every page of a crawl, including the `<link>` elements of its HTML
head. A page that advertises a feed has
`<link rel="alternate" type="application/rss+xml" href=...>`.
A WAT file is a series of gzip members, one per WARC record; we download only its first slice
(a range request) and keep the records that arrived whole.

Everything read here is untrusted data. Downloads are not retried: if one fails, the run fails.
"""

import gzip
import io
import json
import random
import re
import zlib
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any, Protocol

_INDEX_URL = "https://index.commoncrawl.org/collinfo.json"  # every crawl, with its dates
DATA_URL = "https://data.commoncrawl.org/"

_CRAWL_ID = re.compile(r"CC-MAIN-\d{4}-\d{2}")
_WAT_PATH = re.compile(r"crawl-data/CC-MAIN-\d{4}-\d{2}/[A-Za-z0-9/._-]+\.warc\.wat\.gz")
_CHUNK = 64 * 1024

type Fetch = Callable[[str], Awaitable[bytes]]


class CommonCrawlError(Exception):
    pass


async def pick_wat_file(fetch: Fetch, target: datetime, rng: random.Random) -> tuple[str, str]:
    """(crawl id, WAT file path): a random WAT file of the crawl that ended closest to `target`."""
    crawl = _crawl_near(await fetch(_INDEX_URL), target)
    paths = _wat_paths(await fetch(f"{DATA_URL}crawl-data/{crawl}/wat.paths.gz"), crawl)
    return crawl, rng.choice(paths)


def complete_prefix(data: bytes) -> bytes:
    """The leading complete gzip members of a range download, so the record the range cut off
    is dropped, not misread."""
    # Fed in small chunks of a memoryview: passing the whole remainder for each member made this
    # quadratic (zlib copies everything after the member into unused_data), which took over an
    # hour for a 100 MB slice of ~300,000 members.
    view = memoryview(data)
    end = 0  # end of the last complete member
    while end < len(data):
        member = zlib.decompressobj(wbits=31)
        fed = end
        try:
            while not member.eof and fed < len(data):
                chunk = view[fed : fed + _CHUNK]
                fed += len(chunk)
                member.decompress(chunk)
        except zlib.error:
            break
        if not member.eof:
            break  # cut off by the range request
        end = fed - len(member.unused_data)
    return data[:end]


def html_pages(data: bytes, path: str) -> Iterator[tuple[str, Any]]:
    """(page URL, the `<link>` elements of its head, untrusted JSON) for each HTML page of a
    (complete) WAT slice; `path` names the file in errors."""
    try:
        with gzip.open(io.BytesIO(data), "rb") as stream:
            for envelope in _metadata_envelopes(stream):
                found = _head_links(envelope)
                if found is not None:  # None: not an HTML page
                    yield found
    except (OSError, EOFError) as exc:
        raise CommonCrawlError(f"{path}: {exc}") from exc


def _crawl_near(collinfo: bytes, target: datetime) -> str:
    """The crawl whose crawl period ended closest to `target`."""
    try:
        crawls = json.loads(collinfo)
        dated = [
            (abs(datetime.fromisoformat(str(c["to"])).replace(tzinfo=UTC) - target), str(c["id"]))
            for c in crawls
            if isinstance(c, dict) and "to" in c and "id" in c
        ]
    except (ValueError, TypeError) as exc:
        raise CommonCrawlError(f"unexpected collinfo.json: {exc!r}") from exc
    if not dated:
        raise CommonCrawlError("collinfo.json lists no dated crawls")
    crawl = min(dated)[1]
    if not _CRAWL_ID.fullmatch(crawl):
        raise CommonCrawlError(f"unexpected crawl id {crawl!r}")
    return crawl


def _wat_paths(paths_gz: bytes, crawl: str) -> list[str]:
    """The WAT file paths listed in a crawl's wat.paths.gz (only well-formed ones)."""
    try:
        text = gzip.decompress(paths_gz).decode("ascii", errors="replace")
    except (OSError, EOFError) as exc:
        raise CommonCrawlError(f"wat.paths.gz: {exc}") from exc
    paths = [
        p for p in text.split() if _WAT_PATH.fullmatch(p) and p.startswith(f"crawl-data/{crawl}/")
    ]
    if not paths:
        raise CommonCrawlError(f"no WAT paths for {crawl}")
    return paths


class _Readable(Protocol):
    def readline(self) -> bytes: ...
    def read(self, size: int = ...) -> bytes: ...


def _metadata_envelopes(stream: _Readable) -> Iterator[dict[str, Any]]:
    """The JSON "Envelope" of each metadata record; malformed ones are skipped."""
    for headers, body in _warc_records(stream):
        if headers.get(b"warc-type") != b"metadata":
            continue
        try:
            envelope = json.loads(body)["Envelope"]
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(envelope, dict):
            yield envelope


def _warc_records(stream: _Readable) -> Iterator[tuple[dict[bytes, bytes], bytes]]:
    """WARC records from a decompressed stream: (headers with lower-case names, body)."""
    while line := stream.readline():
        if not line.startswith(b"WARC/"):
            continue
        headers: dict[bytes, bytes] = {}
        while header := stream.readline().rstrip(b"\r\n"):
            name, _, value = header.partition(b":")
            headers[name.strip().lower()] = value.strip()
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return
        yield headers, stream.read(length)


def _head_links(envelope: dict[str, Any]) -> tuple[str, Any] | None:
    """(page URL, the `<link>` elements of its head) for an HTML page; None for anything else."""
    page = envelope.get("WARC-Header-Metadata", {}).get("WARC-Target-URI")
    response = envelope.get("Payload-Metadata", {}).get("HTTP-Response-Metadata", {})
    if not isinstance(page, str) or "HTML-Metadata" not in response:
        return None
    return page, response["HTML-Metadata"].get("Head", {}).get("Link", []) or []
