"""Feed URLs from the lists named in a sources config (data/sources.toml).

A source is a name, a license and one or more list files: `urls` (usually raw.githubusercontent.com
URLs on a branch) and/or `files` (paths in this repository, relative to the config file, read from
disk; a path may be a glob pattern like `survivors/*/*.yaml`), so each load reads the lists as they
are today: nothing is pinned, and new feeds added upstream arrive on their own. A source that fails
(file gone, malformed) is skipped and reported; the other sources still load. Since the inputs
table is append-only, a broken or truncated upstream file adds nothing harmful and removes nothing.

Files are downloaded with the same guarded fetcher as the health checks and parsed with
defusedxml (OPML), as plain lines, or as proposal files (YAML, read with yaml.safe_load_all: only
feeds marked `accept: true` count).

collect_inputs() turns the downloaded lists into rows for the `inputs` table: every valid URL
(the same scheme, credential and port rules as a check), minus excluded domains, each credited
to the most permissive source it was found in (by the [licenses] ranking; on a tie, the source
listed first in the config).
"""

import html
import re
import tomllib
import urllib.parse
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from xml.etree.ElementTree import ParseError

import yaml
from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from indexer.net.urls import drop_excluded, valid_urls

type Fetch = Callable[[str], Awaitable[bytes]]
_XML_URL_ATTR = re.compile(rb"""\bxmlUrl\s*=\s*(?:"([^"<]*)"|'([^'<]*)')""")
Format = Literal["opml", "lines", "proposals"]
FORMATS: tuple[Format, ...] = ("opml", "lines", "proposals")


# (source, where the list was found, feed URLs). "Where" is the file's URL, or for a file in
# this repository the source's homepage (else its path).
type Downloaded = list[tuple[Source, str, list[str]]]


class SourceError(Exception):
    """The config is invalid, or a source file could not be fetched or parsed."""


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    license: str  # SPDX id, as stated in the config
    format: Format
    urls: tuple[str, ...]  # list files fetched over HTTPS at load time
    files: tuple[str, ...] = ()  # list files or glob patterns, relative to the config file
    homepage: str = ""
    notes: str = ""


@dataclass(frozen=True, slots=True)
class SourcesConfig:
    sources: tuple[Source, ...]
    # SPDX id -> tier; a lower tier is more permissive. Every source's license is listed.
    license_tiers: Mapping[str, int] = field(default_factory=dict)
    base_dir: Path = Path()  # the config file's directory; `files` are relative to it
    # Registrable domains whose feeds are never loaded (e.g. youtube.com, reddit.com).
    exclude_domains: frozenset[str] = frozenset()
    # [commoncrawl] exclude_keywords: whole words (lower case) that keep proposals from proposing
    # a feed with them in its title. Common Crawl only; the lists are not filtered.
    commoncrawl_exclude_keywords: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class InputRow:
    url: str
    source: str
    source_license: str


@dataclass(slots=True)
class InputsResult:
    rows: list[InputRow]
    found: Counter[str] = field(default_factory=Counter)  # valid unique URLs per source
    invalid: Counter[str] = field(default_factory=Counter)
    excluded: Counter[str] = field(default_factory=Counter)  # dropped by [exclude] domains
    attributed: Counter[str] = field(default_factory=Counter)  # rows credited to each source


