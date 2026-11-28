from datetime import UTC, datetime
from html import escape as html_escape

import pytest
from conftest import FIXTURES

from indexer.check.feed import (
    MAX_POST_DESCRIPTION_LEN,
    MAX_POST_LINK_LEN,
    MAX_SUBTITLE_LEN,
    MAX_TEXT_LEN,
    FeedInfo,
    RecentPost,
    clean_url,
    parse_date,
    parse_feed,
)
from indexer.errors import CheckError, ErrorCode


def parse(name: str):
    return parse_feed((FIXTURES / name).read_bytes())


def error_of(body: bytes) -> ErrorCode:
    with pytest.raises(CheckError) as info:
        parse_feed(body)
    return info.value.code


def test_rss2() -> None:
    info = parse("rss2.xml")
    assert info.kind == "rss"
    assert info.item_count == 3
    assert info.ttl == 60
    assert info.last_item_date == datetime(2024, 3, 5, 8, 30, tzinfo=UTC)


def test_atom() -> None:
    info = parse("atom.xml")
    assert info.kind == "atom"
    assert info.item_count == 2
    assert info.ttl is None
    assert info.last_item_date == datetime(2025, 5, 20, tzinfo=UTC)


def test_rss10_rdf() -> None:
    info = parse("rss10.xml")
    assert (info.kind, info.item_count) == ("rdf", 2)
    assert info.last_item_date == datetime(2019, 4, 2, 10, tzinfo=UTC)


def test_rss091_with_external_doctype_is_parsed_without_loading_it() -> None:
    info = parse("rss091_doctype.xml")
    assert (info.kind, info.item_count, info.last_item_date) == ("rss", 1, None)


def test_billion_laughs_refused() -> None:
    assert error_of((FIXTURES / "billion_laughs.xml").read_bytes()) is ErrorCode.PARSE_ERROR


def test_external_entity_refused() -> None:
    assert error_of((FIXTURES / "xxe.xml").read_bytes()) is ErrorCode.PARSE_ERROR


def test_html_is_not_a_feed() -> None:
    assert error_of((FIXTURES / "page.html").read_bytes()) is ErrorCode.NOT_A_FEED


def test_other_xml_is_not_a_feed() -> None:
    assert error_of((FIXTURES / "sitemap.xml").read_bytes()) is ErrorCode.NOT_A_FEED


@pytest.mark.parametrize("body", [b'{"version": "https://jsonfeed.org/version/1"}', b"", b"hello"])
def test_non_markup_is_not_a_feed(body: bytes) -> None:
    assert error_of(body) is ErrorCode.NOT_A_FEED


def test_broken_xml_is_parse_error() -> None:
    assert error_of((FIXTURES / "broken.xml").read_bytes()) is ErrorCode.PARSE_ERROR


def test_rss_without_channel() -> None:
    assert error_of(b"<rss version='2.0'/>") is ErrorCode.NOT_A_FEED


@pytest.mark.parametrize("ttl", ["abc", "-5", "", "99999999999"])
def test_bad_ttl_ignored(ttl: str) -> None:
    body = f"<rss><channel><ttl>{ttl}</ttl></channel></rss>".encode()
    assert parse_feed(body).ttl is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mon, 01 Jan 2024 10:00:00 +0000", datetime(2024, 1, 1, 10, tzinfo=UTC)),
        ("Mon, 01 Jan 2024 10:00:00 -0000", datetime(2024, 1, 1, 10, tzinfo=UTC)),
        ("2024-01-01T10:00:00Z", datetime(2024, 1, 1, 10, tzinfo=UTC)),
        ("2024-01-01", datetime(2024, 1, 1, tzinfo=UTC)),
        ("yesterday", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(text: str | None, expected: datetime | None) -> None:
    assert parse_date(text) == expected


def test_rss_hints() -> None:
    body = b"""<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"
        xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
      <link>https://example.com/</link>
      <atom:link rel="self" href="/feed.xml" type="application/rss+xml"/>
      <itunes:new-feed-url>https://new.example/podcast.xml</itunes:new-feed-url>
      <generator>  WordPress 6.4
      </generator>
    </channel></rss>"""
    info = parse_feed(body, "https://old.example/blog/rss")
    assert info.kind == "rss"
    assert info.site_url == "https://example.com/"
    assert info.self_url == "https://old.example/feed.xml"  # relative, resolved
    assert info.new_feed_url == "https://new.example/podcast.xml"
    assert info.generator == "WordPress 6.4"


def test_atom_hints() -> None:
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <link href="https://example.com/atom.xml" rel="self"/>
      <link href="https://example.com/"/>
      <generator uri="https://jekyllrb.com/" version="4.3.2">Jekyll</generator>
    </feed>"""
    info = parse_feed(body)
    assert (info.site_url, info.self_url) == (
        "https://example.com/",
        "https://example.com/atom.xml",
    )
    assert info.generator == "Jekyll 4.3.2"
    assert info.new_feed_url is None


def test_rdf_generator_and_link() -> None:
    body = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns="http://purl.org/rss/1.0/" xmlns:admin="http://webns.net/mvcb/">
      <channel><link>http://example.org/</link>
        <admin:generatorAgent rdf:resource="http://www.movabletype.org/?v=3.2"/></channel>
    </rdf:RDF>"""
    info = parse_feed(body)
    assert info.site_url == "http://example.org/"
    assert info.generator == "http://www.movabletype.org/?v=3.2"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("javascript:alert(1)", None),
        ("file:///etc/passwd", None),
        ("https://a.example/\nx", None),
        ("https://a.example/\x00", None),
        ("https://a.example/" + "a" * 3000, None),
        ("  https://a.example/  ", "https://a.example/"),
        ("", None),
    ],
)
def test_clean_url(value: str, expected: str | None) -> None:
    assert clean_url(value) == expected


