"""Decide whether a body is an RSS/Atom feed and extract item count, newest date, ttl, the
generator, the URLs the feed gives for itself (site, self, new-feed-url) and descriptive fields
for labelling (title, subtitle, media type, icon, logo, rights, license), and the text of
the first items (for language detection; not stored).

Parsing uses defusedxml: entity declarations and external references are refused, and DTDs
are never loaded. A bare <!DOCTYPE> (common in RSS 0.91) is tolerated because nothing is
fetched or expanded from it. The Content-Type header is ignored; only the parse result counts.
"""

import dataclasses
import email.utils
import html
import re
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NamedTuple, TypedDict
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from indexer.errors import CheckError, ErrorCode

ATOM = "{http://www.w3.org/2005/Atom}"
ATOM03 = "{http://purl.org/atom/ns#}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
RSS10 = "{http://purl.org/rss/1.0/}"
RSS090 = "{http://my.netscape.com/rdf/simple/0.9/}"
DC = "{http://purl.org/dc/elements/1.1/}"
DCTERMS = "{http://purl.org/dc/terms/}"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
ADMIN = "{http://webns.net/mvcb/}"
MEDIA = "{http://search.yahoo.com/mrss/}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
CC = "{http://backend.userland.com/creativeCommonsRssModule}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

MAX_URL_LEN = 2048
MAX_TEXT_LEN = 200
MAX_SUBTITLE_LEN = 500
MAX_TEXT_ITEMS = 30  # items whose text is kept for language detection (first in the feed)
MAX_ITEM_TEXT_LEN = 300  # title and the start of the description, per item
MAX_ITEM_TAGS = 50  # categories kept per item (topics ignore an item with more than a few)
MAX_TAG_LEN = 100
MAX_RECENT_POSTS = 3  # the most recent items, for reviewing proposals (not stored)
MAX_POST_LINK_LEN = 600
MAX_POST_DESCRIPTION_LEN = 1000
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac")
VIDEO_EXTENSIONS = (".mp4", ".m4v", ".webm", ".mov", ".mkv")
_TAG = re.compile(r"<[^>]*>")
# Elements whose content is code, not text: dropped whole (to the end if never closed).
_CODE = re.compile(r"<(script|style|noscript|template)\b.*?(</\1\s*>|$)", re.IGNORECASE | re.DOTALL)

MAX_INT32 = 2**31 - 1


# Where each format keeps an item's date.
_RSS_DATES = ("pubDate", f"{DC}date", f"{ATOM}updated", f"{ATOM}published")
_ATOM_DATES = (f"{ATOM}updated", f"{ATOM}published")
_ATOM03_DATES = (f"{ATOM03}modified", f"{ATOM03}issued", f"{ATOM03}created")
_RDF_DATES = (f"{DC}date", f"{DCTERMS}modified", f"{DCTERMS}issued")


_DECLARED_ENCODING = re.compile(
    rb"""^(?:\xef\xbb\xbf)?\s*<\?xml\s[^>]*?\bencoding\s*=\s*["']([A-Za-z0-9._:-]{1,40})["']"""
)


class RecentPost(NamedTuple):
    """One of a feed's most recent items, shown to reviewers of proposals. Untrusted text: the
    link is as written in the feed (not resolved or validated) and never fetched."""

    link: str
    title: str | None
    description: str | None
    date: str | None = None  # the item's newest date (published or updated), YYYY-MM-DD in UTC


@dataclass(frozen=True, slots=True)
class FeedInfo:
    kind: str  # rss, rdf, atom, atom03
    item_count: int
    last_item_date: datetime | None
    ttl: int | None
    generator: str | None = None
    site_url: str | None = None  # the website the feed belongs to
    self_url: str | None = None  # where the feed says it lives (atom:link rel=self)
    new_feed_url: str | None = None  # <itunes:new-feed-url>: the feed has moved
    # Descriptive fields, named after their Atom counterparts.
    title: str | None = None
    subtitle: str | None = None  # Atom <subtitle>, RSS <description>
    # The MAX_RECENT_POSTS most recent items with a link, newest first, distinct by link.
    recent_posts: tuple[RecentPost, ...] = ()
    media_type: str | None = None  # text, podcast or video; None for an empty feed
    icon: str | None = None
    logo: str | None = None  # Atom <logo>, RSS <image><url>, <itunes:image>
    rights: str | None = None  # Atom <rights>, RSS <copyright>, dc:rights
    license: str | None = None  # link rel=license, creativeCommons:license
    # Plain text (title and the start of the description) of the first MAX_TEXT_ITEMS items, for
    # language detection: untrusted, never stored.
    item_texts: tuple[str, ...] = ()
    # The categories of each of item_texts' items, as written (split on commas, trimmed, capped):
    # untrusted, never stored; only used as evidence for topics.
    item_tags: tuple[tuple[str, ...], ...] = ()


