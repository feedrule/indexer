"""Publishing lists per topic and the website: selection, escaping of untrusted text, and the output
directory."""

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from defusedxml import ElementTree as SafeET

from indexer.publish.export import (
    LINKS_MAX,
    MARKER,
    OPML_MAX,
    POSTS_URL,
    ExportError,
    ListedFeed,
    clean_output_dir,
    posts_path,
    preview_path,
    render_links,
    render_opml,
    render_preview,
    top_topics,
    topic_slug,
    with_synonyms,
    write_output,
)
from indexer.publish.site import (
    INDEX_TOPICS,
    MIN_INDEXED_FEEDS,
    PAGE_ROWS,
    SHOWN_ROWS,
    STYLE_VERSION,
    SUBTITLE_CHARS,
    feedrule_href,
    other_topics,
    render_topic_page,
    safe_href,
)

SITE = "https://s.example/feeds/"
DATA = "https://d.example/out/"


NOW = datetime(2026, 11, 16, tzinfo=UTC)


ROOT = Path(__file__).resolve().parents[1]


def page(topic: object) -> str:
    return render_topic_page(
        topic,  # type: ignore[arg-type]
        NOW,
        site_url=SITE,
        data_url=DATA,
        opml_max=OPML_MAX,
        links_max=LINKS_MAX,
    )


def rows(html: str) -> list[str]:
    return re.findall(r'<a class="mail" href="([^"]*)"', html)


def feed_links(html: str) -> list[str]:
    return re.findall(r'<a class="rss" href="([^"]*)"', html)


def chips(index: str) -> list[str]:
    """The topic names shown on the home page, in order."""
    return re.findall(r'<li><a href="topic/[^"]+/">([^<]+)<span', index)


def feed(n: int, topics: str | None = None, *, day: int = 1, title: str = "T") -> ListedFeed:
    return ListedFeed(
        url=f"https://f{n}.example/feed",
        title=title,
        site_url=f"https://f{n}.example/",
        last_item_date=datetime(2026, 10, day, tzinfo=UTC),
        language="en",
        topics=topics,
    )


def test_topic_names_most_similar_first() -> None:
    assert feed(1, "rust, c++, web-development").topic_names() == ["rust", "c++", "web-development"]
    assert feed(2).topic_names() == [] and feed(3, " , ").topic_names() == []
    assert topic_slug("c++") == "cplusplus" and topic_slug("c#") == "csharp"
    assert topic_slug("apple-tv+") == "apple-tvplus" and topic_slug("rust") == "rust"


@pytest.mark.parametrize(
    "bad", ["../etc", "a/b", "UPPER", "x" * 101, ".", "dot.ted", "a?b", "a%2f"]
)
def test_unsafe_topic_names_never_become_directories(bad: str) -> None:
    assert feed(1, bad).topic_names() == []


def test_top_topics_by_feed_count_then_name() -> None:
    feeds = [feed(1, "a, b, c"), feed(2, "a, b"), feed(3, "a, d"), feed(4, "d")]
    topics = top_topics(feeds, limit=3)
    assert [(t.name, len(t.feeds)) for t in topics] == [("a", 3), ("b", 2), ("d", 2)]


def test_feeds_in_a_topic_newest_first() -> None:
    feeds = [feed(1, "x", day=3), feed(2, "x", day=9), feed(3, "x", day=9)]
    (topic,) = top_topics(feeds, 1)
    assert [f.url for f in topic.feeds] == [
        "https://f2.example/feed",
        "https://f3.example/feed",
        "https://f1.example/feed",
    ]


def test_only_http_links_are_written() -> None:
    # returned as is: the templates escape it
    assert safe_href(" https://a.example/x?a=1&b=2") == "https://a.example/x?a=1&b=2"
    for bad in ("javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x", "", None):
        assert safe_href(bad) is None