@pytest.mark.parametrize("encoding", ["Shift_JIS", "EUC-JP", "GB2312", "Big5", "EUC-KR"])
def test_multi_byte_declared_encodings_parse(encoding: str) -> None:
    """Expat refuses these encodings ("multi-byte encodings are not supported"); the body is
    re-encoded to UTF-8 with Python's codecs and parsed again."""
    title = {"Big5": "繁體中文", "GB2312": "简体中文", "EUC-KR": "한국어"}.get(
        encoding, "日本語のブログ"
    )
    xml = (
        f'<?xml version="1.0" encoding="{encoding}"?>\n'
        f"<rss><channel><title>{title}</title>"
        "<item><pubDate>Mon, 09 Nov 2026 10:00:00 GMT</pubDate><category>news</category></item>"
        "</channel></rss>"
    )
    info = parse_feed(xml.encode(encoding))
    assert info.title == title
    assert info.last_item_date == datetime(2026, 11, 9, 10, tzinfo=UTC)


def test_unknown_declared_encoding_is_a_parse_error() -> None:
    body = b'<?xml version="1.0" encoding="x-no-such-charset"?><rss><channel/></rss>'
    assert error_of(body) == ErrorCode.PARSE_ERROR


def test_reencoding_keeps_the_xml_protections() -> None:
    bomb = (
        '<?xml version="1.0" encoding="Shift_JIS"?>'
        '<!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss>&b;</rss>'
    ).encode("shift_jis")
    assert error_of(bomb) == ErrorCode.PARSE_ERROR


def links(info: FeedInfo) -> tuple[str, ...]:
    return tuple(post.link for post in info.recent_posts)


def test_recent_post_links_as_written_distinct_in_order() -> None:
    rss = b"""<rss version="2.0"><channel><title>t</title>
      <item><link> https://a.example/1 </link></item>
      <item><link>/relative/2</link></item>
      <item><link>https://a.example/1</link></item>
      <item><title>no link</title></item>
      <item><link>javascript:alert(1)</link></item>
    </channel></rss>"""
    assert links(parse_feed(rss)) == (
        "https://a.example/1",
        "/relative/2",  # as written: not resolved
        "javascript:alert(1)",  # not validated: shown to reviewers, never fetched
    )
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
      <entry><link rel="enclosure" href="https://a.example/a.mp3"/>
             <link href="https://a.example/post"/></entry>
      <entry><link rel="alternate" href="https://spam.example/x"/></entry>
    </feed>"""
    assert links(parse_feed(atom)) == ("https://a.example/post", "https://spam.example/x")
    rdf = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns="http://purl.org/rss/1.0/"><channel><title>t</title></channel>
      <item><link>https://a.example/r</link></item></rdf:RDF>"""
    assert links(parse_feed(rdf)) == ("https://a.example/r",)


def test_recent_posts_are_the_three_most_recent_items() -> None:
    rss = b"""<rss version="2.0"><channel><title>t</title>
      <item><link>https://a.example/old</link>
        <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
      <item><link>https://a.example/undated</link></item>
      <item><link>https://a.example/newest</link>
        <pubDate>Sun, 01 Sep 2024 00:00:00 GMT</pubDate></item>
      <item><link>https://a.example/mid</link>
        <pubDate>Mon, 01 Apr 2024 00:00:00 GMT</pubDate></item>
      <item><link>https://a.example/new</link>
        <pubDate>Mon, 01 Jul 2024 00:00:00 GMT</pubDate></item>
    </channel></rss>"""
    assert links(parse_feed(rss)) == (
        "https://a.example/newest",
        "https://a.example/new",
        "https://a.example/mid",
    )
    undated = b"""<rss version="2.0"><channel><title>t</title>
      <item><link>https://a.example/1</link></item><item><link>https://a.example/2</link></item>
    </channel></rss>"""
    assert links(parse_feed(undated)) == ("https://a.example/1", "https://a.example/2")


