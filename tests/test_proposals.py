"""Proposals: reading WAT files, the rules, the pipeline (synthetic files, no network)."""

import gzip
import json
import os
import random
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from indexer.check.feed import RecentPost
from indexer.check.runner import CheckRow
from indexer.proposals.feed_links import filter_page_links, is_unwanted, scan_wat
from indexer.proposals.filters import (
    Rules,
    _has_keyword,
    _is_active,
    _is_english,
    rejection,
    served_url,
)
from indexer.proposals.run import _CHECK_BATCH, propose
from indexer.proposals.wat import (
    _INDEX_URL,
    DATA_URL,
    CommonCrawlError,
    Fetch,
    _crawl_near,
    _wat_paths,
    complete_prefix,
)

CRAWL = "CC-MAIN-2026-43"
PATH = f"crawl-data/{CRAWL}/segments/1/wat/CC-MAIN-1-00001.warc.wat.gz"


NOW = datetime(2026, 11, 16, tzinfo=UTC)
OLD = "CC-MAIN-2021-49"


KEYWORDS = frozenset({"porn", "sex", "rape", "fetish"})


def record(body: bytes, warc_type: str = "metadata") -> bytes:
    head = f"WARC/1.0\r\nWARC-Type: {warc_type}\r\nContent-Length: {len(body)}\r\n\r\n"
    return head.encode() + body + b"\r\n\r\n"


def page(url: str, links: list[dict[str, str]] | None, html: bool = True) -> bytes:
    response: dict[str, object] = {}
    if html:
        response["HTML-Metadata"] = {"Head": {"Link": links or []}}
    envelope = {
        "WARC-Header-Metadata": {"WARC-Target-URI": url},
        "Payload-Metadata": {"HTTP-Response-Metadata": response},
    }
    return record(json.dumps({"Envelope": envelope}).encode())


def feed_link(href: str, kind: str = "application/rss+xml") -> dict[str, str]:
    return {"path": "LINK@/href", "url": href, "rel": "alternate", "type": kind}


def wat(*records: bytes) -> bytes:
    # Like Common Crawl: one gzip member per record, concatenated.
    return b"".join(gzip.compress(r) for r in records)


def test_scan_wat_collects_every_advertised_feed() -> None:
    data = wat(
        record(b"warcinfo", warc_type="warcinfo"),
        page(
            "https://blog.example/2026/11/post/",
            [
                feed_link("/feed/"),
                feed_link("https://blog.example/comments/feed/"),
                {"url": "/style.css", "rel": "stylesheet", "type": "text/css"},
            ],
        ),
        page("https://blog.example/about/", [feed_link("https://blog.example/feed/")]),
        page("https://www.other.example/", [feed_link("rss.xml", "application/atom+xml")]),
        page("https://shop.example/", []),
        page("https://site.example/data.json", None, html=False),
        page("https://www.youtube.com/@x", [feed_link("/feeds/videos.xml?channel_id=1")]),
        page("http://bad.example/", [feed_link("ftp://bad.example/feed")]),
        record(b"{not json"),
    )
    result = scan_wat(data, PATH, frozenset({"youtube.com"}))
    assert result.pages == 6  # HTML pages only: the JSON capture is not counted
    assert result.pages_with_feed == 3  # with a feed worth checking
    assert result.page_feeds == [  # per page; no comment feed, no YouTube, no ftp
        ["https://blog.example/feed/"],
        ["https://blog.example/feed/"],
        ["https://www.other.example/rss.xml"],  # relative href resolved
    ]


