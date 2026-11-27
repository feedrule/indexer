"""Write the published output: data files at the top (output/topic/<topic>/{links.txt,feeds.opml},
the topic names in output/topic/topics.txt (most feeds first), every listed feed in
output/all/{links.txt,feeds.opml} (with a topic or not), one small JSON preview per listed feed in
output/previews/ (for a search page, see `preview_path` and `render_preview`), a copy of the
repository's SPEC.md), and the website in output/docs/ (see `indexer.publish.site`).

The feeds' recent posts are not in the output: they go to a checkout of the `posts` branch
(`write_posts`, one file per feed at `posts_path`), which keeps them after the database clears
them, and each preview links to its feed's file (`posts_url`). publish.yml pushes that branch
normally (never forced) before it force-pushes the output.

A feed is listed when it had a successful check in the last `success_days` days and its newest
item (as of its latest successful check) is less than `item_years` years old. Its topics are
chosen from its posts when it is checked (see `indexer.text.topics`), and mapped again through
data/topics/synonyms.tsv here (`with_synonyms`), so a new synonym is published without waiting
for every feed to be checked again. Only the `top` topics with the most listed feeds
get a directory; output/all has every listed feed, including those with no topic.

Everything here comes from untrusted feeds, so text is escaped for HTML and XML, and topic
names are checked again before they become directory names. The output directory is cleared
before writing, but only if it is empty or was created by this exporter (it holds a marker
file), so a mistyped path cannot wipe an unrelated directory.
"""

import hashlib
import json
import re
import shutil
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from indexer.publish import site

MARKER = ".indexer-output"
SLUG = re.compile(r"[a-z0-9_-]{1,100}")
# Per-topic caps. Each file keeps the most recently active feeds (newest post first), so the topic
# page (site.PAGE_ROWS) lists the first feeds of feeds.opml, which lists the first of links.txt.
LINKS_MAX = 50_000  # links.txt (~35 bytes a line: ~1.7 MB at most)
OPML_MAX = 500  # feeds.opml
# Where the output branch is served: docs/ by GitHub Pages, the data files from raw.
SITE_URL = "https://feedrule.github.io/indexer/"
DATA_URL = "https://raw.githubusercontent.com/feedrule/indexer/output/"
POSTS_URL = "https://raw.githubusercontent.com/feedrule/indexer/posts/"  # the posts branch


@dataclass(frozen=True, slots=True)
class ListedFeed:
    url: str
    title: str | None
    site_url: str | None
    last_item_date: datetime | None
    language: str | None  # detected on the items: "de, en"
    file_size: int | None = None  # body bytes at the latest successful check
    topics: str | None = None  # from the English item titles, most similar first: "rust, wasm"
    subtitle: str | None = None  # Atom <subtitle> or RSS <description>
    source: str | None = None  # inputs.source: the list file it came from (or its homepage)
    source_license: str | None = None  # that list's SPDX license id
    # The latest successful check's most recent posts, [{link, title, description}], newest
    # first; None once cleanup has cleared them (after a few days).
    recent_posts: list[dict[str, str | None]] | None = None

    def languages(self) -> list[str]:
        """The languages detected on the feed's items ("en"), sorted."""
        return sorted({t.strip() for t in (self.language or "").split(",")} - {""})

    def topic_names(self) -> list[str]:
        """The feed's topics, most similar first; only names that make a safe directory."""
        names = [t.strip() for t in (self.topics or "").split(",")]
        return [t for t in dict.fromkeys(names) if t and SLUG.fullmatch(topic_slug(t))]


def with_synonyms(feeds: Iterable[ListedFeed], synonyms: Mapping[str, str]) -> list[ListedFeed]:
    """The feeds with each stored topic mapped to its canonical name (variant -> canonical, as
    `indexer.text.topics.read_synonyms` gives), keeping the order and the first of any repeats."""
    mapped = []
    for feed in feeds:
        names = [synonyms.get(t.strip(), t.strip()) for t in (feed.topics or "").split(",")]
        topics = ", ".join(dict.fromkeys(n for n in names if n)) or None
        mapped.append(feed if topics == feed.topics else replace(feed, topics=topics))
    return mapped