def test_recent_posts_carry_their_newest_date_as_a_utc_day() -> None:
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
      <entry><link href="https://a.example/1"/><published>2026-11-01T23:30:00-02:00</published>
        <updated>2026-11-03T10:00:00Z</updated></entry>
      <entry><link href="https://a.example/2"/></entry>
    </feed>"""
    assert [p.date for p in parse_feed(atom).recent_posts] == ["2026-11-03", None]
    rss = b"""<rss version="2.0"><channel><title>t</title>
      <item><link>https://a.example/x</link><pubDate>Sun, 01 Nov 2026 23:30:00 -0200</pubDate>
      </item></channel></rss>"""
    assert parse_feed(rss).recent_posts[0].date == "2026-11-02"  # 01:30 UTC


def test_recent_post_links_are_capped() -> None:
    items = "".join(
        f"<item><link>https://a.example/{i}/{'x' * 2 * MAX_POST_LINK_LEN}</link></item>"
        for i in range(150)
    )
    info = parse_feed(f'<rss version="2.0"><channel>{items}</channel></rss>'.encode())
    assert len(info.recent_posts) == 3 and all(
        len(p.link) == MAX_POST_LINK_LEN for p in info.recent_posts
    )


def test_recent_posts_have_a_title_and_the_start_of_the_description() -> None:
    rss = b"""<rss version="2.0"><channel><title>t</title>
      <item><title>First &amp; best</title><link>https://a.example/1</link>
        <description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description></item>
      <item><link>https://a.example/2</link></item>
    </channel></rss>"""
    assert parse_feed(rss).recent_posts == (
        RecentPost("https://a.example/1", "First & best", "Hello world"),  # markup stripped
        RecentPost("https://a.example/2", None, None),
    )
    text = "x" * 3 * MAX_POST_DESCRIPTION_LEN
    item = f"<item><link>https://a.example/</link><description>{text}</description></item>"
    long = f'<rss version="2.0"><channel>{item}</channel></rss>'
    (post,) = parse_feed(long.encode()).recent_posts
    assert post.description is not None and len(post.description) == MAX_POST_DESCRIPTION_LEN


def test_script_and_style_contents_are_not_text() -> None:
    description = (
        "<style>.sourceCode { overflow: visible; }</style><p>Benchmarks</p>"
        "<SCRIPT type='text/javascript'>!function(r,u){}()</SCRIPT> mislead"
        "<noscript>Enable JavaScript</noscript> us <script>never closed(){"
    )
    rss = f"""<rss version="2.0"><channel><title>t</title>
      <item><title>A post</title><link>https://a.example/1</link>
        <description>{html_escape(description)}</description></item>
    </channel></rss>"""
    info = parse_feed(rss.encode())
    assert info.recent_posts[0].description == "Benchmarks mislead us"
    assert info.item_texts == ("A post. Benchmarks mislead us",)


def test_a_post_shows_its_full_content_over_an_excerpt() -> None:
    full = "<p>The whole post, with every paragraph of it.</p><p>And a second one.</p>"
    rss = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel><title>t</title>
      <item><title>A</title><link>https://a.example/1</link>
        <description>The whole post [&#8230;]</description>
        <content:encoded>{html_escape(full)}</content:encoded></item>
      <item><title>B</title><link>https://a.example/2</link>
        <description>Only an excerpt</description></item>
    </channel></rss>"""
    posts = parse_feed(rss.encode()).recent_posts
    assert posts[0].description == "The whole post, with every paragraph of it. And a second one."
    assert posts[1].description == "Only an excerpt"
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
      <entry><title>A</title><link href="https://a.example/1"/><summary>Short</summary>
        <content type="html">&lt;p&gt;Much longer text&lt;/p&gt;</content></entry>
    </feed>"""
    assert parse_feed(atom).recent_posts[0].description == "Much longer text"


def test_a_longer_limit_than_the_default_keeps_more_text() -> None:
    long = "y " * MAX_SUBTITLE_LEN
    rss = f'<rss version="2.0"><channel><title>{long}</title><description>{long}</description>'
    info = parse_feed(f"{rss}</channel></rss>".encode())
    assert info.title is not None and len(info.title) == MAX_TEXT_LEN
    assert info.subtitle is not None and len(info.subtitle) == MAX_SUBTITLE_LEN > MAX_TEXT_LEN