def test_filter_page_links() -> None:
    page_url = "https://s.example/blog/post/"
    links = [
        feed_link("/feed/"),
        feed_link("http://s.example/atom.xml"),  # tried on https
        feed_link("feed/"),  # relative to the page: the post's own feed, two levels deep
        feed_link("/comments/feed/"),
        feed_link("/category/news/feed/"),
        feed_link("https://board.example/rss.xml?sid=abc"),  # one URL once stripped
        feed_link("https://forum.example/feed/"),  # a forum
        feed_link("https://wiki.example/feed.php"),  # DokuWiki
        feed_link("https://board.example/app.php/feed"),  # phpBB
        feed_link("https://board.example/syndication.php?type=atom1.0"),  # MyBB
        feed_link("https://board.example/index.php?type=rss;action=.xml"),  # SMF
        feed_link("https://club.example/activity/feed/"),  # BuddyPress
        feed_link("/2021/04/03/feed/"),  # a dated post
        feed_link("/sitemap.xml"),
        feed_link("/a-very-long-path-segment-indeed/feed/"),  # over 25 characters
        feed_link("/a/b/c/d/e/f/g/feed/"),  # over 7 slashes
        feed_link("/news-and-more"),  # no feed word
        feed_link("/spip.php?page=backend"),  # SPIP
        feed_link("https://x.blogspot.com/feeds/posts/default"),  # Blogger
        feed_link("/?feed=rss2"),  # WordPress without pretty links
        feed_link("/index.php?format=feed&type=atom"),  # Joomla
        feed_link("/backend.php"),
        feed_link("/spip.php?page=article&id=3"),  # not the feed page
        feed_link("https://www.youtube.com/feeds/videos.xml"),
        feed_link("ftp://s.example/feed"),
        feed_link("   "),
        {"url": "/rss/", "rel": "alternate", "type": "Application/RSS+XML; charset=utf-8"},
        {"url": "/rss2/", "rel": "stylesheet", "type": "application/rss+xml"},
        {"url": "/style.css", "rel": "stylesheet", "type": "text/css"},
        "not a link",
    ]
    assert filter_page_links(page_url, links, frozenset({"youtube.com"})) == [
        "https://s.example/feed/",
        "https://s.example/atom.xml",
        "https://board.example/rss.xml",
        "https://s.example/spip.php?page=backend",
        "https://x.blogspot.com/feeds/posts/default",
        "https://s.example/?feed=rss2",
        "https://s.example/index.php?format=feed&type=atom",
        "https://s.example/backend.php",
        "https://s.example/rss/",
    ]
    assert filter_page_links(page_url, None) == []


def test_the_page_s_own_feed_only_beside_another() -> None:
    post = "http://s.example/my-post/"
    assert filter_page_links(post, [feed_link("/feed/"), feed_link("/my-post/feed/")]) == [
        "https://s.example/feed/"  # the post's comments are dropped
    ]
    blog = "https://s.example/blog/"
    assert filter_page_links(blog, [feed_link("/blog/feed/")]) == ["https://s.example/blog/feed/"]
    home = "https://s.example/"
    assert filter_page_links(home, [feed_link("/feed/"), feed_link("/rss.xml")]) == [
        "https://s.example/feed/",
        "https://s.example/rss.xml",
    ]
    stories = [feed_link("/feed/"), feed_link("/web-stories/feed/")]
    assert filter_page_links(home, stories) == ["https://s.example/feed/"]


def test_url_rules_ignore_case() -> None:
    home = "https://s.example/"
    assert filter_page_links(home, [feed_link("/RSS")]) == ["https://s.example/RSS"]  # as written
    assert filter_page_links(home, [feed_link("/Feeds/Posts/Default")]) != []
    assert filter_page_links(home, [feed_link("/blog/Comments/feed/")]) == []
    assert filter_page_links(home, [feed_link("/?Feed=Comments-rss2")]) == []


def test_wat_paths_keeps_only_well_formed_paths_of_the_crawl() -> None:
    listing = "\n".join(
        [PATH, "crawl-data/CC-MAIN-2026-38/segments/1/wat/x.warc.wat.gz", "../../etc/passwd", ""]
    )
    assert _wat_paths(gzip.compress(listing.encode()), CRAWL) == [PATH]
    with pytest.raises(CommonCrawlError):
        _wat_paths(gzip.compress(b"nothing useful"), CRAWL)


def test_crawl_near_picks_the_crawl_that_ended_closest() -> None:
    body = json.dumps(
        [
            {"id": CRAWL, "to": "2026-10-31T00:00:00"},
            {"id": OLD, "to": "2021-12-09T00:00:00"},
            {"id": "CC-MAIN-2021-43", "to": "2021-10-28T00:00:00"},
            {"id": "CC-MAIN-2020-50", "name": "no dates"},
        ]
    ).encode()
    assert _crawl_near(body, datetime(2021, 11, 25, tzinfo=UTC)) == OLD
    with pytest.raises(CommonCrawlError):
        _crawl_near(b"[]", NOW)
    with pytest.raises(CommonCrawlError):
        _crawl_near(json.dumps([{"id": "../x", "to": "2021-01-01"}]).encode(), NOW)
    with pytest.raises(CommonCrawlError):
        _crawl_near(json.dumps([{"id": OLD, "to": "not a date"}]).encode(), NOW)


