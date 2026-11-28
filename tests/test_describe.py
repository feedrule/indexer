"""Descriptive fields used for labelling: title, subtitle, media type, item texts..."""

from conftest import FIXTURES

from indexer.check.feed import MAX_ITEM_TEXT_LEN, MAX_TEXT_ITEMS, parse_feed

RSS_FULL = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:cc="http://backend.userland.com/creativeCommonsRssModule">
<channel>
  <title>  CSS &amp; More  </title>
  <description>&lt;p&gt;Notes on &lt;b&gt;CSS&lt;/b&gt;&lt;/p&gt;</description>
  <language>en_US</language>
  <category>web, CSS</category>
  <category>Accessibility</category>
  <category>css</category>
  <dc:subject>Browsers</dc:subject>
  <itunes:category text="Technology"><itunes:category text="Software"/></itunes:category>
  <itunes:category text="Education"/>
  <image><url>/logo.png</url></image>
  <copyright>(c) 2024 Someone</copyright>
  <cc:license>https://creativecommons.org/licenses/by/4.0/</cc:license>
  <item><title>Ep 1</title><enclosure url="https://cdn.example/1.mp3" type="audio/mpeg"/>
    <category>Node.js</category><category>c++</category></item>
  <item><title>Ep 2</title><enclosure url="https://cdn.example/2.m4a" type=""/></item>
  <item><title>Post</title><category>node.js, Web Dev</category>
    <dc:subject>Audio_Engineering</dc:subject></item>
</channel></rss>"""


def test_rss_description() -> None:
    info = parse_feed(RSS_FULL, "https://blog.example/feed")
    assert info.title == "CSS & More"
    assert info.subtitle == "Notes on CSS"
    assert info.item_texts == ("Ep 1", "Ep 2", "Post")  # categories are not read
    assert info.media_type == "podcast"  # 2 of 3 items carry audio (one by extension)
    assert info.logo == "https://blog.example/logo.png"
    assert info.rights == "(c) 2024 Someone"
    assert info.license == "https://creativecommons.org/licenses/by/4.0/"
    assert info.icon is None


def test_atom_description() -> None:
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="de-DE">
      <title type="html">A &lt;em&gt;tiny&lt;/em&gt; blog</title>
      <subtitle type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Short <b>notes</b></div>
      </subtitle>
      <category term="rust"/><category term="WASM" label="WebAssembly"/><category label="x"/>
      <icon>/favicon.ico</icon><logo>https://a.example/logo.svg</logo>
      <rights>CC BY-SA</rights>
      <link rel="license" href="https://creativecommons.org/licenses/by-sa/4.0/"/>
      <entry><title>t</title></entry>
    </feed>"""
    info = parse_feed(body, "https://a.example/atom.xml")
    assert info.title == "A tiny blog"
    assert info.subtitle == "Short notes"
    assert info.item_texts == ("t",)
    assert info.item_tags == ((),)  # the feed's own categories are not an item's
    assert info.icon == "https://a.example/favicon.ico"
    assert info.logo == "https://a.example/logo.svg"
    assert info.rights == "CC BY-SA"
    assert info.license == "https://creativecommons.org/licenses/by-sa/4.0/"
    assert info.media_type == "text"


def test_youtube_feed_is_video() -> None:
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"
        xmlns:yt="http://www.youtube.com/xml/schemas/2015"
        xmlns:media="http://search.yahoo.com/mrss/">
      <title>Channel</title>
      <entry><yt:videoId>abc</yt:videoId></entry>
      <entry><media:group>
        <media:content url="https://x.example/v" type="application/x-shockwave-flash"
                       medium="video"/>
      </media:group></entry>
    </feed>"""
    assert parse_feed(body).media_type == "video"


def test_empty_feed_has_no_media_type() -> None:
    assert parse_feed(b"<rss><channel><title>t</title></channel></rss>").media_type is None


def test_rdf_description() -> None:
    info = parse_feed((FIXTURES / "rss10.xml").read_bytes())
    assert info.kind == "rdf"
    assert info.title  # the fixture has a channel title
    assert info.media_type == "text"


def test_long_and_dirty_values_are_capped_and_cleaned() -> None:
    title = "T\u200bitle\n\t" + "x" * 500
    body = f"<rss><channel><title>{title}</title><image><url>javascript:x</url></image>"
    body += "</channel></rss>"
    info = parse_feed(body.encode())
    assert info.title is not None and len(info.title) == 200 and info.title.startswith("T itle")
    assert info.logo is None


def test_item_texts_title_and_description_as_plain_text() -> None:
    body = b"""<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
      <item><title>Hello &amp; welcome</title>
        <description>&lt;p&gt;First &lt;b&gt;post&lt;/b&gt;&lt;/p&gt;</description></item>
      <item><title>Only content</title>
        <content:encoded>&lt;p&gt;Body&lt;/p&gt;</content:encoded></item>
      <item><description>No title</description></item>
      <item><title> </title></item>
    </channel></rss>"""
    info = parse_feed(body)
    assert info.item_texts == ("Hello & welcome. First post", "Only content. Body", "No title")


def test_item_texts_atom_summary_or_content() -> None:
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>A</title><summary>short</summary></entry>
      <entry><title>B</title><content type="html">&lt;i&gt;long&lt;/i&gt;</content></entry>
    </feed>"""
    assert parse_feed(body).item_texts == ("A. short", "B. long")


def test_item_texts_capped_in_number_and_length() -> None:
    items = "".join(f"<item><title>{i} {'x' * 1000}</title></item>" for i in range(50))
    texts = parse_feed(f"<rss><channel>{items}</channel></rss>".encode()).item_texts
    assert len(texts) == MAX_TEXT_ITEMS and texts[0].startswith("0 ")
    assert all(len(t) <= MAX_ITEM_TEXT_LEN for t in texts)


def test_item_tags_per_item_as_written() -> None:
    body = b"""<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
      <item><title>A</title><category>Web Dev, CSS</category><dc:subject>css</dc:subject>
        <category>  </category><category>CSS</category></item>
      <item><title>B</title></item>
    </channel></rss>"""
    info = parse_feed(body)
    assert info.item_tags == (("Web Dev", "CSS", "css"), ())  # split, trimmed, distinct as written
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>t</title><category term="rust"/><category label="WebAssembly"/></entry>
    </feed>"""
    assert parse_feed(atom).item_tags == (("rust", "WebAssembly"),)


def test_item_authors_names_only() -> None:
    body = b"""<rss xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
      <item><title>A</title><author>noreply@blogger.com (Jo Bloggs)</author></item>
      <item><title>B</title><dc:creator><![CDATA[ admin ]]></dc:creator></item>
      <item><title>C</title><author>jo@example.org</author></item>
      <item><title>D</title><itunes:author>The Show</itunes:author></item>
      <item><title>E</title></item>
    </channel></rss>"""
    assert parse_feed(body).item_authors == ("Jo Bloggs", "admin", None, "The Show", None)
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><author><name>Feed Owner</name></author>
      <entry><title>t</title><author><name>Ann</name><email>a@x.org</email></author></entry>
      <entry><title>u</title></entry>
    </feed>"""
    assert parse_feed(atom).item_authors == ("Ann", "Feed Owner")  # an entry inherits the feed's
