"""The published website: static HTML pages in output/docs/, served by GitHub Pages from the
output branch's /docs folder. An index of the topics and one page per topic (its most recently
active feeds), a stylesheet, feedrule.com's icon, a sitemap and .nojekyll. The data files
(links.txt, OPML, previews) stay outside docs/ and are linked on raw.githubusercontent.com,
so they don't count toward the Pages size limit.

The pages are Jinja templates (publish/templates/) with autoescaping on, so every value from
a feed is escaped unless marked safe (nothing is). Escaping does not make a URL safe, so a link
is only written for an http(s) URL (`safe_href`: a feed's site URL could be `javascript:`).
Links to the feeds and their sites are rel="nofollow"; links to feedrule.com are not.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from importlib.resources import files
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import quote, urlsplit

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

if TYPE_CHECKING:
    from indexer.publish.export import ListedFeed, Topic

PAGE_ROWS = 200  # feeds on a topic page (the first ones of its feeds.opml)
SHOWN_ROWS = 20  # of those, shown at first; "Show more" reveals as many again (all without script)
SUBTITLE_CHARS = 200  # a feed's subtitle on its row, cut beyond this
INDEX_TOPICS = 50  # topics shown on the home page (the filter box searches all of them)
FILTER_MATCHES = 100  # topics the filter box shows at most
FEEDRULE = "https://feedrule.com/"
FEEDRULE_FORM = "https://feedrule.com/?feed="  # the email-digest form, prefilled with a feed
REPO = "https://github.com/feedrule/indexer"
FILES = files("indexer.publish")  # the templates, stylesheet and icon sit next to this module
ICON = "feedrule.svg"
STYLE = "style.css"
# Pages link the stylesheet as style.css?v=<hash of its content>: GitHub Pages lets browsers
# cache it for 10 minutes, and a new URL makes them load a changed stylesheet right away.
STYLE_VERSION = hashlib.sha256(FILES.joinpath(STYLE).read_bytes()).hexdigest()[:12]
# Topic pages with fewer feeds are published (and linked) but marked noindex and left out of the
# sitemap: near-empty pages would count as thin content against the whole site. A page loses the
# mark on its own once its topic grows.
MIN_INDEXED_FEEDS = 3


_env = Environment(
    loader=PackageLoader("indexer.publish", "templates"),
    autoescape=select_autoescape(["html", "xml"]),
    undefined=StrictUndefined,  # a typo in a template fails the export instead of rendering ""
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
_env.filters["num"] = lambda n: f"{n:,}"
_env.policies["json.dumps_kwargs"] = {"separators": (",", ":")}  # compact topic list
_env.globals.update(
    icon=ICON, style=f"{STYLE}?v={STYLE_VERSION}", feedrule=FEEDRULE, repo=REPO, noindex=False
)


class Source(NamedTuple):
    name: str
    url: str
    license: str
    license_url: str  # or ""
    feeds: str  # what it has
    how: str  # a "how" link, or ""


def source_of(url: str | None) -> Source | None:
    """The entry of SOURCES an inputs.source URL belongs to: the first whose name (without a
    trailing slash) is in the URL's path. That matches a list's raw file URLs
    (raw.githubusercontent.com/kagisearch/smallweb/...) and this repository's files under any
    branch or old name (.../data/survivors.txt). Feeds loaded from data/survivors/ (the proposals
    before they moved to the proposals branch) keep that source: they belong to data/proposals/.
    None if no entry matches."""
    path = urlsplit(url).path if url else ""
    for old, new in _OLD_NAMES.items():
        path = path.replace(old, new)
    return next((s for s in SOURCES if s.name.rstrip("/") in path), None)


_OLD_NAMES = {"data/survivors": "data/proposals"}  # an inputs.source path -> its entry's


def safe_href(url: str | None) -> str | None:
    """An untrusted URL usable as a link (the template escapes it), or None unless it is
    http(s)."""
    if not url or urlsplit(url.strip()).scheme.lower() not in ("http", "https"):
        return None
    return url.strip()


def feedrule_href(url: str) -> str:
    """feedrule.com's form prefilled with the feed `url`, percent-encoded (only ':' and '/'
    kept) so it stays one query value."""
    return FEEDRULE_FORM + quote(url, safe=":/")


def other_topics(
    feed: ListedFeed, current: str, published: Mapping[str, str]
) -> list[tuple[str, str | None]]:
    """The feed's topics other than `current`, most similar first, each with its page's slug
    (None if the topic has no page). `published` maps a topic name to its page's slug."""
    return [(name, published.get(name)) for name in feed.topic_names() if name != current]


def subtitle_line(feed: ListedFeed, label: str) -> str:
    """The feed's subtitle on one line, cut at SUBTITLE_CHARS (with an ellipsis); empty when it
    has none or only repeats the title."""
    text = " ".join((feed.subtitle or "").split())
    if text.casefold() == label.casefold():
        return ""
    if len(text) > SUBTITLE_CHARS:
        text = text[: SUBTITLE_CHARS - 1].rstrip() + "…"
    return text


