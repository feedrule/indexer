"""The links in a post's HTML, for reviewers of proposals: which other sites it links to, and
whether it carries affiliate or sponsored links. Kept in memory only (never stored), and no
link is ever fetched: hrefs are only parsed and matched.

A link counts as sponsored on strict evidence only (generic `aff=` parameters and `/go/` paths
gave false matches in a test on reviewed proposals, 2026-11-28): rel="sponsored", an affiliate
network's domain, an Amazon link with a `tag=` parameter; a post also counts as sponsored when
its text discloses affiliate links ("As an Amazon Associate ...").
"""

import re
import urllib.parse
from collections import Counter
from collections.abc import Iterable
from html.parser import HTMLParser
from typing import NamedTuple

from indexer.net.domains import registrable_domain

MAX_LINKED_SITES = 5  # per post, most linked first
MAX_LINKS = 500  # <a> elements read per post
MAX_HTML_LEN = 200_000  # characters of a post's HTML read

# Affiliate networks and their link domains (matched as the host or a parent of it).
AFFILIATE_DOMAINS = frozenset(
    {
        "amzn.to",
        "anrdoezrs.net",  # CJ
        "avantlink.com",
        "awin1.com",
        "clickbank.net",
        "dpbolvw.net",  # CJ
        "geni.us",
        "go2cloud.org",
        "howl.me",
        "jdoqocy.com",  # CJ
        "kqzyfj.com",  # CJ
        "linksynergy.com",  # Rakuten
        "liketk.it",
        "prf.hn",  # Partnerize
        "pxf.io",  # impact
        "rstyle.me",
        "shareasale.com",
        "shopstyle.com",
        "sjv.io",  # impact
        "skimresources.com",
        "tkqlhce.com",  # CJ
    }
)
_DISCLOSURE = re.compile(
    r"amazon associate|affiliate links?\b|affiliate commission|contains affiliate"
    r"|(earn|receive) (a small |a )?commission",
    re.IGNORECASE,
)


class PostLinks(NamedTuple):
    sites: tuple[tuple[str, int], ...] = ()  # other sites' registrable domains, with link counts
    sponsored: bool = False


def post_links(
    fragments: Iterable[str],
    base: str | None,
    anchors: Iterable[tuple[str, str]] = (),
    text: str = "",
) -> PostLinks:
    """The links of a post: those of its HTML fragments (escaped HTML, as in an RSS description)
    plus `anchors`, (href, rel) pairs already parsed (Atom XHTML content), with `text`. Relative
    links are resolved against `base` (the post's link); links to its own site are left out."""
    parser = _Anchors()
    for fragment in fragments:
        try:
            parser.feed(fragment[:MAX_HTML_LEN])
            parser.close()
        except (AssertionError, ValueError):  # malformed markup: keep what was read
            pass
        parser.reset()
    return links_of([*parser.anchors, *anchors], f"{parser.text} {text}", base)


def links_of(anchors: Iterable[tuple[str, str]], text: str, base: str | None) -> PostLinks:
    """`anchors`: (href, rel) pairs; `text`: the post's text, for a disclosure."""
    own = _domain(base)
    sites: Counter[str] = Counter()
    sponsored = bool(_DISCLOSURE.search(text))
    for n, (href, rel) in enumerate(anchors):
        if n == MAX_LINKS:
            break
        url = _absolute(href, base)
        if url is None:
            continue
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").removeprefix("www.")
        sponsored = sponsored or "sponsored" in rel.lower().split() or _affiliate(host, parts)
        domain = registrable_domain(host)
        if domain and domain != own:
            sites[domain] += 1
    top = sorted(sites.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_LINKED_SITES]
    return PostLinks(tuple(top), sponsored)


def _affiliate(host: str, parts: urllib.parse.SplitResult) -> bool:
    labels = host.split(".")
    if any(".".join(labels[i:]) in AFFILIATE_DOMAINS for i in range(len(labels) - 1)):
        return True
    if "amazon" in labels[:-1]:  # amazon.com, amazon.co.uk, smile.amazon.de
        query = urllib.parse.parse_qs(parts.query)
        return "tag" in query
    return False


def _absolute(href: str, base: str | None) -> str | None:
    href = href.strip()
    if not href or any(ord(c) < 0x20 for c in href):
        return None
    try:
        url = urllib.parse.urljoin(base, href) if base else href
        scheme = urllib.parse.urlsplit(url).scheme.lower()
    except ValueError:
        return None
    return url if scheme in ("http", "https") else None


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None
    return registrable_domain(host)


class _Anchors(HTMLParser):
    """Collects (href, rel) of <a> elements and the text (for a disclosure); nothing is loaded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._text: list[str] = []
        self._length = 0

    @property
    def text(self) -> str:
        return " ".join(self._text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and len(self.anchors) < MAX_LINKS:
            values = dict(attrs)
            if href := values.get("href"):
                self.anchors.append((href, values.get("rel") or ""))

    def handle_data(self, data: str) -> None:
        if self._length < MAX_HTML_LEN:
            self._text.append(data)
            self._length += len(data)
