"""Offline filtering of the feed links a crawled page advertises: URL rules, nothing fetched."""

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from indexer.net.urls import drop_excluded, strip_session, valid_urls
from indexer.proposals.wat import html_pages

FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml", "application/rdf+xml"})

MAX_LENGTH = 256  # characters in the URL
MAX_SLASHES = 7  # in the path
MAX_SEGMENT = 25  # characters in the longest path segment
# Top-level domains Freenom gave away for free: mostly spam (agreed 2026-11-21).
FREE_TLDS = frozenset({"tk", "ml", "ga", "cf", "gq"})
# WordPress serves its feed in several formats; /feed/ is the one we keep.
_WORDPRESS_FORMAT = re.compile(r"/feed/(atom|rss|rss2|rdf)/?$", re.IGNORECASE)


@dataclass(slots=True)
class WatScan:
    """What one WAT slice held."""

    path: str  # the WAT file, relative to wat.DATA_URL
    pages: int = 0  # HTML pages
    pages_with_feed: int = 0  # of those, pages advertising at least one feed
    feed_links: int = 0  # feed links on those pages
    # per page with at least one: its valid feed links outside the excluded domains
    page_feeds: list[list[str]] = field(default_factory=list)


def scan_wat(data: bytes, path: str, exclude: frozenset[str] = frozenset()) -> WatScan:
    """The feed URLs worth checking on each page of a (complete) WAT slice."""
    scan = WatScan(path)
    for page, links in html_pages(data, path):
        scan.pages += 1
        feeds = filter_page_links(page, links, exclude)
        scan.pages_with_feed += bool(feeds)
        scan.feed_links += len(feeds)
        if feeds:
            scan.page_feeds.append(feeds)
    return scan


def filter_page_links(page: str, links: Any, exclude: frozenset[str] = frozenset()) -> list[str]:
    """The feed URLs worth checking among a page's `<link>` elements (as the WAT records them):
    rel="alternate" with a feed type, made absolute, on https, valid like any input, outside the
    `exclude` domains, without session parameters, WordPress's /feed/atom/, /feed/rss2/, ...
    as /feed/ (one feed, already known or proposed under that URL), shaped like a feed URL
    (`_looks_like_a_feed`; that also rules out comment and WordPress archive feeds), and not the
    page's own feed when the page offers another."""
    urls: list[str] = []
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict):
            continue
        rel = str(link.get("rel", "")).lower().split()
        kind = str(link.get("type", "")).split(";")[0].strip().lower()
        href = link.get("url")
        if "alternate" in rel and kind in FEED_TYPES and isinstance(href, str) and href.strip():
            try:
                url = urljoin(page, href.strip())
            except ValueError:
                continue
            if url.lower().startswith("http://"):  # tried on https; other schemes stay invalid
                url = "https://" + url[len("http://") :]
            urls.append(url)
    valid, _invalid = valid_urls(urls)
    valid, _excluded = drop_excluded(valid, exclude)
    cleaned = dict.fromkeys(_main_format(strip_session(url)) for url, _host in valid)
    kept = [url for url in cleaned if _looks_like_a_feed(url)]
    if len(kept) > 1:  # beside the site's feed, the page's own is a post's comments
        kept = [url for url in kept if not _is_own_feed(page, url)]
    return kept


def is_unwanted(url: str) -> bool:
    """On a free domain or matching a rejected pattern: checked on the links before fetching,
    and again on the URL a feed is served from after redirects."""
    if (urlsplit(url).hostname or "").rsplit(".", 1)[-1] in FREE_TLDS:
        return True
    return any(pattern.search(url) for pattern in _REJECTED)


def _patterns(*regexes: str) -> list[re.Pattern[str]]:
    """Compiled case-insensitive: `/RSS` is as much a feed path as `/rss`."""
    return [re.compile(regex, re.IGNORECASE) for regex in regexes]


def _main_format(url: str) -> str:
    """WordPress's feed in another format (/feed/atom/, /feed/rss2/, /feed/rss/, /feed/rdf/) as
    its main feed URL, /feed/; any other URL unchanged."""
    parts = urlsplit(url)
    if parts.query or not _WORDPRESS_FORMAT.search(parts.path):
        return url
    return parts._replace(path=_WORDPRESS_FORMAT.sub("/feed/", parts.path)).geturl()


def _is_own_feed(page: str, url: str) -> bool:
    """The feed of the page itself: the page's URL followed by /feed (not on the home page)."""
    page_parts, parts = urlsplit(page), urlsplit(url)
    page_path = page_parts.path.rstrip("/")
    return (
        bool(page_path)
        and parts.hostname == page_parts.hostname
        and parts.path.rstrip("/") == page_path + "/feed"
    )


def _looks_like_a_feed(url: str) -> bool:
    """Short, not too deep, not on a free domain, no rejected pattern, and what follows the host
    is an accepted shape."""
    parts = urlsplit(url)
    if len(url) > MAX_LENGTH:
        return False
    if is_unwanted(url):
        return False
    if parts.path.count("/") > MAX_SLASHES:
        return False
    if max(len(segment) for segment in parts.path.split("/")) > MAX_SEGMENT:
        return False
    beyond_host = url[len(parts.scheme) + 3 + len(parts.netloc) :]
    return any(pattern.fullmatch(beyond_host) for pattern in _ACCEPTED)


# Anywhere in the URL: never a feed worth proposing.
_REJECTED = _patterns(
    r"/20[0-9]{2}/[0-9]{2}/",  # a dated post
    r"/comments",
    r"comments/",
    r"feed=comments",
    r"commentId=",
    r"/comment/",
    r":Recent",
    r"alt=rss",
    r"/web-stories/",  # the Google Web Stories plugin, not posts
    # forums and wikis (agreed 2026-11-21: mostly change logs and noise in review)
    r"[./-]forums?(?=[./?#-]|$)",  # a forum.* or forums.* host, a /forum/ or -forums/ path
    r"feed\.php",  # DokuWiki, phpBB
    r"app\.php/feed",  # phpBB
    r"syndication\.php",  # MyBB
    r"type=rss;action",  # SMF
    r"/activity/feed",  # BuddyPress member activity, not posts
    r"/rss/(public|\d+-)",  # Invision Community forums
    r"/recent\.rss",  # Digital Commons: recently added papers, not posts
)
# The whole of what follows the host must be one of these.
_ACCEPTED = _patterns(
    # main feeds of platforms whose feed URL has no feed word in the path
    r"/feeds/posts/default",  # Blogger
    r"/(index\.php)?\?feed=(rss2?|atom|rdf)",  # WordPress without pretty links
    r"/(index\.php)?\?format=feed&type=(rss|atom)",  # Joomla
    r"/spip\.php\?page=backend",  # SPIP
    r"/backend\.php",  # PHP-Nuke, XOOPS, old SPIP
    # feed paths
    r"/(atom|rss|rss2|feed|feeds)(\.feed\.php)?/?",
    r"/(atom|rss|rss2|feed|feeds)\.xml",
    r"/\?rss=1",
    r"\?com=rss",
    r"/(atom|rss|rss2|feed|feeds)\.php",
    r"/xml\.php\?rss=true",
    r"/(atom|rss|feed|feeds)(/|\.)[a-zA-Z0-9_.-]+/?",
    r"/[a-zA-Z0-9_.-]+(/|\.)(atom|rss|feed|feeds)/?",
)