def load_config(path: Path) -> SourcesConfig:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SourceError(f"{path}: {exc}") from exc
    sources: list[Source] = []
    for i, raw in enumerate(data.get("source") or []):
        where = f"[[source]] #{i + 1}"
        name = _require(raw, "name", str, where)
        where = f"source '{name}'"
        fmt = _require(raw, "format", str, where)
        if fmt not in FORMATS:
            raise SourceError(f"{where}: format must be one of {', '.join(FORMATS)}")
        urls = raw.get("urls", [])
        files = raw.get("files", [])
        for key, value in (("urls", urls), ("files", files)):
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise SourceError(f"{where}: '{key}' must be a list of strings")
        if not urls and not files:
            raise SourceError(f"{where}: needs 'urls' and/or 'files'")
        for f in files:
            resolved = (path.parent / f).resolve()
            if Path(f).is_absolute() or not resolved.is_relative_to(path.parent.resolve()):
                raise SourceError(f"{where}: files must stay inside {path.parent}: {f}")
        for u in urls:
            if urllib.parse.urlsplit(u).scheme != "https":
                raise SourceError(f"{where}: source URLs must be https: {u}")
        sources.append(
            Source(
                name=name,
                license=_require(raw, "license", str, where),
                format=fmt,
                urls=tuple(urls),
                files=tuple(files),
                homepage=str(raw.get("homepage", "")),
                notes=str(raw.get("notes", "")),
            )
        )
    if not sources:
        raise SourceError(f"{path}: no [[source]] entries")
    if len({s.name for s in sources}) != len(sources):
        raise SourceError(f"{path}: source names must be unique")
    tiers = _license_tiers(data.get("licenses"), path)
    exclude = data.get("exclude", {})
    domains = exclude.get("domains", []) if isinstance(exclude, dict) else None
    if not isinstance(domains, list) or not all(isinstance(d, str) and d for d in domains):
        raise SourceError(f"{path}: [exclude] domains must be a list of domain names")
    commoncrawl = data.get("commoncrawl", {})
    keywords = commoncrawl.get("exclude_keywords", []) if isinstance(commoncrawl, dict) else None
    if not isinstance(keywords, list) or not all(
        isinstance(k, str) and k.strip() for k in keywords
    ):
        raise SourceError(f"{path}: [commoncrawl] exclude_keywords must be a list of words")
    unranked = sorted({s.license for s in sources} - tiers.keys())
    if unranked:
        raise SourceError(f"{path}: licenses missing from [licenses] ranking: {unranked}")
    return SourcesConfig(
        tuple(sources),
        tiers,
        path.parent,
        frozenset(d.strip().lower().rstrip(".") for d in domains),
        frozenset(k.strip().lower() for k in keywords),
    )


def extract_urls(body: bytes, fmt: Format) -> list[str]:
    """Feed URLs from one file: OPML xmlUrl attributes, one URL per line, or the accepted feeds
    of a proposal file."""
    if fmt == "proposals":
        return [str(d["url"]) for d in proposal_documents(body) if d.get("accept") is True]
    if fmt == "opml":
        try:
            root = SafeET.fromstring(body, forbid_dtd=True)
        except DefusedXmlException as exc:
            raise SourceError(f"forbidden XML in OPML: {type(exc).__name__}") from exc
        except ParseError:
            # Hand-edited OPML often has a bare '&' in a URL. Fall back to reading the
            # xmlUrl attributes directly; entities are only the predefined ones.
            return _scan_xml_urls(body)
        return [u.strip() for el in root.iter("outline") if (u := el.get("xmlUrl"))]
    text = body.decode("utf-8", errors="replace")
    return [s for line in text.splitlines() if (s := line.strip()) and not s.startswith("#")]


def proposal_documents(body: bytes) -> list[dict[str, Any]]:
    """The feeds of a proposal file (data/survivors/, written by proposals/files.py): one
    YAML document per feed, each a mapping with at least a `url`. Read with safe_load_all, which
    builds only plain values (no tags, no objects). SourceError if it isn't such a file."""
    try:
        documents = list(yaml.safe_load_all(body.decode("utf-8", errors="replace")))
    except yaml.YAMLError as exc:
        raise SourceError(f"malformed proposal file: {exc}") from exc
    feeds = [d for d in documents if d is not None]  # a comment-only header is no document
    for d in feeds:
        if not isinstance(d, dict) or not isinstance(d.get("url"), str):
            raise SourceError("malformed proposal file: every feed needs a url")
    return feeds