def test_topic_page_rows_are_escaped_and_capped() -> None:
    evil = '<script>alert(1)</script> & "x"'
    feeds = [feed(i, "x", title=evil) for i in range(PAGE_ROWS + 5)]
    feeds.append(
        ListedFeed("https://f0.example/feed", "ok", "javascript:alert(1)", None, "en", None, "x")
    )
    (topic,) = top_topics(feeds, 1)
    html = page(topic)
    assert len(rows(html)) == PAGE_ROWS
    assert "<script>" not in html.split("<table")[1].split("</table>")[0]
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &#34;x&#34;" in html  # autoescaped
    assert f"{PAGE_ROWS + 6} active feeds are about" in html
    # a non-http site URL is never a link: the feed's home page replaces it
    (only,) = top_topics([feeds[-1]], 1)
    assert "javascript:" not in page(only)
    assert '<a class="title" href="https://f0.example/" rel="nofollow">ok</a>' in page(only)


def test_feedrule_link_is_prefilled_and_encoded() -> None:
    assert (
        feedrule_href("https://github.com/rust-lang/rust/releases.atom")
        == "https://feedrule.com/?feed=https://github.com/rust-lang/rust/releases.atom"
    )
    assert feedrule_href('https://e.example/f?a=1&b="><script>|x').endswith(
        "feed=https://e.example/f%3Fa%3D1%26b%3D%22%3E%3Cscript%3E%7Cx"
    )
    (topic,) = top_topics([feed(1, "x")], 1)
    html = page(topic)
    assert rows(html) == ["https://feedrule.com/?feed=https://f1.example/feed"]
    assert '<img src="../../feedrule.svg" width="20" height="20" alt="Get by email">' in html
    # feed and site links are nofollow; feedrule.com links are not
    # the title and "Website" link to the website; "Feed" to the feed
    assert '<a class="title" href="https://f1.example/" rel="nofollow">T</a>' in html
    # one cell per feed: the domain, Website and Feed on one line, the date on its own
    assert (
        'f1.example · <a class="site" href="https://f1.example/" rel="nofollow">Website</a>'
        ' · <a class="rss" href="https://f1.example/feed" rel="nofollow">Feed</a></span>' in html
    )
    assert html.count("<td") == html.count('<td class="feed">') == 1
    assert 'Last indexed post: <time datetime="' in html
    assert 'href="https://feedrule.com/" rel' not in html


def test_other_topics_most_similar_first() -> None:
    f = feed(1, "rare, cur, c++")
    published = {"c++": "cplusplus", "cur": "cur"}
    # the page's own topic left out; the stored order kept; linked (by slug) if published
    assert other_topics(f, "cur", published) == [("rare", None), ("c++", "cplusplus")]
    assert other_topics(feed(2, "cur"), "cur", {}) == []


def test_topic_pages_show_each_feeds_other_topics(tmp_path: Path) -> None:
    out = tmp_path / "out"
    feeds = [feed(1, "css, rare, c++"), feed(2, "css, c++"), feed(3, "c++")]
    write_output(out, feeds, top=2, generated=NOW, success_days=60, item_years=5)
    css = (out / "docs" / "topic" / "css" / "index.html").read_text()
    assert '<span class="ftopics"><span>rare</span><a href="../cplusplus/">c++</a></span>' in css
    cpp = (out / "docs" / "topic" / "cplusplus" / "index.html").read_text()
    assert "<h1>c++ <span" in cpp and (out / "topic" / "cplusplus" / "links.txt").exists()


def test_opml_is_well_formed_and_escaped() -> None:
    evil = feed(1, "x", title='A & B "quoted" <tag>')
    (topic,) = top_topics([evil, feed(2, "x", title="")], 1)
    root = SafeET.fromstring(render_opml(topic, NOW).encode())
    outlines = root.findall("./body/outline")
    assert [o.get("xmlUrl") for o in outlines] == [
        "https://f1.example/feed",
        "https://f2.example/feed",
    ]
    assert outlines[0].get("title") == 'A & B "quoted" <tag>'
    assert outlines[1].get("title") == "https://f2.example/feed"  # no title: the URL
    assert outlines[0].get("htmlUrl") == "https://f1.example/"


def test_clean_output_dir_refuses_unrelated_directory(tmp_path: Path) -> None:
    (tmp_path / "precious.txt").write_text("keep me")
    with pytest.raises(ExportError, match="refusing"):
        clean_output_dir(tmp_path)
    assert (tmp_path / "precious.txt").exists()