def clean_url(value: str | None, base: str | None = None) -> str | None:
    """An absolute http(s) URL that is safe to store, or None. Nothing here is fetched."""
    if not value or not (value := value.strip()) or len(value) > MAX_URL_LEN:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return None
    try:
        joined = urllib.parse.urljoin(base, value) if base else value
        scheme = urllib.parse.urlsplit(joined).scheme.lower()
    except ValueError:
        return None
    if scheme not in ("http", "https") or len(joined) > MAX_URL_LEN:
        return None
    return joined


def parse_date(text: str | None) -> datetime | None:
    """RFC 822 (RSS) or ISO 8601 (Atom, Dublin Core). Naive times are taken as UTC."""
    if not text or not (value := text.strip()):
        return None
    parsers = (_iso, _rfc822) if value[:4].isdigit() else (_rfc822, _iso)
    for parser in parsers:
        try:
            dt = parser(value)
        except (ValueError, TypeError, IndexError, OverflowError):
            continue
        if dt is not None:
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return None


def parse_feed(body: bytes, base_url: str | None = None) -> FeedInfo:
    """Raise CheckError unless body is a feed. base_url resolves relative links."""
    if not _looks_like_markup(body):
        raise CheckError(ErrorCode.NOT_A_FEED, "body is not XML")
    try:
        try:
            root = _parse_xml(body)
        except DefusedXmlException:
            raise  # a ValueError too: handled below, never retried
        except (ValueError, LookupError):
            # Expat refuses a declared multi-byte or unknown encoding: re-encode and retry.
            root = _parse_xml(_as_utf8(body))
    except DefusedXmlException as exc:
        raise CheckError(ErrorCode.PARSE_ERROR, f"forbidden XML: {type(exc).__name__}") from exc
    except ParseError as exc:
        if _looks_like_html(body):
            raise CheckError(ErrorCode.NOT_A_FEED, "HTML document") from exc
        raise CheckError(ErrorCode.PARSE_ERROR, str(exc)) from exc

    info = _parse_root(root, base_url)
    return dataclasses.replace(info, **_describe(root, info.kind, base_url))


class Description(TypedDict):
    title: str | None
    subtitle: str | None
    recent_posts: tuple[RecentPost, ...]
    item_texts: tuple[str, ...]
    item_tags: tuple[tuple[str, ...], ...]
    media_type: str | None
    icon: str | None
    logo: str | None
    rights: str | None
    license: str | None


def _clean_text(value: str | None, limit: int = MAX_TEXT_LEN) -> str | None:
    """Printable text on one line, at most `limit` characters."""
    if not value:
        return None
    text = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    return text[:limit] or None


def _atom_links(parent: Element, ns: str, base: str | None) -> tuple[str | None, str | None]:
    """(site_url, self_url) from <link> children; rel defaults to alternate."""
    site = self_ = None
    for link in parent.findall(f"{ns}link"):
        rel = (link.get("rel") or "alternate").strip().lower()
        href = clean_url(link.get("href"), base)
        if href is None:
            continue
        if rel == "self" and self_ is None:
            self_ = href
        elif rel == "alternate" and site is None:
            site = href
    return site, self_


def _atom_generator(root: Element, ns: str) -> str | None:
    gen = root.find(f"{ns}generator")
    if gen is None:
        return None
    text = (gen.text or "").strip()
    version = (gen.get("version") or "").strip()
    if version and version not in text:
        text = f"{text} {version}"
    return _clean_text(text)


def _rfc822(value: str) -> datetime | None:
    return email.utils.parsedate_to_datetime(value)


def _iso(value: str) -> datetime | None:
    return datetime.fromisoformat(value)


def _newest(items: Iterable[Element], tags: tuple[str, ...]) -> datetime | None:
    newest: datetime | None = None
    for item in items:
        for tag in tags:
            dt = parse_date(item.findtext(tag))
            if dt is not None and (newest is None or dt > newest):
                newest = dt
    return newest


def _ttl(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        value = int(text.strip())
    except ValueError:
        return None
    return value if 0 <= value <= MAX_INT32 else None


def _looks_like_markup(body: bytes) -> bool:
    head = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b"<")