def test_complete_prefix_drops_a_record_cut_off_by_the_range() -> None:
    whole = wat(page("https://a.example/", [feed_link("/feed")]), page("https://b.example/", []))
    assert complete_prefix(whole) == whole
    cut = whole[:-10]
    first = gzip.compress(page("https://a.example/", [feed_link("/feed")]))
    assert complete_prefix(cut) == first
    assert complete_prefix(whole[:5]) == b""
    assert complete_prefix(b"") == b""
    assert scan_wat(complete_prefix(cut), PATH).page_feeds == [["https://a.example/feed"]]


def test_complete_prefix_with_records_larger_than_a_chunk() -> None:
    big = os.urandom(200_000)  # incompressible: a gzip member of ~200 KB spans several chunks
    members = [gzip.compress(big), gzip.compress(b"small"), gzip.compress(big)]
    whole = b"".join(members)
    assert complete_prefix(whole) == whole
    assert complete_prefix(whole[:-1]) == members[0] + members[1]
    assert complete_prefix(members[0][:-1]) == b""


def test_complete_prefix_is_linear() -> None:
    # 50,000 records in 2 MB; the old version copied the remainder for each one (quadratic).
    data = b"".join(gzip.compress(b"record %d" % i) for i in range(50_000))
    started = time.perf_counter()
    assert complete_prefix(data + data[:-3]) == data + data[: data.rfind(b"\x1f\x8b")]
    assert time.perf_counter() - started < 5


def checked(
    url: str,
    days_ago: float | None,
    error: str | None = None,
    final_url: str | None = None,
    language: str | None = "en",
    title: str | None = "A Feed",
    subtitle: str | None = "About things",
) -> CheckRow:
    date = None if days_ago is None else NOW - timedelta(days=days_ago)
    return CheckRow(
        url,
        NOW,
        final_url=final_url,
        last_item_date=date,
        error=error,
        language=language,
        title=title,
        subtitle=subtitle,
        recent_posts=(RecentPost(url + "#post", "A post", "Some text"),),
    )


def test_is_active() -> None:
    assert _is_active(checked("u", 10), NOW, 365)
    assert _is_active(checked("u", 365), NOW, 365)
    assert not _is_active(checked("u", 366), NOW, 365)
    assert not _is_active(checked("u", None), NOW, 365)  # no dated items
    assert not _is_active(checked("u", 1, error="http_5xx"), NOW, 365)
    assert _is_active(checked("u", -0.5), NOW, 365)  # time zones: up to a day ahead is fine
    assert not _is_active(checked("u", -3), NOW, 365)  # a future date is not trusted


@pytest.mark.parametrize(
    ("language", "english"),
    [
        ("en", True),
        ("de, en", False),  # English must be the only language
        ("fr", False),
        (None, False),  # nothing detected
    ],
)
def test_is_english(language: str | None, english: bool) -> None:
    assert _is_english(checked("u", 1, language=language)) is english


@pytest.mark.parametrize(
    ("title", "excluded"),
    [
        ("Captured Porn Passwords", True),
        ("BestCutScenes: death fetish", True),
        ("Sex-toys shop", True),  # hyphen and underscore separate words
        ("adult_porn", True),
        ("Essex Gardening", False),  # inside another word
        ("Pornography studies", False),  # a different word: list each form
        (None, False),
    ],
)
def test_has_keyword(title: str | None, excluded: bool) -> None:
    row = CheckRow("u", NOW, title=title)
    assert _has_keyword(row, KEYWORDS) is excluded
    assert not _has_keyword(row, frozenset())


def test_has_keyword_three_times_in_the_subtitle_and_posts() -> None:
    def row(subtitle: str, *posts: str) -> CheckRow:
        return CheckRow("u", NOW, title="A blog", subtitle=subtitle, item_texts=posts)

    assert not _has_keyword(row("Porn studies", "On porn and film"), KEYWORDS)  # twice
    assert _has_keyword(row("Porn studies", "On porn and film", "Rape culture"), KEYWORDS)
    assert _has_keyword(row("", "sex sex sex"), KEYWORDS)  # every occurrence counts
    assert not _has_keyword(row("Essex", "Essex news", "Essex sport"), KEYWORDS)  # not a word
    assert not _has_keyword(row("", "sex sex sex"), frozenset())