def topic_slug(name: str) -> str:
    """A topic's directory and URL name: + and # spelled out (c++ -> cplusplus, c# -> csharp)."""
    return name.replace("+", "plus").replace("#", "sharp")


@dataclass(frozen=True, slots=True)
class Topic:
    name: str  # as in data/topics/topics.txt: "c++"
    feeds: tuple[ListedFeed, ...]  # newest item first, then by URL

    @property
    def slug(self) -> str:
        return topic_slug(self.name)


def top_topics(feeds: Iterable[ListedFeed], limit: int | None = None) -> list[Topic]:
    """The `limit` topics with the most feeds (all of them if None; ties broken by name), each
    with its feeds."""
    members: defaultdict[str, list[ListedFeed]] = defaultdict(list)
    for feed in feeds:
        for name in feed.topic_names():
            members[name].append(feed)
    ranked = sorted(members.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]
    return [Topic(name, tuple(sorted(fs, key=_newest_first))) for name, fs in ranked]


# --- rendering --------------------------------------------------------------------------------


def by_popularity(topics: Iterable[Topic]) -> list[Topic]:
    """Most feeds first; ties by name."""
    return sorted(topics, key=lambda t: (-len(t.feeds), t.name))


def render_topic_names(topics: Iterable[Topic]) -> str:
    """topic/topics.txt: one published topic name per line, most feeds first. (Slugs have no
    dot, so the file cannot clash with a topic directory.)"""
    return "".join(f"{topic.name}\n" for topic in by_popularity(topics))


def render_links(topic: Topic, limit: int | None = LINKS_MAX) -> str:
    """The `limit` most recently active feeds (all if None), newest post first: the first lines
    are the best results, so a search page can show them in file order."""
    return "".join(f"{f.url}\n" for f in topic.feeds[:limit])


def render_opml(
    topic: Topic, generated: datetime, limit: int | None = OPML_MAX, title: str | None = None
) -> str:
    """The `limit` most recently active feeds (all if None), sorted by URL."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        f"    <title>{xml_escape(title or f'{topic.name} feeds')}</title>",
        f"    <dateCreated>{format_datetime(generated)}</dateCreated>",
        "  </head>",
        "  <body>",
    ]
    for feed in sorted(topic.feeds[:limit], key=lambda f: f.url):  # most active, by URL
        title = quoteattr(" ".join((feed.title or feed.url).split()))
        attrs = f'type="rss" text={title} title={title} xmlUrl={quoteattr(feed.url)}'
        if feed.site_url:
            attrs += f" htmlUrl={quoteattr(feed.site_url)}"
        lines.append(f"    <outline {attrs}/>")
    lines += ["  </body>", "</opml>"]
    return "\n".join(lines) + "\n"


def _hashed(url: str) -> str:
    """<2 hex>/<sha256 of the URL, hex>.json: fixed length whatever the URL (hex of the URL
    itself can exceed the 255-byte file name limit), split in 256 folders. A browser computes it
    with crypto.subtle.digest("SHA-256", new TextEncoder().encode(url))."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"{digest[:2]}/{digest}.json"


def preview_path(url: str) -> str:
    """previews/<2 hex>/<sha256 of the URL, hex>.json: the feed's preview in the output."""
    return f"previews/{_hashed(url)}"


def posts_path(url: str) -> str:
    """data/posts/<2 hex>/<sha256 of the URL, hex>.json: the feed's file on the posts branch
    (the same name as its preview)."""
    return f"data/posts/{_hashed(url)}"