def _looks_like_html(body: bytes) -> bool:
    head = body.lstrip(b"\xef\xbb\xbf \t\r\n")[:1024].lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head


def _parse_xml(body: bytes) -> Element:
    return SafeET.fromstring(body, forbid_dtd=False, forbid_entities=True, forbid_external=True)


def _as_utf8(body: bytes) -> bytes:
    """The body re-encoded as UTF-8, for an encoding expat can't read itself (Shift_JIS, EUC-JP,
    GB2312, Big5, EUC-KR, ...: "multi-byte encodings are not supported"). Decoded with Python's
    codecs from just after the declared name, which is ASCII in all of them; the declaration then
    says UTF-8. Undecodable bytes become U+FFFD. The result is parsed with the same protections."""
    match = _DECLARED_ENCODING.match(body[:512])
    if match is None:
        raise CheckError(ErrorCode.PARSE_ERROR, "unsupported encoding")
    name = match.group(1).decode("ascii")
    try:
        rest = body[match.end(1) :].decode(name, errors="replace")
    except LookupError as exc:
        raise CheckError(ErrorCode.PARSE_ERROR, f"unknown encoding {name[:40]}") from exc
    return body[: match.start(1)] + b"UTF-8" + rest.encode("utf-8")


def _parse_root(root: Element, base_url: str | None) -> FeedInfo:
    tag = root.tag
    if tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise CheckError(ErrorCode.NOT_A_FEED, "<rss> without <channel>")
        items = channel.findall("item") or root.findall("item")
        date_tags: tuple[str, ...] = _RSS_DATES
        _site, self_url = _atom_links(channel, ATOM, base_url)
        return FeedInfo(
            "rss",
            len(items),
            _newest(items, date_tags),
            _ttl(channel.findtext("ttl")),
            generator=_clean_text(channel.findtext("generator")),
            site_url=clean_url(channel.findtext("link"), base_url),
            self_url=self_url,
            new_feed_url=clean_url(channel.findtext(f"{ITUNES}new-feed-url"), base_url),
        )
    if tag in (f"{ATOM}feed", f"{ATOM03}feed"):
        ns = ATOM if tag == f"{ATOM}feed" else ATOM03
        entries = root.findall(f"{ns}entry")
        date_tags = _ATOM_DATES if ns == ATOM else _ATOM03_DATES
        site, self_url = _atom_links(root, ns, base_url)
        return FeedInfo(
            "atom" if ns == ATOM else "atom03",
            len(entries),
            _newest(entries, date_tags),
            None,
            generator=_atom_generator(root, ns),
            site_url=site,
            self_url=self_url,
        )
    if tag == f"{RDF}RDF":
        ns = RSS10
        channel = root.find(f"{RSS10}channel")
        if channel is None:
            ns, channel = RSS090, root.find(f"{RSS090}channel")
        if channel is None:
            raise CheckError(ErrorCode.NOT_A_FEED, "RDF document without an RSS channel")
        items = root.findall(f"{RSS10}item") + root.findall(f"{RSS090}item")
        date_tags = _RDF_DATES
        agent = channel.find(f"{ADMIN}generatorAgent")
        return FeedInfo(
            "rdf",
            len(items),
            _newest(items, date_tags),
            None,
            generator=_clean_text(agent.get(f"{RDF}resource")) if agent is not None else None,
            site_url=clean_url(channel.findtext(f"{ns}link"), base_url),
        )
    raise CheckError(ErrorCode.NOT_A_FEED, f"root element {tag[:100]}")


def _text_of(el: Element | None, *, markup: bool = False, limit: int = MAX_TEXT_LEN) -> str | None:
    """Plain text of an element. markup=True strips tags from escaped HTML (RSS descriptions,
    Atom type="html"), and the contents of script, style, noscript and template elements;
    Atom type="xhtml" content is child elements, so itertext covers it."""
    if el is None:
        return None
    text = "".join(el.itertext())
    if markup or el.get("type") == "html":
        text = html.unescape(_TAG.sub(" ", _CODE.sub(" ", text)))
    return _clean_text(text, limit)


def _first(*values: str | None) -> str | None:
    return next((v for v in values if v), None)