def test_clean_output_dir_refuses_a_file(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.write_text("x")
    with pytest.raises(ExportError, match="not a directory"):
        clean_output_dir(target)


def test_clean_output_dir_replaces_previous_output(tmp_path: Path) -> None:
    out = tmp_path / "out"
    clean_output_dir(out)
    (out / "topic" / "old").mkdir(parents=True)
    (out / "topic" / "old" / "links.txt").write_text("stale")
    clean_output_dir(out)
    assert sorted(p.name for p in out.iterdir()) == [MARKER]


def test_write_output(tmp_path: Path) -> None:
    out = tmp_path / "out"
    feeds = [feed(1, "css, web-dev"), feed(2, "css"), feed(3, "rust")]
    topics = write_output(
        out,
        feeds,
        top=2,
        generated=NOW,
        success_days=60,
        item_years=5,
        site_url=SITE,
        data_url=DATA,
    )
    assert [t.name for t in topics] == ["css", "rust"]
    css = out / "topic" / "css"
    assert sorted(p.name for p in css.iterdir()) == ["feeds.opml", "links.txt"]
    assert (css / "links.txt").read_text() == ("https://f1.example/feed\nhttps://f2.example/feed\n")
    assert not (out / "topic" / "web-dev").exists()  # not in the top 2
    assert not list(out.rglob("*.md"))  # no Markdown: the site is HTML
    docs = out / "docs"
    assert sorted(p.name for p in docs.iterdir()) == [
        ".nojekyll",
        "feedrule.svg",
        "index.html",
        "sitemap.xml",
        "style.css",
        "topic",
    ]
    assert (docs / "feedrule.svg").read_bytes().startswith(b"<svg ")
    index = (docs / "index.html").read_text()
    assert '<a href="topic/css/">css<span class="n">2</span></a>' in index
    assert "3 RSS and Atom feeds that work" in index
    assert f'<link rel="canonical" href="{SITE}">' in index
    topic_page = (docs / "topic" / "css" / "index.html").read_text()
    assert f'<a class="button" href="{DATA}topic/css/feeds.opml">' in topic_page
    assert f'href="{DATA}topic/css/links.txt"' in topic_page
    assert f'<link rel="stylesheet" href="../../style.css?v={STYLE_VERSION}">' in topic_page
    assert len(STYLE_VERSION) == 12
    assert '<meta name="robots" content="noindex">' in topic_page  # 2 feeds: too few
    assert "noindex" not in index
    assert f'<link rel="canonical" href="{SITE}topic/css/">' in topic_page
    sitemap = SafeET.fromstring((docs / "sitemap.xml").read_bytes())
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    assert [u.findtext(f"{ns}loc") for u in sitemap] == [SITE]  # no topic has 3 feeds


def test_small_topics_are_published_but_not_indexed(tmp_path: Path) -> None:
    out = tmp_path / "out"
    feeds = [feed(i, "css" if i <= MIN_INDEXED_FEEDS else "rust") for i in range(1, 6)]
    topics = write_output(
        out, feeds, top=None, generated=NOW, success_days=60, item_years=5, site_url=SITE
    )
    assert [t.name for t in topics] == ["css", "rust"]  # no cap: every topic gets a page
    docs = out / "docs"
    css = (docs / "topic" / "css" / "index.html").read_text()
    rust = (docs / "topic" / "rust" / "index.html").read_text()
    assert "noindex" not in css and '<meta name="robots" content="noindex">' in rust
    sitemap = (docs / "sitemap.xml").read_text()
    assert f"{SITE}topic/css/" in sitemap and "topic/rust/" not in sitemap
    index = (docs / "index.html").read_text()
    assert 'href="topic/rust/"' in index  # still linked and searchable


def test_top_topics_without_a_limit_keeps_all() -> None:
    feeds = [feed(1, "a, b"), feed(2, "c")]
    assert [t.name for t in top_topics(feeds)] == ["a", "b", "c"]
    assert [t.name for t in top_topics(feeds, 1)] == ["a"]


def test_header_links_the_repository(tmp_path: Path) -> None:
    out = tmp_path / "out"
    write_output(out, [feed(1, "css")], top=None, generated=NOW, success_days=60, item_years=5,
                 site_url=SITE, data_url=DATA)  # fmt: skip
    index = (out / "docs" / "index.html").read_text()
    topic_page = (out / "docs" / "topic" / "css" / "index.html").read_text()
    for page in (index, topic_page):
        assert '<a class="gh" href="https://github.com/feedrule/indexer"' in page


def test_urls_must_end_with_a_slash(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="end with"):
        write_output(
            tmp_path / "o",
            [],
            top=1,
            generated=NOW,
            success_days=1,
            item_years=1,
            site_url="https://s.example",
        )


def test_all_lists_every_feed_with_a_topic_or_not(tmp_path: Path) -> None:
    out = tmp_path / "out"
    feeds = [feed(1, "css"), feed(2, ""), feed(3), feed(4, "rare")]
    write_output(
        out,
        feeds,
        top=1,
        generated=NOW,
        success_days=180,
        item_years=5,
        data_url=DATA,
    )
    urls = [f"https://f{i}.example/feed" for i in range(1, 5)]
    assert (out / "all" / "links.txt").read_text().splitlines() == urls
    root = SafeET.fromstring((out / "all" / "feeds.opml").read_bytes())
    assert root.findtext("./head/title") == "All active feeds"
    assert [o.get("xmlUrl") for o in root.findall("./body/outline")] == urls
    index = (out / "docs" / "index.html").read_text()
    assert "4 RSS and Atom feeds that work" in index
    assert "at least one post in the last 5 years." in index
    assert "all/links.txt" not in index and "all/feeds.opml" not in index  # too big to offer
    assert not (out / "topic" / "all").exists()  # all/ is outside topic/: no clash


def test_all_is_not_capped() -> None:
    (topic,) = top_topics([feed(i, "x") for i in range(OPML_MAX + 5)], 1)
    assert len(render_links(topic, None).splitlines()) == OPML_MAX + 5
    root = SafeET.fromstring(render_opml(topic, NOW, None).encode())
    assert len(root.findall("./body/outline")) == OPML_MAX + 5


def test_per_file_caps_keep_the_most_recently_active() -> None:
    # feed i posted on day (i % 28) + 1: the newest feeds have the highest day numbers
    feeds = [feed(i, "x", day=(i % 28) + 1) for i in range(LINKS_MAX + 300)]
    (topic,) = top_topics(feeds, 1)
    newest = topic.feeds  # newest post first

    links = render_links(topic).splitlines()
    assert len(links) == LINKS_MAX
    assert set(links) == {f.url for f in newest[:LINKS_MAX]}
    assert links == [f.url for f in newest[:LINKS_MAX]]  # newest post first

    root = SafeET.fromstring(render_opml(topic, NOW).encode())
    opml = [o.get("xmlUrl") for o in root.findall("./body/outline")]
    assert len(opml) == OPML_MAX
    assert set(opml) == {f.url for f in newest[:OPML_MAX]}

    html = page(topic)
    assert feed_links(html) == opml[:PAGE_ROWS]  # the page shows the start of feeds.opml
    assert f"{LINKS_MAX + 300:,} active feeds are about" in html
    assert f"Below: the {PAGE_ROWS} most recently active" in html
    assert f"has the {OPML_MAX} most recently active" in html
    assert f"lists the {LINKS_MAX:,} most recently active" in html


def test_small_topic_lists_everything() -> None:
    (topic,) = top_topics([feed(i, "x") for i in range(3)], 1)
    assert len(render_links(topic).splitlines()) == 3
    assert "Subscribe to all of them" in page(topic)


def test_topic_page_says_all_when_files_are_not_capped() -> None:
    (topic,) = top_topics([feed(i, "x") for i in range(PAGE_ROWS + 10)], 1)
    html = page(topic)
    assert f"has all {PAGE_ROWS + 10}; links.txt lists all {PAGE_ROWS + 10}," in html


def test_index_sorted_by_feed_count(tmp_path: Path) -> None:
    feeds = [feed(1, "b, a, c"), feed(2, "c, a"), feed(3, "c"), feed(4, "d")]
    write_output(tmp_path / "o", feeds, top=3, generated=NOW, success_days=180, item_years=5)
    # topic/topics.txt: the published topics, most feeds first, ties by name (b and d have 1)
    assert (tmp_path / "o" / "topic" / "topics.txt").read_text() == "c\na\nb\n"
    index = (tmp_path / "o" / "docs" / "index.html").read_text()
    assert chips(index) == ["c", "a", "b"]  # 3, 2, 1 feeds


def test_previews(tmp_path: Path) -> None:
    out = tmp_path / "out"
    long_url = "https://f9.example/feed?" + "q=x&" * 200  # hex of it would not fit a file name
    feeds = [
        ListedFeed(
            "https://f1.example/feed",
            '  A <b>"x"</b>\n',
            None,
            datetime(2026, 10, 3, tzinfo=UTC),
            "en, fr, de",
            48213,
            "rust, web-development",
            "  Notes on\n <i>Rust</i> ",
            "https://raw.githubusercontent.com/simevidas/web-dev-feeds/master/feeds.opml",
            "MIT",
            [
                {
                    "link": "https://f1.example/a",
                    "title": " New\n <b>post</b>",
                    "description": None,
                },
                {"link": "javascript:alert(1)", "title": None, "description": "  text "},
            ],
        ),
        ListedFeed(long_url, "", None, None, None, None),
    ]
    write_output(out, feeds, top=1, generated=NOW, success_days=180, item_years=5)
    digest = hashlib.sha256(b"https://f1.example/feed").hexdigest()
    assert preview_path("https://f1.example/feed") == f"previews/{digest[:2]}/{digest}.json"
    doc = json.loads((out / preview_path("https://f1.example/feed")).read_text(encoding="utf-8"))
    # every language; every topic of the feed, published or not (only rust is)
    assert doc == {
        "url": "https://f1.example/feed",
        "title": 'A <b>"x"</b>',  # plain text: the page must not render it as HTML
        "subtitle": "Notes on <i>Rust</i>",  # whitespace collapsed, also plain text
        "bytes": 48213,
        "last": "2026-10-03",
        "languages": ["de", "en", "fr"],
        "topics": ["rust", "web-development"],  # as stored: most similar first
        "source": {  # as the site's sources table shows it
            "name": "simevidas/web-dev-feeds",
            "license": "MIT",
        },
        "posts_url": None,  # no posts directory given
    }
    other = json.loads((out / preview_path(long_url)).read_text(encoding="utf-8"))
    assert other == {
        "url": long_url,
        "title": None,
        "subtitle": None,
        "bytes": None,
        "last": None,
        "languages": [],
        "topics": [],
        "source": None,  # not in inputs
        "posts_url": None,
    }
    assert len(list((out / "previews").rglob("*.json"))) == 2


def write_with_posts(out: Path, posts: Path, feeds: list[ListedFeed]) -> None:
    write_output(
        out,
        feeds,
        top=None,
        generated=NOW,
        success_days=180,
        item_years=5,
        posts_dir=posts,
        posts_url="https://p.example/posts/",
    )


def test_posts_files_keep_the_posts_and_previews_link_them(tmp_path: Path) -> None:
    out, posts = tmp_path / "out", tmp_path / "posts"
    posts.mkdir()
    with_posts = replace(
        feed(1),
        recent_posts=[
            {"link": "javascript:alert(1)", "title": " New\n <b>post</b>", "description": None}
        ],
    )
    write_with_posts(out, posts, [with_posts, feed(2)])
    digest = hashlib.sha256(b"https://f1.example/feed").hexdigest()
    assert posts_path("https://f1.example/feed") == f"{digest[:2]}/{digest}.json"
    stored = json.loads((posts / posts_path(with_posts.url)).read_text(encoding="utf-8"))
    assert stored == {
        "url": "https://f1.example/feed",
        "posts": [{"link": None, "title": "New <b>post</b>", "description": None}],
    }
    preview = json.loads((out / preview_path(with_posts.url)).read_text(encoding="utf-8"))
    assert preview["posts_url"] == f"https://p.example/posts/{posts_path(with_posts.url)}"
    assert "posts" not in preview
    other = json.loads((out / preview_path(feed(2).url)).read_text(encoding="utf-8"))
    assert other["posts_url"] is None  # no posts, no file
    assert [p.name for p in posts.rglob("*.json")] == [f"{digest}.json"]

    # Later: the database cleared the posts; the file stays and the preview still links it.
    write_with_posts(out, posts, [feed(1)])
    assert json.loads((posts / posts_path(with_posts.url)).read_text()) == stored
    preview = json.loads((out / preview_path(with_posts.url)).read_text(encoding="utf-8"))
    assert preview["posts_url"] == f"https://p.example/posts/{posts_path(with_posts.url)}"


def test_posts_files_change_only_with_the_posts(tmp_path: Path) -> None:
    out, posts = tmp_path / "out", tmp_path / "posts"
    posts.mkdir()
    first = replace(
        feed(1), recent_posts=[{"link": "https://f1.example/a", "title": "A", "description": "x"}]
    )
    write_with_posts(out, posts, [first])
    path = posts / posts_path(first.url)
    before = path.read_bytes()
    write_with_posts(out, posts, [replace(first, file_size=99)])  # a recheck, same posts
    assert path.read_bytes() == before  # same bytes: no change for git
    newer = replace(
        first, recent_posts=[{"link": "https://f1.example/b", "title": "B", "description": None}]
    )
    write_with_posts(out, posts, [newer])
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["posts"] == [{"link": "https://f1.example/b", "title": "B", "description": None}]


def test_posts_dir_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="posts branch"):
        write_with_posts(tmp_path / "out", tmp_path / "missing", [feed(1)])
    assert not (tmp_path / "out").exists()


