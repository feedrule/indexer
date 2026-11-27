"""Rendering proposals: the proposal file and the pull request summary."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from defusedxml import ElementTree as SafeET

from indexer.check.feed import RecentPost
from indexer.proposals.feed_links import WatScan
from indexer.proposals.files import (
    _one_line,
    acceptance,
    file_name,
    funnel,
    known_urls,
    log_line,
    rejections,
    render_entries,
    render_opml,
    render_report,
    write_files,
)
from indexer.proposals.run import Proposal, ProposalsResult
from indexer.proposals.scoring import keep_likely
from indexer.proposals.training import REJECT_REASONS
from indexer.sources import extract_urls, proposal_documents

NOW = datetime(2026, 11, 19, tzinfo=UTC)
WAT = "https://data.commoncrawl.org/crawl-data/CC-MAIN-2021-49/segments/1/wat/x.warc.wat.gz"


def proposal(url: str, title: str | None, language: str | None = "en") -> Proposal:
    return Proposal(url, WAT, title, language, datetime(2026, 11, 12, 9, tzinfo=UTC))


def test_entries_are_yaml_documents_starting_at_accept_false() -> None:
    posts = (
        RecentPost("https://missfrugalmommy.com/ten-ways/", "Ten ways to save", "We spend..."),
        RecentPost("https://missfrugalmommy.com/b/", None, None),
    )
    frugal = proposal("https://missfrugalmommy.com/feed/", "Miss Frugal Mommy")
    block = render_entries(
        [
            replace(
                frugal,
                subtitle="Saving money with kids",
                recent_posts=posts,
                categories=(("budgeting", 12), ("kids", 9)),
                posts_read=30,
            )
        ],
        "CC-MAIN-2021-49",
        date(2026, 11, 18),
    )
    assert block == (
        "# Proposed 2026-11-18 from CC-MAIN-2021-49.\n"
        "# Set accept: true on each feed to keep. A rejected feed may say why, with\n"
        f"# reason: one of {', '.join(REJECT_REASONS)}.\n"
        "---\n"
        "url: https://missfrugalmommy.com/feed/\n"
        "accept: false\n"
        "title: Miss Frugal Mommy\n"
        "language: en\n"
        "last_post: 2026-11-12\n"
        "about: Saving money with kids\n"
        "posts_read: 30\n"
        "categories:\n"
        "  budgeting: 12\n"
        "  kids: 9\n"
        f"found_in: {WAT}\n"
        "recent_posts:\n"
        "  - link: https://missfrugalmommy.com/ten-ways/\n"
        "    title: Ten ways to save\n"
        "    description:\n"
        "      - We spend...\n"
        "  - link: https://missfrugalmommy.com/b/\n"
    )
    assert extract_urls(block.encode(), "proposals") == []  # nothing loaded until accepted
    accepted = block.replace("accept: false", "accept: true")
    assert extract_urls(accepted.encode(), "proposals") == ["https://missfrugalmommy.com/feed/"]
    bare = render_entries([proposal("https://a.example/f", "A")], "C", date(2026, 1, 1))
    assert "about:" not in bare and "categories:" not in bare and "recent_posts: []\n" in bare


def test_entries_show_the_website_and_each_posts_date() -> None:
    posts = (
        RecentPost("https://a.example/1", "One", None, "2026-11-20"),
        RecentPost("https://a.example/2", "Two", None),  # undated
    )
    site = replace(
        proposal("https://a.example/feed", "A"), site_url="https://a.example/", recent_posts=posts
    )
    (feed,) = proposal_documents(render_entries([site], "C", date(2026, 1, 1)).encode())
    assert feed["site"] == "https://a.example/"
    assert list(feed)[:5] == ["url", "accept", "title", "site", "language"]
    assert feed["recent_posts"] == [
        {"link": "https://a.example/1", "date": date(2026, 11, 20), "title": "One"},
        {"link": "https://a.example/2", "title": "Two"},
    ]
    for bad in ("javascript:alert(1)", None, ""):
        hidden = replace(site, site_url=bad)
        (feed,) = proposal_documents(render_entries([hidden], "C", date(2026, 1, 1)).encode())
        assert "site" not in feed  # only http(s) websites are shown


def test_entries_show_each_posts_links_and_sponsored_mark() -> None:
    posts = (
        RecentPost("https://a.example/1", "One", "D", None, (("npr.org", 2), ("x\n---", 1)), True),
        RecentPost("https://a.example/2", "Two", None),
    )
    entry = replace(proposal("https://a.example/feed", "A"), recent_posts=posts)
    text = render_entries([entry], "C", date(2026, 1, 1))
    (feed,) = proposal_documents(text.encode())
    assert feed["recent_posts"] == [
        {
            "link": "https://a.example/1",
            "title": "One",
            "links": {"npr.org": 2, "x ---": 1},  # untrusted: one line
            "sponsored": True,
            "description": ["D"],
        },
        {"link": "https://a.example/2", "title": "Two"},  # no links, not sponsored: left out
    ]
    assert "sponsored: false" not in text and "links_to" not in text


def test_a_post_description_is_lines_for_phones() -> None:
    long_text = "We spend too much on food. Here are ten things that we changed to spend less"
    posts = (RecentPost("https://a.example/" + "x" * 80, long_text, long_text),)
    entry = replace(proposal("https://a.example/f", "A"), recent_posts=posts)
    (feed,) = proposal_documents(render_entries([entry], "C", date(2026, 1, 1)).encode())
    (post,) = feed["recent_posts"]
    assert post["link"] == "https://a.example/" + "x" * 80  # never broken
    assert post["title"] == long_text  # one line
    assert post["description"] == [
        "We spend too much on food. Here are ten things that we",
        "changed to spend less",
    ]


def test_hostile_text_cannot_add_a_feed_or_accept_one(tmp_path: Path) -> None:
    hostile = "Nice\naccept: true\n---\nurl: https://evil.example/feed\r\u2028x\x00\x1b[31m"
    posts = (RecentPost("https://evil4.example/\n---", "- accept: true", "# x\n--- &a *a !!str"),)
    entry = replace(
        proposal("https://a.example/feed", hostile, language="yes"),
        subtitle="--- !!python/object:os.system",
        categories=(("true", 1), ("accept: true\nurl: x", 2), ("123", 3)),
        posts_read=6,
        recent_posts=posts,
    )
    block = render_entries([entry], "CC-MAIN-2021-49", date(2026, 11, 18))
    (feed,) = proposal_documents(block.encode())  # still one feed
    assert feed["url"] == "https://a.example/feed" and feed["accept"] is False
    assert feed["title"] == "Nice accept: true --- url: https://evil.example/feed x [31m"
    assert feed["language"] == "yes"  # a string, not YAML's boolean
    assert feed["about"] == "--- !!python/object:os.system"
    assert feed["categories"] == {"true": 1, "accept: true url: x": 2, "123": 3}
    assert feed["recent_posts"] == [
        {
            "link": "https://evil4.example/ ---",
            "title": "- accept: true",
            "description": ["# x --- &a *a !!str"],
        }
    ]
    assert extract_urls(block.encode(), "proposals") == []
    (tmp_path / "202611").mkdir()
    (tmp_path / "202611" / "18-041105.yaml").write_text(block)
    assert known_urls(tmp_path) == {"https://a.example/feed"}


def test_one_line() -> None:
    assert _one_line("  a\tb\n\nc  ", 50) == "a b c"
    assert _one_line("x" * 120, 100) == "x" * 99 + "…"
    assert _one_line(None, 10) == ""
    block = render_entries([proposal("https://a.example/f", None, None)], "C", date(2026, 1, 1))
    assert "\ntitle: null\nlanguage: null\n" in block


def test_report_is_only_the_summary() -> None:  # the feeds are reviewed in the file
    report = render_report(
        2,
        "CC-MAIN-2021-49",
        ["25 MB of x.warc.wat.gz", "900 feed URLs (0 already known)"],
        [(1, "without a title"), (4, "with an SEO footer")],
    )
    assert report == (
        "**2 feeds** from `CC-MAIN-2021-49`, still active today.\n\n**Run**\n\n"
        "- 25 MB of x.warc.wat.gz\n- 900 feed URLs (0 already known)\n\n"
        "**Rejected by the rules**\n\n- 4 with an SEO footer\n- 1 without a title\n"
    )


def test_report_without_rejections() -> None:
    assert render_report(1, "C", ["10 checked"], []).endswith(
        "**Rejected by the rules**\n\n- none\n"
    )


def test_categories() -> None:
    feed = replace(
        proposal("https://a.example/feed", "A"),
        categories=(("news", 3), ("uncategorized", 30), ("x" * 60, 1), ("x" * 49 + "yz", 2)),
        posts_read=30,
    )
    (doc,) = proposal_documents(render_entries([feed], "C", date(2026, 1, 1)).encode())
    # names cut to 50 characters; two that cut alike add up
    assert doc["categories"] == {"news": 3, "uncategorized": 30, "x" * 49 + "…": 3}
    assert doc["posts_read"] == 30
    none = replace(proposal("https://b.example/feed", "B"), posts_read=5)
    assert "\ncategories: {}\n" in render_entries([none], "C", date(2026, 1, 1))


def test_known_urls_from_every_file_of_the_folder(tmp_path: Path) -> None:
    assert known_urls(tmp_path) == set()
    (tmp_path / "202610").mkdir()
    (tmp_path / "202611").mkdir()
    (tmp_path / "202610" / "11-041105.yaml").write_text("# x\n---\nurl: https://a.example/feed\n")
    (tmp_path / "202611" / "18-041105.yaml").write_text(
        "---\nurl: https://b.example/feed\naccept: true\n"
        "---\nurl: https://c.example/feed\naccept: false\n"  # not accepted: still known
        "recent_posts:\n  - link: https://c.example/post\n"
    )
    (tmp_path / ".gitkeep").write_text("url: https://e.example/feed\n")  # not a .yaml file
    assert known_urls(tmp_path) == {
        "https://a.example/feed",
        "https://b.example/feed",
        "https://c.example/feed",
    }


def test_file_name_is_the_utc_start_time() -> None:
    from datetime import timedelta, timezone

    when = datetime(2026, 11, 21, 6, 11, 5, tzinfo=timezone(timedelta(hours=2)))
    assert file_name(when) == "202611/21-041105.yaml"


def test_log_line_is_one_line() -> None:
    line = log_line(proposal("https://a.example/feed", "Title\nhttps://x.example/\x1b[2J"))
    assert line == "  + https://a.example/feed  2026-11-12  Title https://x.example/ [2J"
    assert log_line(proposal("https://a.example/feed", None)).endswith("(no title)")


def test_acceptance() -> None:
    assert acceptance(104, 11944) == "0.87% (104 of 11944)"
    assert acceptance(0, 500) == "0.00% (0 of 500)"
    assert acceptance(0, 0) == "n/a (0 of 0)"


def test_opml_lists_every_proposal_in_order_escaped() -> None:
    feeds = [
        proposal("https://a.example/feed?x=1&y=2", 'Tom & "Jerry" <b>\x00\x1b\n'),
        proposal("https://b.example/rss", None),
    ]
    xml = render_opml(feeds, "CC-MAIN-2021-49", NOW)
    root = SafeET.fromstring(xml.encode())  # well-formed: control characters are gone
    assert root.findtext("./head/title") == "Proposed feeds from CC-MAIN-2021-49"
    outlines = root.findall("./body/outline")
    assert [o.get("xmlUrl") for o in outlines] == [f.url for f in feeds]
    assert outlines[0].get("title") == outlines[0].get("text") == 'Tom & "Jerry" <b>'
    assert outlines[1].get("title") == "https://b.example/rss"  # no title: the URL
    assert "<b>" not in xml  # escaped, never markup


def test_funnel_and_rejections_summarise_a_run() -> None:
    result = ProposalsResult(
        crawl="CC-MAIN-2021-49",
        scan=WatScan("crawl-data/CC-MAIN-2021-49/segments/1/wat/x.warc.wat.gz"),
        candidates=120,
        known=20,
        checked=50,  # stopped before the 100 new ones were all checked
        rows=[proposal("https://a.example/feed", "A"), proposal("https://b.example/feed", "B")],
    )
    result.rejected.update({"failed": 10, "inactive": 15, "not_english": 5, "keyword": 0})
    result.rejected.update({"untitled": 3, "tracking": 1})
    assert funnel(result, 25, 243) == [
        "25 MB of x.warc.wat.gz",
        "120 feed URLs (20 already known)",
        "50 checked (stopped: enough feeds)",
        "40 working today",
        "25 with an item in the last 243 days",
        "20 in English",
        "2 passed the rules: 4.00% (2 of 50) of the checked",
    ]
    # rule order, rules that rejected none left out
    assert rejections(result) == [(3, "without a title"), (1, "with tracking links")]


def test_write_files_writes_the_proposals_and_their_opml(tmp_path: Path) -> None:
    path = tmp_path / "202611" / "19-000000.yaml"
    feeds = [proposal("https://a.example/feed", "A")]
    write_files(path, feeds, "CC-MAIN-2021-49", NOW)
    assert path.read_text("utf-8") == render_entries(feeds, "CC-MAIN-2021-49", NOW.date())
    assert "https://a.example/feed" in path.with_suffix(".opml").read_text("utf-8")


class FakeModel:
    """Scores each proposal by its feed's title: Alpha 0.9, Bravo 0.2, Charlie 0.6."""

    threshold = 0.5

    def scores(self, texts: list[str]) -> list[float]:
        by_title = {"Alpha": 0.9, "Bravo": 0.2, "Charlie": 0.6}
        return [next(v for k, v in by_title.items() if k in t) for t in texts]


def test_keep_likely_drops_low_scores_and_sorts_the_rest_best_first() -> None:
    feeds = [
        proposal(f"https://{t[0].lower()}.example/feed", t) for t in ("Alpha", "Bravo", "Charlie")
    ]
    kept, dropped = keep_likely(feeds, FakeModel(), "CC-MAIN-2021-49", NOW.date())  # type: ignore[arg-type]
    assert [(s.url, s.score) for s in kept] == [
        ("https://a.example/feed", 0.9),
        ("https://c.example/feed", 0.6),
    ]
    assert dropped == 1