def _item_media(item: Element) -> str | None:
    """'video', 'audio' or None, from enclosures, Media RSS and YouTube elements."""
    found: set[str] = set()
    if item.find(f"{YT}videoId") is not None:
        return "video"
    candidates: list[tuple[str, str]] = []
    for enc in item.findall("enclosure"):
        candidates.append((enc.get("type") or "", enc.get("url") or ""))
    for ns in (ATOM, ATOM03):
        for link in item.findall(f"{ns}link"):
            if (link.get("rel") or "").strip().lower() == "enclosure":
                candidates.append((link.get("type") or "", link.get("href") or ""))
    for media in item.iter(f"{MEDIA}content"):
        medium = (media.get("medium") or "").lower()
        if medium in ("audio", "video"):
            found.add(medium)
        candidates.append((media.get("type") or "", media.get("url") or ""))
    for mime, href in candidates:
        major = mime.split("/")[0].strip().lower()
        try:
            path = urllib.parse.urlsplit(href.strip()).path.lower()
        except ValueError:
            path = ""
        if major == "video" or path.endswith(VIDEO_EXTENSIONS):
            found.add("video")
        elif major == "audio" or path.endswith(AUDIO_EXTENSIONS):
            found.add("audio")
    if "video" in found:
        return "video"
    return "audio" if "audio" in found else None


def _media_type(items: list[Element]) -> str | None:
    if not items:
        return None
    kinds = [_item_media(item) for item in items]
    if 2 * kinds.count("video") >= len(items):
        return "video"
    if 2 * kinds.count("audio") >= len(items):
        return "podcast"
    return "text"


def _rel_link(parent: Element, ns: str, rel: str, base: str | None) -> str | None:
    for link in parent.findall(f"{ns}link"):
        if (link.get("rel") or "").strip().lower() == rel:
            href = clean_url(link.get("href"), base)
            if href:
                return href
    return None


def _recent_posts(
    items: Iterable[Element],
    ns: str,
    *,
    atom: bool,
    date_tags: tuple[str, ...],
    title_tag: str,
    description_tags: tuple[str, ...],
) -> tuple[RecentPost, ...]:
    """The MAX_RECENT_POSTS most recent items with a link (undated items after dated ones, in
    feed order), distinct by link: the link as written (RSS/RDF <link> text, Atom <link
    rel=alternate href>, only printable characters, length capped), the title, the start of the
    first non-empty description, and the item's newest date (a day, UTC; None if undated)."""
    dated: list[tuple[float, int, str, Element, datetime | None]] = []
    for position, item in enumerate(items):
        if atom:
            raw = next(
                (
                    link.get("href")
                    for link in item.findall(f"{ns}link")
                    if (link.get("rel") or "alternate").strip().lower() == "alternate"
                ),
                None,
            )
        else:
            raw = item.findtext(f"{ns}link")
        link = "".join(c for c in (raw or "") if c.isprintable()).strip()[:MAX_POST_LINK_LEN]
        if link:
            dates = [d for tag in date_tags if (d := parse_date(item.findtext(tag)))]
            when = max(dates) if dates else None
            dated.append((-when.timestamp() if when else float("inf"), position, link, item, when))
    posts: dict[str, RecentPost] = {}
    for _newest, _position, link, item, when in sorted(dated, key=lambda d: (d[0], d[1])):
        if link in posts:
            continue
        description = next(
            (
                text
                for tag in description_tags
                if (text := _text_of(item.find(tag), markup=True, limit=MAX_POST_DESCRIPTION_LEN))
            ),
            None,
        )
        day = when.astimezone(UTC).date().isoformat() if when else None
        posts[link] = RecentPost(
            link, _text_of(item.find(title_tag), markup=True), description, day
        )
        if len(posts) == MAX_RECENT_POSTS:
            break
    return tuple(posts.values())


def _item_tags(item: Element) -> tuple[str, ...]:
    """The item's categories (RSS <category>, Atom <category term|label>, dc:subject), each value
    split on commas, trimmed, capped at MAX_TAG_LEN; at most MAX_ITEM_TAGS, in order, distinct."""
    tags: dict[str, None] = {}
    for child in item:
        if child.tag in ("category", f"{DC}subject"):
            value = child.text
        elif child.tag in (f"{ATOM}category", f"{ATOM03}category"):
            value = child.get("term") or child.get("label") or child.text
        else:
            continue
        for part in (value or "").split(","):
            if tag := _clean_text(part):
                tags.setdefault(tag[:MAX_TAG_LEN])
    return tuple(tags)[:MAX_ITEM_TAGS]


class _ItemText(TypedDict):
    item_texts: tuple[str, ...]
    item_tags: tuple[tuple[str, ...], ...]