def test_posts_url_must_end_with_a_slash(tmp_path: Path) -> None:
    assert POSTS_URL.endswith("/posts/")
    (tmp_path / "posts").mkdir()
    with pytest.raises(ExportError, match="end with"):
        write_output(
            tmp_path / "o",
            [],
            top=1,
            generated=NOW,
            success_days=1,
            item_years=1,
            posts_dir=tmp_path / "posts",
            posts_url="https://p.example/posts",
        )


def test_preview_is_indented() -> None:
    text = render_preview(feed(1, "css, rust", title="T"))
    assert text.startswith('{\n  "url": "https://f1.example/feed",\n') and text.endswith("}\n")
    assert '\n    "css",\n' in text and '"languages": [\n    "en"\n  ]' in text


def test_spec_is_copied(tmp_path: Path) -> None:
    out, spec = tmp_path / "out", tmp_path / "SPEC.md"
    spec.write_text("# spec\n")
    write_output(
        out,
        [feed(1, "css")],
        top=1,
        generated=NOW,
        success_days=180,
        item_years=5,
        spec=spec,
    )
    assert (out / "SPEC.md").read_text() == "# spec\n"


def test_missing_spec_fails_before_touching_the_output(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with pytest.raises(ExportError, match="not found"):
        write_output(
            out,
            [],
            top=1,
            generated=NOW,
            success_days=180,
            item_years=5,
            spec=tmp_path / "missing.md",
        )
    assert not out.exists()


def test_repository_spec_matches_the_output() -> None:
    """SPEC.md is hand-written: it must list every file pattern and the current limits."""
    spec = (ROOT / "SPEC.md").read_text(encoding="utf-8")
    for pattern in (
        "topic/topics.txt",
        "topic/<slug>/links.txt",
        "topic/<slug>/feeds.opml",
        "all/links.txt",
        "all/feeds.opml",
        "previews/<h:2>/<h>.json",
        "docs/index.html",
        "docs/topic/<slug>/index.html",
        "docs/style.css",
        "docs/feedrule.svg",
        "docs/sitemap.xml",
        "docs/.nojekyll",
    ):
        assert f"`{pattern}`" in spec
    assert f"{LINKS_MAX:,}" in spec and f"the {OPML_MAX} most" in spec
    assert f"first {PAGE_ROWS} of those feeds" in spec and "SHA-256" in spec
    assert "`cplusplus`" in spec and "`csharp`" in spec  # the slug rule
    for field in (
        "url",
        "title",
        "subtitle",
        "bytes",
        "last",
        "languages",
        "topics",
        "posts_url",
        "posts",
    ):
        assert f"`{field}`" in spec
    assert "`<h:2>/<h>.json`" in spec  # the posts branch's files


def test_index_shows_the_largest_topics_and_searches_all(tmp_path: Path) -> None:
    feeds = [feed(i, f"t{i:03d}") for i in range(INDEX_TOPICS + 20)] + [feed(999, "t000, c#")]
    write_output(tmp_path / "o", feeds, top=1000, generated=NOW, success_days=180, item_years=5)
    index = (tmp_path / "o" / "docs" / "index.html").read_text()
    shown = chips(index)
    assert len(shown) == INDEX_TOPICS and shown[0] == "t000"  # the largest first
    topics = json.loads(
        re.search(r'<script type="application/json" id="topics">(.*?)</script>', index).group(1)
    )  # type: ignore[union-attr]
    assert len(topics) == INDEX_TOPICS + 21 and topics[0] == ["t000", "t000", 2]
    assert ["c#", "csharp", 1] in topics  # name, slug (the filter links to topic/<slug>/), feeds
    assert (tmp_path / "o" / "docs" / "topic" / "t069" / "index.html").exists()  # published


def test_index_lists_the_sources_and_licenses(tmp_path: Path) -> None:
    out = tmp_path / "out"
    write_output(out, [feed(1, "css")], top=1, generated=NOW, success_days=180, item_years=5)
    index = (out / "docs" / "index.html").read_text()
    assert '<h2 id="sources">Where the feeds come from</h2>' in index
    assert (
        '<tr><td><a href="https://github.com/kagisearch/smallweb">kagisearch/smallweb</a></td>'
        "<td>MIT</td>" in index
    )
    assert 'href="data/' not in index  # links are absolute: the site is not the repository


def test_rows_show_the_subtitle_escaped_and_cut() -> None:
    def with_subtitle(n: int, title: str, subtitle: str | None) -> ListedFeed:
        return ListedFeed(
            f"https://f{n}.example/feed", title, None, None, "en", None, "x", subtitle
        )

    feeds = [
        with_subtitle(1, "Food in Jars", "  Canning,\n <b>pickling</b> & preserving "),
        with_subtitle(2, "Same", "same"),  # only the title again: not shown
        with_subtitle(3, "Long", "word " * 100),
        with_subtitle(4, "None", None),
    ]
    (topic,) = top_topics(feeds, 1)
    html = page(topic)
    assert '<span class="sub">Canning, &lt;b&gt;pickling&lt;/b&gt; &amp; preserving</span>' in html
    subs = re.findall(r'<span class="sub">([^<]*)</span>', html)
    assert len(subs) == 2  # feeds 2 and 4 have no line
    assert len(subs[1]) == SUBTITLE_CHARS and subs[1].endswith("…")


def test_show_more_reveals_the_rest_of_the_rows() -> None:
    (topic,) = top_topics([feed(i, "x") for i in range(SHOWN_ROWS + 5)], 1)
    html = page(topic)
    assert len(rows(html)) == SHOWN_ROWS + 5  # all in the page: it works without the script
    assert '<button id="more" class="button ghost" type="button" hidden>Show more</button>' in html
    assert f"const step = {SHOWN_ROWS}," in html
    (small,) = top_topics([feed(i, "x") for i in range(SHOWN_ROWS)], 1)
    assert 'id="more"' not in page(small)  # nothing more to show


def test_with_synonyms_maps_stored_topics_at_publish_time() -> None:
    synonyms = {"quantum-computing": "quantum", "quantum-physics": "quantum", "film": "movies"}
    feeds = [
        feed(1, "quantum-computing, rust, quantum-physics"),
        feed(2, "film"),
        feed(3, "rust"),
        feed(4, None),
    ]
    mapped = with_synonyms(feeds, synonyms)
    assert [f.topics for f in mapped] == ["quantum, rust", "movies", "rust", None]
    assert mapped[2] is feeds[2] and mapped[3] is feeds[3]  # untouched
    assert {t.name: len(t.feeds) for t in top_topics(mapped)} == {
        "rust": 2,
        "movies": 1,
        "quantum": 1,
    }


def test_preview_source_names_the_list() -> None:
    def source(url: str | None, license: str | None = None) -> object:
        doc = json.loads(render_preview(replace(feed(1), source=url, source_license=license)))
        return doc["source"]

    repo = "https://github.com/feedrule/indexer"
    survivors = {"name": "data/survivors/", "license": "Common Crawl terms"}
    assert source(f"{repo}/tree/develop/data/survivors", "LicenseRef-CommonCrawl-ToU") == survivors
    assert source(f"{repo}/blob/develop/data/survivors.txt", "x") == survivors  # its old file
    assert source("https://raw.githubusercontent.com/kagisearch/smallweb/main/smallweb.txt") == {
        "name": "kagisearch/smallweb",
        "license": "MIT",
    }
    unknown = "https://lists.example/feeds.opml"
    assert source(unknown, "CC0-1.0") == {"name": None, "license": "CC0-1.0"}
    assert source(None) is None