@pytest.mark.parametrize(
    "url",
    [
        # WordPress category, tag and author feeds
        "https://gazetashqiptare.al/category/bota/rajoni/feed/",
        "https://x.example/tag/python/feed",
        "https://x.example/author/jane/feed/atom/",
        "https://x.example/blog/category/news/feed/rss2/",
        # comment feeds
        "https://x.example/comments/feed/",
        "https://x.example/blog/comments/feed",
        "https://x.example/comments/feed/atom/",
        "https://x.example/?feed=comments-rss2",
        "https://x.example/index.php?p=1&feed=comments-atom",
        "https://x.blogspot.com/feeds/comments/default",
        "https://x.blogspot.com/feeds/123456789/comments/default?alt=rss",
    ],
)
def test_archive_and_comment_feeds_are_dropped(url: str) -> None:
    assert filter_page_links("https://x.example/", [feed_link(url)]) == []


def test_served_url_is_https_after_redirects() -> None:
    assert served_url(checked("https://a.example/feed", 1)) == "https://a.example/feed"
    moved = checked("http://a.example/feed", 1, final_url="https://a.example/feed/")
    assert served_url(moved) == "https://a.example/feed/"
    assert served_url(checked("http://a.example/feed", 1)) is None  # still plain http
    down = checked("https://a.example/feed", 1, final_url="http://a.example/feed")
    assert served_url(down) is None
    with_sid = checked("http://f.example/feed?sid=abc", 1, final_url="https://f.example/feed?sid=x")
    assert served_url(with_sid) == "https://f.example/feed"
    to_yt = checked("https://a.example/feed", 1, final_url="https://www.youtube.com/feeds/x")
    assert served_url(to_yt, frozenset({"youtube.com"})) is None