def _item_texts(items: list[Element], title: str, descriptions: tuple[str, ...]) -> _ItemText:
    """Title and the start of the first non-empty description of each of the first MAX_TEXT_ITEMS
    items, as plain text (markup stripped), MAX_ITEM_TEXT_LEN characters at most, with each one's
    categories (`_item_tags`)."""
    texts: list[str] = []
    tags: list[tuple[str, ...]] = []
    for item in items[:MAX_TEXT_ITEMS]:
        head = _text_of(item.find(title), markup=True, limit=MAX_ITEM_TEXT_LEN) or ""
        body = next(
            (
                t
                for tag in descriptions
                if (t := _text_of(item.find(tag), markup=True, limit=MAX_ITEM_TEXT_LEN))
            ),
            "",
        )
        text = f"{head}. {body}" if head and body else head or body
        if text:
            texts.append(text[:MAX_ITEM_TEXT_LEN])
            tags.append(_item_tags(item))
    return {"item_texts": tuple(texts), "item_tags": tuple(tags)}


def _describe(root: Element, kind: str, base: str | None) -> Description:
    """Descriptive fields; every value is cleaned, length-capped text or a cleaned URL."""
    if kind == "rss":
        channel = root.find("channel")
        assert channel is not None
        items = channel.findall("item") or root.findall("item")
        image = channel.find(f"{ITUNES}image")
        return {
            "title": _text_of(channel.find("title"), markup=True),
            "subtitle": _text_of(channel.find("description"), markup=True, limit=MAX_SUBTITLE_LEN),
            "recent_posts": _recent_posts(
                items,
                "",
                atom=False,
                date_tags=_RSS_DATES,
                title_tag="title",
                description_tags=("description", f"{CONTENT}encoded"),
            ),
            **_item_texts(items, "title", ("description", f"{CONTENT}encoded")),
            "media_type": _media_type(items),
            "icon": clean_url(channel.findtext(f"{ATOM}icon"), base),
            "logo": _first(
                clean_url(channel.findtext("image/url"), base),
                clean_url(image.get("href"), base) if image is not None else None,
            ),
            "rights": _first(
                _text_of(channel.find("copyright")), _text_of(channel.find(f"{DC}rights"))
            ),
            "license": _first(
                clean_url(channel.findtext(f"{CC}license"), base),
                _rel_link(channel, ATOM, "license", base),
            ),
        }
    if kind in ("atom", "atom03"):
        ns = ATOM if kind == "atom" else ATOM03
        entries = root.findall(f"{ns}entry")
        return {
            "title": _text_of(root.find(f"{ns}title")),
            "subtitle": _text_of(
                root.find(f"{ns}subtitle") if ns == ATOM else root.find(f"{ns}tagline"),
                limit=MAX_SUBTITLE_LEN,
            ),
            "recent_posts": _recent_posts(
                entries,
                ns,
                atom=True,
                date_tags=_ATOM_DATES if ns == ATOM else _ATOM03_DATES,
                title_tag=f"{ns}title",
                description_tags=(f"{ns}summary", f"{ns}content"),
            ),
            **_item_texts(entries, f"{ns}title", (f"{ns}summary", f"{ns}content")),
            "media_type": _media_type(entries),
            "icon": clean_url(root.findtext(f"{ns}icon"), base),
            "logo": clean_url(root.findtext(f"{ns}logo"), base),
            "rights": _text_of(
                root.find(f"{ns}rights") if ns == ATOM else root.find(f"{ns}copyright")
            ),
            "license": _rel_link(root, ns, "license", base),
        }
    # rdf
    ns = RSS10 if root.find(f"{RSS10}channel") is not None else RSS090
    channel = root.find(f"{ns}channel")
    assert channel is not None
    items = root.findall(f"{RSS10}item") + root.findall(f"{RSS090}item")
    recent_posts = _recent_posts(
        items,
        ns,
        atom=False,
        date_tags=_RDF_DATES,
        title_tag=f"{ns}title",
        description_tags=(f"{ns}description",),
    )
    return {
        "title": _text_of(channel.find(f"{ns}title"), markup=True),
        "subtitle": _text_of(channel.find(f"{ns}description"), markup=True, limit=MAX_SUBTITLE_LEN),
        "recent_posts": recent_posts,
        **_item_texts(items, f"{ns}title", (f"{ns}description",)),
        "media_type": _media_type(items),
        "icon": None,
        "logo": clean_url(root.findtext(f"{ns}image/{ns}url"), base),
        "rights": _text_of(channel.find(f"{DC}rights")),
        "license": clean_url(channel.findtext(f"{CC}license"), base),
    }
