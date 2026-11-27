"""A post's links for reviewers: other sites linked to, and affiliate or sponsored evidence."""

from datetime import UTC, datetime
from html import escape

from indexer.check.feed import RecentPost, parse_feed
from indexer.check.post_links import MAX_LINKED_SITES, PostLinks, post_links
from indexer.check.runner import CheckRow
from indexer.db.database import _health_check

POST = "https://blog.example/2026/11/post/"


def links(html: str) -> PostLinks:
    return post_links([html], POST)


def test_other_sites_by_registrable_domain_most_linked_first() -> None:
    html = (
        '<a href="https://www.npr.org/a">x</a><a href="https://text.npr.org/b">x</a>'
        '<a href="https://bbc.co.uk/c">x</a><a href="/own/relative">x</a>'
        '<a href="https://blog.example/about">x</a><a href="https://www.blog.example/">x</a>'
        '<a href="mailto:me@x.example">x</a><a href="javascript:alert(1)">x</a><a>no href</a>'
    )
    assert links(html) == PostLinks((("npr.org", 2), ("bbc.co.uk", 1)), False)


def test_shared_hosts_are_separate_sites() -> None:
    own = post_links(['<a href="https://other.blogspot.com/p">x</a>'], "https://me.blogspot.com/p")
    assert own.sites == (("other.blogspot.com", 1),)


def test_at_most_a_few_sites_ties_by_name() -> None:
    html = "".join(f'<a href="https://s{i}.example/">x</a>' for i in range(9))
    sites = links(html + '<a href="https://s8.example/2">x</a>').sites
    assert len(sites) == MAX_LINKED_SITES
    assert sites[0] == ("s8.example", 2) and sites[1] == ("s0.example", 1)


def test_sponsored_on_strict_evidence() -> None:
    for html in (
        '<a rel="nofollow sponsored" href="https://shop.example/x">x</a>',
        '<a href="https://www.shareasale.com/r.cfm?b=1">x</a>',
        '<a href="https://click.linksynergy.com/deeplink?id=1">x</a>',
        '<a href="https://amzn.to/3abc">x</a>',
        '<a href="https://www.amazon.co.uk/dp/B01?tag=me-21">x</a>',
        "<p>As an Amazon Associate I earn from qualifying purchases.</p>",
        "<p>This post contains affiliate links.</p>",
    ):
        assert links(html).sponsored, html
    for html in (
        '<a href="https://www.amazon.com/dp/B01">a book</a>',  # no tag: not an affiliate link
        '<a href="https://shop.example/x?aff=1">x</a>',  # generic parameters gave false matches
        '<a href="/go/product">x</a>',
        '<a rel="nofollow" href="https://shop.example/x">x</a>',
        "<p>The affiliate program of our university.</p>",
    ):
        assert not links(html).sponsored, html


def test_malformed_markup_keeps_what_was_read() -> None:
    assert links('<a href="https://npr.org/">x</a><a href="https://bbc.com/').sites == (
        ("npr.org", 1),
    )


def test_feeds_give_each_recent_post_its_links() -> None:
    content = escape('<p>See <a href="https://npr.org/x">this</a> and <a href="/y">mine</a></p>')
    rss = f"""<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel><title>t</title>
      <item><link>{POST}</link><description>Short excerpt</description>
        <content:encoded>{content}</content:encoded></item>
      <item><link>https://blog.example/2</link>
        <description>{escape('<a href="https://amzn.to/x">buy</a>')}</description></item>
      <item><link>https://blog.example/3</link><description>no links</description></item>
    </channel></rss>"""
    posts = parse_feed(rss.encode()).recent_posts
    assert [(p.links, p.sponsored) for p in posts] == [
        ((("npr.org", 1),), False),
        ((("amzn.to", 1),), True),
        ((), False),
    ]
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
      <entry><link href="https://blog.example/1"/><content type="xhtml">
        <div xmlns="http://www.w3.org/1999/xhtml"><a href="https://bbc.com/" rel="sponsored">x</a>
        </div></content></entry>
      <entry><link href="https://blog.example/2"/>
        <summary type="html">&lt;a href="https://npr.org/"&gt;x&lt;/a&gt;</summary></entry>
    </feed>"""
    posts = parse_feed(atom).recent_posts
    assert [(p.links, p.sponsored) for p in posts] == [
        ((("bbc.com", 1),), True),
        ((("npr.org", 1),), False),
    ]


def test_links_are_never_stored() -> None:
    post = RecentPost("https://a.example/1", "T", "D", "2026-11-20", (("npr.org", 2),), True)
    row = CheckRow(url="https://a.example/feed", checked_at=datetime.now(UTC), recent_posts=(post,))
    stored = _health_check(1, row).recent_posts
    assert stored == [
        {"link": "https://a.example/1", "title": "T", "description": "D", "date": "2026-11-20"}
    ]