async def test_proposals_checks_every_link_and_keeps_the_shortest_url_per_site() -> None:
    paths = [f"crawl-data/{OLD}/segments/1/wat/CC-MAIN-1-{i:05}.warc.wat.gz" for i in range(3)]
    slice_ = wat(
        page("https://s.example/", [feed_link("/feed/"), feed_link("/comments/feed/")]),
        page("https://s.example/a/", [feed_link("/category/news/feed/")]),  # nothing to pick
        page("https://s.example/b/", [feed_link("/feed/")]),  # the same pick: fetched once
        page("https://s.example/old/", [feed_link("/old/feed/")]),
        page("http://h.example/", [feed_link("/rss")]),  # tried on https, which redirects to http
        page("http://m0.example/", [feed_link("/feed")]),  # m0 and m1 redirect to one URL
        page("http://m1.example/", [feed_link("/feed")]),
        page("https://fr.example/", [feed_link("/fr/feed")]),
        page("https://board.example/", [feed_link("/feed?sid=1")]),  # one URL once stripped
        page("https://board.example/x", [feed_link("/feed?sid=2")]),
        page("https://a.free.fr/", [feed_link("/feed/")]),  # two users of one shared host
        page("https://b.free.fr/", [feed_link("/feed/")]),
        page("https://x.example/", [feed_link("https://feeds.feedburner.com/x")]),  # not a shape
        page("https://blog.example/", [feed_link("/feed/")]),  # shorter: kept
        page("https://blog.example/p/", [feed_link("/posts/feed/")]),
        page("https://multi.example/", [feed_link("/a/feed"), feed_link("/b/feed")]),  # both
        page("https://shared.example/", [feed_link("/feed/")]),  # no dated items
        page("https://adult.example/", [feed_link("/feed/")]),  # excluded by keyword
        page("https://untitled.example/", [feed_link("/feed/")]),  # no title
        page("https://www.youtube.com/@x", [feed_link("/feeds/videos.xml")]),
        page("https://cut.example/", [feed_link("/feed/")]),  # cut off by the range
    )
    collinfo = [
        {"id": CRAWL, "to": "2026-10-31T00:00:00"},
        {"id": OLD, "to": "2021-12-09T00:00:00"},
    ]
    served = {
        _INDEX_URL: json.dumps(collinfo).encode(),
        f"{DATA_URL}crawl-data/{OLD}/wat.paths.gz": gzip.compress("\n".join(paths).encode()),
    }
    sliced: list[str] = []

    async def fetch(url: str) -> bytes:
        return served[url]

    async def fetch_slice(url: str) -> bytes:
        sliced.append(url)
        return slice_[:-7]

    asked: list[str] = []

    async def check(urls: Sequence[str]) -> list[CheckRow]:
        asked.extend(urls)
        rows = []
        for url in urls:
            if "/old/" in url:
                rows.append(checked(url, 400))  # newest item too old
            elif "shared" in url:
                rows.append(checked(url, None))
            elif "/fr/" in url:
                rows.append(checked(url, 3, language="fr"))
            elif "untitled" in url:
                rows.append(checked(url, 3, title=None))
            elif "adult" in url:
                row = checked(url, 3)
                row.title = "Free Porn Logins"
                rows.append(row)
            elif "//m0." in url or "//m1." in url:  # both redirect to one URL
                rows.append(checked(url, 3, final_url="https://new.example/feed"))
            elif "//h." in url:  # https redirects back to plain http
                rows.append(checked(url, 3, final_url="http://h.example/rss"))
            else:
                rows.append(checked(url, 30))
        return rows

    result = await propose(
        fetch,
        fetch_slice,
        check,
        years_ago=5,
        active_days=365,
        exclude=frozenset({"youtube.com"}),
        keywords=KEYWORDS,
        rng=random.Random(1),
        now=NOW,
    )
    assert result.crawl == OLD
    assert len(sliced) == 1 and sliced[0].removeprefix(DATA_URL) in paths  # one random file
    assert result.scan.path == sliced[0].removeprefix(DATA_URL)
    assert not any("/category/" in u or "/comments/" in u for u in asked)
    assert result.candidates == 16 == len(asked) == len(set(asked))
    assert all(u.startswith("https://") for u in asked)
    assert "https://board.example/feed" in asked
    assert {"https://multi.example/a/feed", "https://multi.example/b/feed"} <= set(asked)
    assert not any("youtube" in u or "cut.example" in u or "feedburner" in u for u in asked)
    assert result.working == 16
    assert result.rejected == {
        "inactive": 2,  # old, shared
        "not_english": 1,
        "untitled": 1,
        "keyword": 1,
        "not_https": 1,  # h.example
    }
    urls = sorted(r.url for r in result.rows)
    assert urls[:4] == [
        "https://a.free.fr/feed/",
        "https://b.free.fr/feed/",
        "https://blog.example/feed/",
        "https://board.example/feed",
    ]
    assert urls[4:] == [
        "https://multi.example/a/feed",  # as short as /b/feed: by name
        "https://new.example/feed",
        "https://s.example/feed/",
    ]
    for row in result.rows:
        assert row.wat_url == sliced[0]  # credited to the WAT file it was found in
        assert row.language == "en"
        days = 3 if row.url == "https://new.example/feed" else 30  # as the fake check said
        assert row.last_item_date == NOW - timedelta(days=days)


async def test_proposals_fails_when_the_slice_fails() -> None:
    path = f"crawl-data/{OLD}/segments/1/wat/CC-MAIN-1-00000.warc.wat.gz"
    served = {
        _INDEX_URL: json.dumps([{"id": OLD, "to": "2021-12-09T00:00:00"}]).encode(),
        f"{DATA_URL}crawl-data/{OLD}/wat.paths.gz": gzip.compress(path.encode()),
    }

    async def fetch(url: str) -> bytes:
        return served[url]

    async def busy(url: str) -> bytes:
        raise CommonCrawlError("http_5xx 503")

    async def check(urls: Sequence[str]) -> list[CheckRow]:
        raise AssertionError("nothing to check")

    with pytest.raises(CommonCrawlError, match="http_5xx 503"):  # no retries, no other file
        await propose(fetch, busy, check, years_ago=5, active_days=365, now=NOW)