def render_posts(feed: ListedFeed) -> str:
    """One feed's file on the posts branch:

        {"url": "https://...",
         "posts": [{"link": "https://...", "title": "...", "description": "..."}]}

    posts are the feed's most recent posts, newest first (at most 3): link is http(s) or null,
    title and description (the start of the post) may be null. All text is untrusted plain
    text."""
    doc = {
        "url": feed.url,
        "posts": _posts(feed),
    }
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def render_preview(feed: ListedFeed, posts_url: str | None = None) -> str:
    """One feed's preview, for a search page showing the feeds of a topic's links.txt:

        {"url": "https://...", "title": "...", "subtitle": "...", "bytes": 48213,
         "last": "2026-11-14", "languages": ["de", "en"], "topics": ["rust", "webassembly"],
         "source": {"name": "kagisearch/smallweb", "license": "MIT"},
         "posts_url": "https://raw.githubusercontent.com/.../posts/ab/ab12....json"}

    url is the feed URL the file is named after, so a preview stands alone. title, subtitle,
    bytes (body size at the latest successful check) and last are null when unknown; languages
    are the languages detected on its items (sorted, empty if none); topics are its topics
    (names from data/topics/topics.txt, most similar first, empty if none; only those in
    topic/topics.txt have a folder); source is the list the feed came from, as the site's sources
    table shows it (`site.source_of`: name and license); for a source missing from that table,
    name is null and license its SPDX id; null for a feed not in inputs. posts_url is the feed's
    file on the posts branch (`render_posts`), null if it has none. All text is untrusted plain
    text."""
    doc = {
        "url": feed.url,
        "title": " ".join(feed.title.split()) or None if feed.title else None,
        "subtitle": " ".join(feed.subtitle.split()) or None if feed.subtitle else None,
        "bytes": feed.file_size,
        "last": feed.last_item_date.date().isoformat() if feed.last_item_date else None,
        "languages": feed.languages(),
        "topics": feed.topic_names(),
        "source": _source(feed),
        "posts_url": posts_url,
    }
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def _newest_first(feed: ListedFeed) -> tuple[float, str]:
    ts = feed.last_item_date.timestamp() if feed.last_item_date else float("-inf")
    return (-ts, feed.url)


def _posts(feed: ListedFeed) -> list[dict[str, str | None]]:
    return [_post(p) for p in feed.recent_posts or ()]


def _post(post: Mapping[str, str | None]) -> dict[str, str | None]:
    def text(key: str) -> str | None:
        value = post.get(key)
        return " ".join(value.split()) or None if isinstance(value, str) else None

    link = post.get("link")
    return {
        "link": site.safe_href(link) if isinstance(link, str) else None,
        "title": text("title"),
        "description": text("description"),
    }


def _source(feed: ListedFeed) -> dict[str, str | None] | None:
    if not feed.source:
        return None
    if (known := site.source_of(feed.source)) is not None:
        return {"name": known.name, "license": known.license}
    return {"name": None, "license": feed.source_license}


# --- writing ----------------------------------------------------------------------------------


class ExportError(Exception):
    pass


def clean_output_dir(path: Path) -> None:
    """Empty `path` (creating it if needed). Refuses a non-empty directory without the marker."""
    if path.exists():
        if not path.is_dir():
            raise ExportError(f"{path} exists and is not a directory")
        entries = list(path.iterdir())
        if entries and not (path / MARKER).is_file():
            raise ExportError(
                f"{path} is not empty and was not created by indexer export ({MARKER} missing);"
                " refusing to delete it"
            )
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    path.mkdir(parents=True, exist_ok=True)
    (path / MARKER).write_text("Generated by `indexer export`; this directory is replaced.\n")


def write_posts(directory: Path, feeds: Iterable[ListedFeed]) -> None:
    """Write the posts file (`render_posts`) of each feed with recent posts into `directory` (a
    checkout of the posts branch; a recheck with the same posts writes the same bytes, so git
    sees no change). Never deletes a file: a feed whose posts were cleared keeps its last ones."""
    for feed in feeds:
        if feed.recent_posts:
            path = directory / posts_path(feed.url)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_posts(feed), encoding="utf-8")


