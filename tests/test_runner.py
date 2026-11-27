from collections.abc import Sequence

from aiohttp.test_utils import TestServer
from conftest import url

from indexer.check.runner import CheckRow, run_checks
from indexer.net.domains import registrable_domain
from indexer.net.fetch import FetchResult
from indexer.text.topics import Post


class ExplodingFetcher:
    async def fetch(self, raw_url: str) -> FetchResult:
        if "boom" in raw_url:
            raise RuntimeError("bug")
        return FetchResult(url=raw_url, http_status=200, body=b"<rss><channel/></rss>")


async def test_one_row_per_url_even_when_fetcher_crashes() -> None:
    urls = ["https://a.example/feed", "https://boom.example/feed", "https://c.example/x"]
    seen: list[CheckRow] = []

    async def on_row(row: CheckRow) -> None:
        seen.append(row)

    rows = await run_checks(urls, ExplodingFetcher(), on_row=on_row)
    assert [r.url for r in rows] == urls
    assert len(seen) == 3
    assert rows[1].error == "internal_error"
    assert rows[0].error is None and rows[0].item_count == 0


async def test_end_to_end_rows(make_fetcher, server: TestServer) -> None:
    urls = [
        url(server, "/feed.xml"),
        url(server, "/chain/2"),
        url(server, "/status/404"),
        "not a url",
        "http://127.0.0.2/",
        url(server, "/gzip-bomb"),
        url(server, "/moved-page"),
    ]
    async with make_fetcher() as fetcher:
        rows = await run_checks(urls, fetcher)
    by_url = {r.url: r for r in rows}
    assert len(rows) == len(urls)

    ok = by_url[urls[0]]
    assert (ok.error, ok.item_count, ok.ttl, ok.file_size) == (None, 3, 60, ok.file_size)
    assert ok.file_size and ok.file_size > 0
    assert ok.last_item_date is not None

    atom = by_url[urls[1]]
    assert atom.error is None and atom.redirected and atom.item_count == 2

    assert by_url[urls[2]].error == "http_4xx" and by_url[urls[2]].http_status == 404
    assert by_url[urls[3]].error == "invalid_url"
    assert by_url[urls[4]].error == "blocked_ip"
    assert by_url[urls[5]].error == "too_large"

    assert (ok.feed_kind, ok.site_url, ok.ip_address) == (
        "rss",
        "https://example.com/",
        "127.0.0.1",
    )
    assert atom.feed_kind == "atom" and atom.first_redirect_status == 302

    assert by_url[urls[6]].error == "not_a_feed"


def test_registrable_domain() -> None:
    assert registrable_domain("feeds.bbci.co.uk") == "bbci.co.uk"
    assert registrable_domain("www.example.com") == "example.com"
    assert registrable_domain("alice.github.io") == "alice.github.io"
    assert registrable_domain("Example.COM.") == "example.com"
    assert registrable_domain("8.8.8.8") is None
    assert registrable_domain(None) is None


class OneFeedFetcher:
    BODY = (
        "<rss><channel>"
        + "".join(
            f"<item><title>{t}</title><description>{'words ' * 10}</description>"
            f"<category>{t.split()[0]}</category></item>"
            for t in ("Rust tips", "Hallo Welt", "Rust news", "Hallo again", "Python notes")
        )
        + "</channel></rss>"
    ).encode()

    async def fetch(self, raw_url: str) -> FetchResult:
        return FetchResult(url=raw_url, http_status=200, body=self.BODY)


async def test_topics_come_from_the_english_items_only() -> None:
    class Languages:
        def post_languages(self, texts: Sequence[str]) -> list[str | None]:
            return ["de" if t.startswith("Hallo") else "en" for t in texts]

    class Model:
        def __init__(self) -> None:
            self.seen: list[Post] = []
            self.about: list[tuple[str | None, str | None]] = []
            self.first_label: list[bool] = []

        def topics(
            self,
            posts: Sequence[Post],
            title: str | None = None,
            subtitle: str | None = None,
            *,
            first_label: bool = True,
        ) -> list[str]:
            self.seen.extend(posts)
            self.about.append((title, subtitle))
            self.first_label.append(first_label)
            return ["rust", "python"]

    model = Model()
    urls = ["https://a.example/feed"]
    (row,) = await run_checks(urls, OneFeedFetcher(), languages=Languages(), topics=model)
    assert row.language == "de, en"
    assert [p.text.split(".")[0] for p in model.seen] == ["Rust tips", "Rust news", "Python notes"]
    assert [tuple(p.tags) for p in model.seen] == [("Rust",), ("Rust",), ("Python",)]
    assert model.about == [(None, None)]  # this feed has no title or subtitle
    assert model.first_label == [True]  # "en" is among the detected languages
    assert row.topics == "rust, python"  # stored comma separated

    (plain,) = await run_checks(urls, OneFeedFetcher())
    assert plain.language is None and plain.topics is None  # no language model: neither runs
    assert plain.item_texts[0].startswith("Rust tips.")  # kept on the row for proposals


async def test_no_first_label_fallback_unless_detected_english() -> None:
    class MostlyGerman:  # one English item of four: "en" is not among the feed's languages
        def post_languages(self, texts: Sequence[str]) -> list[str | None]:
            return ["en" if t.startswith("Python") else "de" for t in texts]

    class Model:
        def __init__(self) -> None:
            self.first_label: list[bool] = []

        def topics(
            self,
            posts: Sequence[Post],
            title: str | None = None,
            subtitle: str | None = None,
            *,
            first_label: bool = True,
        ) -> list[str]:
            self.first_label.append(first_label)
            return []

    model = Model()
    (row,) = await run_checks(
        ["https://a.example/feed"], OneFeedFetcher(), languages=MostlyGerman(), topics=model
    )
    assert row.language == "de" and model.first_label == [False] and row.topics is None