async def test_proposals_stops_once_enough_feeds_pass() -> None:
    fetch, fetch_slice = _one_slice(_CHECK_BATCH * 3)
    asked: list[str] = []

    async def check(urls: Sequence[str]) -> list[CheckRow]:
        asked.extend(urls)
        return [checked(u, 3) for u in urls]  # every feed passes

    result = await propose(
        fetch, fetch_slice, check, years_ago=5, active_days=365, max_feeds=10, now=NOW
    )
    assert len(result.rows) == 10
    assert len(asked) == result.checked == _CHECK_BATCH  # one batch was enough
    assert result.candidates == _CHECK_BATCH * 3


async def test_proposals_checks_at_most_10_feeds_per_site_shortest_first() -> None:
    links = [feed_link(f"/rss/{'s' * i}") for i in range(1, 13)]  # 12 section feeds
    path = f"crawl-data/{OLD}/segments/1/wat/CC-MAIN-1-00000.warc.wat.gz"
    served = {
        _INDEX_URL: json.dumps([{"id": OLD, "to": "2021-12-09T00:00:00"}]).encode(),
        f"{DATA_URL}crawl-data/{OLD}/wat.paths.gz": gzip.compress(path.encode()),
    }
    data = wat(
        page("https://news.example/", links), page("https://b.example/", [feed_link("/feed")])
    )

    async def fetch(url: str) -> bytes:
        return served[url]

    async def fetch_slice(url: str) -> bytes:
        return data

    asked: list[str] = []

    async def check(urls: Sequence[str]) -> list[CheckRow]:
        asked.extend(urls)
        return [checked(u, 3) for u in urls]

    result = await propose(fetch, fetch_slice, check, years_ago=5, active_days=365, now=NOW)
    news = sorted(u for u in asked if "news." in u)
    assert news == sorted(f"https://news.example/rss/{'s' * i}" for i in range(1, 11))
    assert "https://b.example/feed" in asked
    assert result.candidates == 13 and result.over_site_limit == 2


async def test_proposals_skips_known_urls_before_and_after_checking() -> None:
    fetch, fetch_slice = _one_slice(3)
    asked: list[str] = []

    async def check(urls: Sequence[str]) -> list[CheckRow]:
        asked.extend(urls)
        # s1 now redirects to a feed that is already an input
        return [
            checked(u, 3, final_url="https://moved.example/feed" if "s1." in u else None)
            for u in urls
        ]

    async def known(urls: Sequence[str]) -> set[str]:
        return {u for u in urls if u in {"https://s0.example/feed", "https://moved.example/feed"}}

    result = await propose(
        fetch, fetch_slice, check, years_ago=5, active_days=365, known=known, now=NOW
    )
    assert "https://s0.example/feed" not in asked  # known before checking: never fetched
    assert [r.url for r in result.rows] == ["https://s2.example/feed"]
    assert result.known == 2


def test_simple_junk_rules() -> None:
    rules = Rules(NOW, 365)

    def judged(
        subtitle: str | None = "About things", *posts: RecentPost, texts: str = ""
    ) -> object:
        row = checked("https://s.example/feed/", 3, subtitle=subtitle)
        row.recent_posts = posts or row.recent_posts
        row.item_texts = (texts,) if texts else ()
        return rejection(row, rules)

    def post(link: str, description: str | None = None) -> RecentPost:
        return RecentPost(link, "A post", description)

    assert judged() is None
    assert judged(None) is None  # no description is fine: many good blogs have none
    assert judged("x", post("https://w.example/doku.php?id=a&rev=1&do=diff")) == "wiki"
    assert judged("x", post("https://w.example/index.php?title=A&oldid=3")) == "wiki"
    assert judged("x", post("https://s.example/p/?utm_source=rss&utm_medium=rss")) == "tracking"
    footer = "Great news. The post Great news appeared first on Other Site."
    assert judged("x", post("https://s.example/p/", footer)) == "seo_footer"
    assert judged("x", texts="Big day. This article first appeared on Elsewhere") == "seo_footer"
    assert judged("x", post("https://s.example/p/", "It appeared on our first day.")) is None


def test_keywords_ignore_accents() -> None:
    assert _has_keyword(
        CheckRow("u", NOW, title="Come è organizzato un casinò online"), frozenset({"casino"})
    )
    assert _has_keyword(CheckRow("u", NOW, title="CASINÒ"), frozenset({"casinò"}))