async def download(config: SourcesConfig, fetch: Fetch) -> tuple[Downloaded, dict[str, str]]:
    """Fetch every source file and extract its URLs (not yet validated). A failing source is
    reported in the returned failures and does not stop the others."""
    out: Downloaded = []
    failures: dict[str, str] = {}
    for source in config.sources:
        files: Downloaded = []
        try:
            for url in source.urls:
                body = await fetch(url)
                try:
                    files.append((source, url, extract_urls(body, source.format)))
                except SourceError as exc:
                    raise SourceError(f"{url}: {exc}") from exc
            for name in _expand(config.base_dir, source.files):
                try:
                    body = (config.base_dir / name).read_bytes()
                except OSError as exc:
                    raise SourceError(f"{name}: {exc.strerror or exc}") from exc
                try:
                    found = extract_urls(body, source.format)
                except SourceError as exc:
                    raise SourceError(f"{name}: {exc}") from exc
                files.append((source, source.homepage or name, found))
        except SourceError as exc:
            failures[source.name] = str(exc)
            continue
        out.extend(files)
    return out, failures


def collect_inputs(config: SourcesConfig, downloaded: Downloaded) -> InputsResult:
    """One row per valid unique URL, credited to its most permissive source."""
    best: dict[str, InputRow] = {}
    best_tier: dict[str, int] = {}
    credited: dict[str, str] = {}  # feed URL -> source name
    seen: dict[str, set[str]] = {}
    result = InputsResult(rows=[])
    for source, file_url, urls in downloaded:
        tier = config.license_tiers[source.license]
        valid, invalid = valid_urls(urls)
        valid, excluded = drop_excluded(valid, config.exclude_domains)
        result.invalid[source.name] += invalid
        result.excluded[source.name] += excluded
        unique = seen.setdefault(source.name, set())
        for canonical, _host in valid:
            unique.add(canonical)
            if canonical not in best or tier < best_tier[canonical]:
                best[canonical] = InputRow(canonical, file_url, source.license)
                best_tier[canonical] = tier
                credited[canonical] = source.name
    for name, unique in seen.items():
        result.found[name] = len(unique)
    for name in credited.values():
        result.attributed[name] += 1
    result.rows = list(best.values())
    return result


def _require(table: dict[str, Any], key: str, kind: type, where: str) -> Any:
    value = table.get(key)
    if not isinstance(value, kind) or isinstance(value, bool) is not (kind is bool):
        raise SourceError(f"{where}: '{key}' must be a {kind.__name__}")
    return value


def _license_tiers(table: object, path: Path) -> dict[str, int]:
    """[licenses] ranking = [["CC0-1.0", ...], ["MIT", ...], ...], most permissive first."""
    ranking = table.get("ranking") if isinstance(table, dict) else None
    if not isinstance(ranking, list) or not ranking:
        raise SourceError(f"{path}: [licenses] needs a non-empty 'ranking' list of lists")
    tiers: dict[str, int] = {}
    for tier, group in enumerate(ranking):
        if not isinstance(group, list) or not all(isinstance(x, str) for x in group):
            raise SourceError(f"{path}: [licenses] ranking entries must be lists of strings")
        for spdx in group:
            if spdx in tiers:
                raise SourceError(f"{path}: license {spdx} is ranked twice")
            tiers[spdx] = tier
    return tiers


def _scan_xml_urls(body: bytes) -> list[str]:
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise SourceError("malformed OPML with a DTD; refusing to scan it")
    urls = []
    for match in _XML_URL_ATTR.finditer(body):
        raw = (match.group(1) or match.group(2) or b"").decode("utf-8", errors="replace")
        if url := html.unescape(raw).strip():
            urls.append(url)
    return urls


def _expand(base_dir: Path, files: tuple[str, ...]) -> list[str]:
    """The files to read, relative to `base_dir`: a plain path as is (missing: an error when
    read), a glob pattern as the files it matches today, sorted (none is fine). A match that
    resolves outside `base_dir` (a symlink) is an error."""
    names: list[str] = []
    for name in files:
        if not any(c in name for c in "*?["):
            names.append(name)
            continue
        for match in sorted(base_dir.glob(name)):
            if not match.resolve().is_relative_to(base_dir.resolve()):
                raise SourceError(f"{name}: {match} is outside {base_dir}")
            if match.is_file():
                names.append(str(match.relative_to(base_dir)))
    return names