def render_topic_page(
    topic: Topic,
    generated: datetime,
    *,
    site_url: str,
    data_url: str,
    opml_max: int,
    links_max: int,
    published: Mapping[str, str] | None = None,
) -> str:
    """`published` maps the name of each topic with a page to its slug (a feed's other topics
    link to theirs)."""
    total = len(topic.feeds)
    return _env.get_template("topic.html").render(
        title=f"{total:,} active {topic.name} RSS feeds",
        description=(
            f"{total:,} working RSS and Atom feeds about {topic.name}, newest posts first."
            " Download them as OPML, or get any of them by email."
        ),
        canonical=f"{site_url}topic/{topic.slug}/",
        root="../../",
        generated=generated,
        data_url=data_url,
        topic=topic.name,
        total=total,
        data=f"{data_url}topic/{topic.slug}/",
        opml_max=opml_max,
        links_max=links_max,
        rows=[_row(f, topic.name, published or {}) for f in topic.feeds[:PAGE_ROWS]],
        shown_rows=SHOWN_ROWS,
        noindex=not indexed(topic),
    )


def indexed(topic: Topic) -> bool:
    """Whether search engines should index the topic's page (and the sitemap list it)."""
    return len(topic.feeds) >= MIN_INDEXED_FEEDS


def render_index(
    topics: Sequence[Topic],
    listed: int,
    generated: datetime,
    success_days: int,
    item_years: int,
    total: int,
    *,
    site_url: str,
    data_url: str,
) -> str:
    entries = [(t.name, t.slug, len(t.feeds)) for t in topics]
    return _env.get_template("index.html").render(
        title="Active RSS feeds, by topic",
        description=(
            f"{total:,} working RSS and Atom feeds in {len(topics):,} topics, checked regularly."
            " Download them as OPML, or get any of them by email."
        ),
        canonical=site_url,
        root="",
        generated=generated,
        data_url=data_url,
        total=total,
        listed=listed,
        success_days=success_days,
        item_years=item_years,
        popular=entries[:INDEX_TOPICS],
        topics=entries,  # all of them, for the filter box (tojson escapes </script>)
        filter_matches=FILTER_MATCHES,
        sources=SOURCES,
    )


def render_sitemap(topics: Iterable[Topic], generated: datetime, site_url: str) -> str:
    urls = [site_url] + [f"{site_url}topic/{t.slug}/" for t in topics if indexed(t)]
    return _env.get_template("sitemap.xml").render(urls=urls, day=generated.date().isoformat())


# The source lists and their licenses, shown on the index. Update with data/sources.toml.
SOURCES = (
    Source("kagisearch/smallweb", "https://github.com/kagisearch/smallweb",
           "MIT", "", "~44,000 personal sites", ""),
    Source("outcoldman/hackernews-personal-blogs",
           "https://github.com/outcoldman/hackernews-personal-blogs",
           "MIT", "", "~1,230 Hacker News users' blogs", ""),
    Source("simevidas/web-dev-feeds", "https://github.com/simevidas/web-dev-feeds",
           "MIT", "", "~1,070 web development blogs", ""),
    Source("plenaryapp/awesome-rss-feeds", "https://github.com/plenaryapp/awesome-rss-feeds",
           "CC0-1.0", "", "~750 by topic, and news sites by country", ""),
    Source("tuan3w/awesome-tech-rss", "https://github.com/tuan3w/awesome-tech-rss",
           "CC0-1.0", "", "~140 startups, science, technology", ""),
    Source("matthiasjost/dotnet-creators-opml",
           "https://github.com/matthiasjost/dotnet-creators-opml",
           "MIT", "", "~140 .NET blogs", ""),
    Source("data/proposals/", f"{REPO}/tree/proposals/data/proposals",
           "Common Crawl terms", "https://commoncrawl.org/terms-of-use",
           "old feeds still active today, reviewed by hand", f"{REPO}#feeds-from-common-crawl"),
    Source("data/submissions.txt", f"{REPO}/blob/develop/data/submissions.txt",
           "MIT", "", "feeds submitted to the project", ""),
)  # fmt: skip


def _row(feed: ListedFeed, current: str, published: Mapping[str, str]) -> dict[str, object]:
    parts = urlsplit(feed.url)
    # the feed's website, or else the home page of the feed's own domain
    site = safe_href(feed.site_url) or safe_href(f"{parts.scheme}://{parts.netloc}/") or ""
    label = " ".join((feed.title or "").split()) or feed.url
    return {
        "label": label,
        "subtitle": subtitle_line(feed, label),
        "feed": safe_href(feed.url) or "",
        "site": site,
        "host": urlsplit(site).hostname or "",
        "feedrule": feedrule_href(feed.url),
        "topics": other_topics(feed, current, published),
        "date": feed.last_item_date.date().isoformat() if feed.last_item_date else "",
    }