def test_wordpress_sample_post() -> None:
    row = checked("https://s.example/feed/", 3)
    row.recent_posts = (
        RecentPost("https://s.example/?p=1", "Hello world!", "Welcome to WordPress. This is your "
                   "first post. Edit or delete it, then start writing!"),
    )  # fmt: skip
    assert rejection(row, Rules(NOW, 365)) == "sample_post"


def test_free_domains_are_dropped_unchecked() -> None:
    links = [feed_link("https://maps.example.tk/rss.xml"), feed_link("https://a.example/feed/")]
    assert filter_page_links("https://a.example/", links) == ["https://a.example/feed/"]


def test_wordpress_formats_count_as_the_main_feed() -> None:
    links = [
        feed_link("https://a.example/feed/atom/"),
        feed_link("https://b.example/feed/rss2"),
        feed_link("https://c.example/blog/feed/rdf/"),
        feed_link("https://d.example/feed/"),
        feed_link("https://d.example/feed/atom/"),  # the same feed twice: kept once
    ]
    assert filter_page_links("https://x.example/", links) == [
        "https://a.example/feed/",
        "https://b.example/feed/",
        "https://c.example/blog/feed/",
        "https://d.example/feed/",
    ]


def test_repeated_posts() -> None:
    def judged(*posts: RecentPost) -> object:
        row = checked("https://s.example/feed/", 3)
        row.recent_posts = posts
        return rejection(row, Rules(NOW, 365))

    same = RecentPost("https://s.example/p-5/", "Invoicing errors", "PUE, PPD and REP explained")
    assert judged(same, same._replace(link="https://s.example/p-6/")) == "repeated"
    weekly = RecentPost("https://s.example/w-1/", "Friday Night Extravaganza!", "This week: A")
    assert judged(weekly, weekly._replace(link="https://s.example/w-2/", description="B")) is None
    untitled = RecentPost("https://s.example/1", None, "Same text")
    assert judged(untitled, untitled._replace(link="https://s.example/2")) is None


def test_keywords_count_recent_post_titles() -> None:
    row = checked("https://s.example/feed/", 3)
    row.recent_posts = (
        RecentPost("https://s.example/1", "Rape culture on campus", None),
        RecentPost("https://s.example/2", "Fetish fashion", None),
        RecentPost("https://s.example/3", "Porn studies", None),
    )
    assert _has_keyword(row, KEYWORDS)


def test_seo_summaries() -> None:
    def judged(*descriptions: str) -> object:
        row = checked("https://s.example/feed/", 3)
        row.recent_posts = tuple(
            RecentPost(f"https://s.example/{i}", f"Post {i}", d) for i, d in enumerate(descriptions)
        )
        return rejection(row, Rules(NOW, 365))

    farm = ("Explore legal cannabis options in BC.", "Learn about car title loans in Ocala.", "x")
    assert judged(*farm) == "seo_summary"
    assert judged("Discover how AI helps triage.", "We went hiking.", "A quiet week.") is None
    assert judged("Learning to knit, week 3.", "Explorers of the Arctic.", "x") is None  # words


def test_title_echo_and_reposts() -> None:
    def judged(*posts: tuple[str, str]) -> object:
        row = checked("https://s.example/feed/", 3)
        row.recent_posts = tuple(
            RecentPost(f"https://s.example/{i}", t, d) for i, (t, d) in enumerate(posts)
        )
        return rejection(row, Rules(NOW, 365))

    echo = ("BiP was first AMPylated in vitro", "BiP was first AMPylated  in vitro. helix and")
    other = ("2a) A control", "2a) a control, we all also proven that")
    assert judged(echo, other, ("x", "y")) == "title_echo"
    assert judged(echo, ("x", "y"), ("z", "w")) is None  # one post only
    reposted = ("Keach dies at 85", "(Carmel Dagan's article appeared in Variety.) Stacy Keach")
    assert judged(reposted, ("x", "y")) == "reposted"


def test_posts_without_any_text() -> None:
    row = checked("https://s.example/feed/", 3)
    row.recent_posts = (RecentPost("https://s.example/1", None, None),)
    assert rejection(row, Rules(NOW, 365)) == "no_text"
    row.recent_posts = (RecentPost("https://s.example/1", None, None),
                        RecentPost("https://s.example/2", "A title", None))  # fmt: skip
    assert rejection(row, Rules(NOW, 365)) is None