def write_output(
    path: Path,
    feeds: Sequence[ListedFeed],
    *,
    top: int | None,
    generated: datetime,
    success_days: int,
    item_years: int,
    spec: Path | None = None,
    site_url: str = SITE_URL,
    data_url: str = DATA_URL,
    posts_dir: Path | None = None,
    posts_url: str = POSTS_URL,
) -> list[Topic]:
    """Write everything to `path` (cleared first), with a copy of `spec` (SPEC.md) if given:
    the data files at the top, the website in docs/. With `posts_dir` (a checkout of the posts
    branch, served at `posts_url`), the feeds' posts files are written there first
    (`write_posts`) and each preview links to its feed's file; without it, no preview has a
    posts_url. `site_url`, `data_url` and `posts_url` end with '/'."""
    if spec is not None and not spec.is_file():
        raise ExportError(f"{spec} not found: the output's SPEC.md is copied from it")
    if posts_dir is not None and not posts_dir.is_dir():
        raise ExportError(f"{posts_dir} is not a directory (a checkout of the posts branch)")
    if not all(url.endswith("/") for url in (site_url, data_url, posts_url)):
        raise ExportError("--site-url, --data-url and --posts-url must end with '/'")
    topics = top_topics(feeds, top)
    clean_output_dir(path)
    root = path.resolve()
    unique = {f.url: f for f in feeds}.values()
    published = {t.name: t.slug for t in topics}
    docs = path / "docs"
    for topic in topics:
        directory = path / "topic" / topic.slug
        page = docs / "topic" / topic.slug
        if not SLUG.fullmatch(topic.slug) or not all(
            d.resolve().is_relative_to(root) for d in (directory, page)
        ):
            raise ExportError(f"unsafe topic directory {topic.slug!r}")  # cannot happen
        directory.mkdir(parents=True)
        page.mkdir(parents=True)
        (directory / "links.txt").write_text(render_links(topic), encoding="utf-8")
        (directory / "feeds.opml").write_text(render_opml(topic, generated), encoding="utf-8")
        (page / "index.html").write_text(
            site.render_topic_page(
                topic,
                generated,
                site_url=site_url,
                data_url=data_url,
                opml_max=OPML_MAX,
                links_max=LINKS_MAX,
                published=published,
            ),
            encoding="utf-8",
        )
    (path / "topic").mkdir(exist_ok=True)
    (path / "topic" / "topics.txt").write_text(render_topic_names(topics), encoding="utf-8")
    everything = Topic("all", tuple(sorted(unique, key=_newest_first)))
    (path / "all").mkdir()
    (path / "all" / "links.txt").write_text(render_links(everything, None), encoding="utf-8")
    (path / "all" / "feeds.opml").write_text(
        render_opml(everything, generated, None, "All active feeds"), encoding="utf-8"
    )
    if posts_dir is not None:
        write_posts(posts_dir, everything.feeds)
    for feed in everything.feeds:
        preview = path / preview_path(feed.url)
        preview.parent.mkdir(parents=True, exist_ok=True)
        stored = posts_dir is not None and (posts_dir / posts_path(feed.url)).is_file()
        link = posts_url + posts_path(feed.url) if stored else None
        preview.write_text(render_preview(feed, link), encoding="utf-8")
    if spec is not None:
        shutil.copyfile(spec, path / "SPEC.md")
    listed = len({f.url for t in topics for f in t.feeds})
    docs.mkdir(exist_ok=True)
    for name in (site.ICON, site.STYLE):
        (docs / name).write_bytes(site.FILES.joinpath(name).read_bytes())
    (docs / ".nojekyll").write_text("")
    ranked = by_popularity(topics)
    (docs / "index.html").write_text(
        site.render_index(
            ranked,
            listed,
            generated,
            success_days,
            item_years,
            len(everything.feeds),
            site_url=site_url,
            data_url=data_url,
        ),
        encoding="utf-8",
    )
    (docs / "sitemap.xml").write_text(
        site.render_sitemap(ranked, generated, site_url), encoding="utf-8"
    )
    return topics