@pytest.mark.parametrize(
    ("url", "unwanted"),
    [
        ("https://forums.veeam.com/feed", True),
        ("https://wimsbios.com/forum/feed", True),
        ("https://pregame.com/pregame-forums/rss", True),
        ("https://lib.example/recent.rss", True),  # Digital Commons
        ("https://hightechforum.org/feed/", False),  # "forum" inside a word
        ("https://magforum.wordpress.com/feed/", False),
        ("https://tal.forum2.org/xml/rss", False),
        ("https://www.gagadaily.com/rss/1-bandsintown.xml/", True),  # Invision Community
        ("https://sims4studio.com/rss/public", True),
        ("https://blog.example/feed/", False),
    ],
)
def test_unwanted_urls(url: str, unwanted: bool) -> None:
    assert is_unwanted(url) is unwanted


def test_a_redirect_to_an_unwanted_url_is_rejected() -> None:
    row = checked(
        "https://www.celestialheavens.com/forum/feed",
        3,
        final_url="https://www.celestialheavens.com/forums/app.php/feed",
    )
    assert rejection(row, Rules(NOW, 365)) == "unwanted_url"


@pytest.mark.parametrize(
    ("title", "subtitle", "business"),
    [
        ("UK Document Services", "direct to your door", True),
        ("MONDOM communications", "Boutique Communications Agency", True),
        ("My Shop...granny likes it", "Irish-made gifts", True),
        ("TBJ Incorporated", "World Leader of Custom Stainless Steel Equipment", True),
        ("MLT Aikins", "Western Canada's Law Firm", True),
        ("93.9 WKYS", "Washington DC's Hip-Hop & R&B Radio Station", True),
        ("UU Miami", "Unitarian Universalist Congregation of Miami", True),
        ("Dogtown Media", "Mobile App Development Company", True),
        ("Workshop notes", "Woodworking at home", False),  # inside a word
        ("A Feed", "About things", False),
    ],
)
def test_business_words(title: str, subtitle: str, business: bool) -> None:
    row = checked("https://s.example/feed/", 3, title=title, subtitle=subtitle)
    assert (rejection(row, Rules(NOW, 365)) == "business") is business


def test_job_boards() -> None:
    row = checked("https://jobs.example/rss/", 3, subtitle="Jobs from Joblink | Leeds")
    assert rejection(row, Rules(NOW, 365)) == "job_board"


def test_top_categories() -> None:
    from indexer.proposals.run import _top_categories

    tags = (("Recipes", "Kids"), ("recipes",), ("Kids", "kids"), (), ("  ",))
    assert _top_categories(tags) == (("kids", 2), ("recipes", 2))  # once per post, case ignored


def test_top_authors() -> None:
    from indexer.proposals.run import _top_authors

    authors = ("Jo", "admin", "jo", None, "Admin", "Jo", "Ann")
    assert _top_authors(authors) == (("Jo", 3), ("admin", 2), ("Ann", 1))  # case ignored


def test_keywords_count_categories() -> None:
    row = checked("https://s.example/feed/", 3, title="I Love Discipline")
    row.item_tags = (("otk", "punishment"), ("otk",), ("otk",))
    assert _has_keyword(row, frozenset({"otk"}))  # a category on three posts
    row.item_tags = (("otk",),)
    assert not _has_keyword(row, frozenset({"otk"}))  # once is not enough


def _one_slice(feeds: int) -> tuple[Fetch, Fetch]:
    """fetch and fetch_slice serving one WAT file with `feeds` sites, each advertising a feed."""
    path = f"crawl-data/{OLD}/segments/1/wat/CC-MAIN-1-00000.warc.wat.gz"
    served = {
        _INDEX_URL: json.dumps([{"id": OLD, "to": "2021-12-09T00:00:00"}]).encode(),
        f"{DATA_URL}crawl-data/{OLD}/wat.paths.gz": gzip.compress(path.encode()),
    }
    data = wat(*(page(f"https://s{i}.example/", [feed_link("/feed")]) for i in range(feeds)))

    async def fetch(url: str) -> bytes:
        return served[url]

    async def fetch_slice(url: str) -> bytes:
        return data

    return fetch, fetch_slice
