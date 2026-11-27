"""Feed URLs from the sources config (no network: fetch is a fake)."""

from pathlib import Path

import pytest

from indexer.net.urls import valid_urls
from indexer.sources import (
    InputRow,
    Source,
    SourceError,
    SourcesConfig,
    collect_inputs,
    download,
    extract_urls,
    load_config,
    proposal_documents,
)

REPO_CONFIG = Path(__file__).parent.parent / "data" / "sources.toml"
TIERS = {"CC0-1.0": 0, "MIT": 1, "ISC": 1}


GOOD = """
[licenses]
ranking = [["CC0-1.0"], ["MIT", "ISC"]]


[[source]]
name = "a"
license = "MIT"
format = "opml"
urls = ["https://raw.githubusercontent.com/o/a/main/My%20List.opml"]
"""


URLS = 'urls = ["https://raw.githubusercontent.com/o/a/main/My%20List.opml"]'


EXCLUDE = frozenset({"youtube.com", "reddit.com"})
PLATFORM_URLS = [
    "https://www.youtube.com/feeds/videos.xml?channel_id=UC1",
    "https://old.reddit.com/r/rss/.rss",
    "https://reddit.com/.rss",
]
LOOKALIKES = [
    "https://notreddit.com/feed",
    "https://reddit.com.evil.example/feed",
    "https://blog.example/youtube.com/feed",
]


def raw(name: str, file: str = "list.txt") -> str:
    return f"https://raw.githubusercontent.com/o/{name}/main/{file}"


class FakeFiles:
    """Serves bodies by URL; an unknown URL fails the way the CLI's fetch does."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requested: list[str] = []

    async def __call__(self, url: str) -> bytes:
        self.requested.append(url)
        if url not in self.files:
            raise SourceError(f"{url}: http_4xx 404")
        return self.files[url]


def src(
    name: str, *, license: str = "MIT", urls: tuple[str, ...] | None = None, **kw: object
) -> Source:
    return Source(name=name, license=license, format="lines", urls=urls or (raw(name),), **kw)  # type: ignore[arg-type]


def cfg(*sources: Source) -> SourcesConfig:
    return SourcesConfig(sources=sources, license_tiers=TIERS)


# --- config ----------------------------------------------------------------------------------


def test_repo_config_is_valid() -> None:
    config = load_config(REPO_CONFIG)
    assert all(u.startswith("https://") for s in config.sources for u in s.urls)
    assert {s.license for s in config.sources} <= config.license_tiers.keys()


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sources.toml"
    path.write_text(text)
    return path


def test_load_config(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, GOOD))
    (source,) = config.sources
    assert source.urls == ("https://raw.githubusercontent.com/o/a/main/My%20List.opml",)
    assert (source.license, source.format) == ("MIT", "opml")
    assert config.license_tiers == {"CC0-1.0": 0, "MIT": 1, "ISC": 1}


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('format = "opml"', 'format = "csv"', "format"),
        (URLS, "urls = []", "needs 'urls' and/or 'files'"),
        (URLS, 'files = "x.txt"', "'files' must be a list"),
        (URLS, 'files = ["../../etc/passwd"]', "must stay inside"),
        (URLS, 'urls = "https://x.example/a"', "urls"),
        (URLS, 'urls = ["http://x.example/list.txt"]', "https"),
        ('license = "MIT"', "", "license"),
        ('license = "MIT"', 'license = "GPL-3.0"', "missing from"),
        ('ranking = [["CC0-1.0"], ["MIT", "ISC"]]', 'ranking = [["MIT"], ["MIT"]]', "twice"),
        ('ranking = [["CC0-1.0"], ["MIT", "ISC"]]', 'ranking = ["MIT"]', "lists of strings"),
        ('ranking = [["CC0-1.0"], ["MIT", "ISC"]]', "", "ranking"),
        ("[[source]]", '[exclude]\ndomains = "youtube.com"\n\n[[source]]', "exclude"),
        ("[[source]]", "[exclude]\ndomains = [1]\n\n[[source]]", "exclude"),
    ],
)
def test_bad_config(tmp_path: Path, old: str, new: str, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        load_config(write(tmp_path, GOOD.replace(old, new)))


# --- downloading -----------------------------------------------------------------------------


async def test_download_reads_every_file_of_a_source() -> None:
    files = FakeFiles(
        {
            raw("a", "1.txt"): b"https://one.example/f\n",
            raw("a", "2.txt"): b"https://two.example/f\n",
        }
    )
    downloaded, failures = await download(
        cfg(src("a", urls=(raw("a", "1.txt"), raw("a", "2.txt")))), files
    )
    assert failures == {}
    assert [(url, urls) for _, url, urls in downloaded] == [
        (raw("a", "1.txt"), ["https://one.example/f"]),
        (raw("a", "2.txt"), ["https://two.example/f"]),
    ]


async def test_failing_source_is_skipped_and_others_load() -> None:
    files = FakeFiles(
        {
            raw("good"): b"https://g.example/feed\n",
            raw("half", "1.txt"): b"https://h.example/feed\n",
            raw("broken"): b"<!DOCTYPE x [<!ENTITY e 'x'>]><opml/>",
        }
    )
    config = cfg(
        src("good"),
        src("gone"),
        src("half", urls=(raw("half", "1.txt"), raw("half", "missing.txt"))),
        Source(name="broken", license="MIT", format="opml", urls=(raw("broken"),)),
    )
    downloaded, failures = await download(config, files)
    # only the good source contributes; a source with one missing file contributes nothing
    assert [(s.name, urls) for s, _, urls in downloaded] == [("good", ["https://g.example/feed"])]
    assert set(failures) == {"gone", "half", "broken"}
    assert "404" in failures["gone"] and "missing.txt" in failures["half"]
    assert raw("broken") in failures["broken"]


# --- parsing ---------------------------------------------------------------------------------


def test_extract_opml() -> None:
    body = b"""<?xml version="1.0"?><opml version="2.0"><body>
      <outline text="Group"><outline text="A" xmlUrl=" https://a.example/feed "/></outline>
      <outline text="no feed" htmlUrl="https://b.example/"/>
      <outline text="C" xmlUrl="https://c.example/rss?x=1&amp;y=2"/>
    </body></opml>"""
    assert extract_urls(body, "opml") == [
        "https://a.example/feed",
        "https://c.example/rss?x=1&y=2",
    ]


def test_malformed_opml_with_bare_ampersand_is_scanned() -> None:
    body = b"""<opml><body>
      <outline text="A &amp; B" xmlUrl="https://a.example/rss?x=1&y=2" type="rss" />
      <outline text='C' xmlUrl='https://c.example/feed?a=1&amp;b=2'/>
    </body></opml>"""
    assert extract_urls(body, "opml") == [
        "https://a.example/rss?x=1&y=2",
        "https://c.example/feed?a=1&b=2",
    ]


@pytest.mark.parametrize(
    "body",
    [
        b'<!DOCTYPE opml [<!ENTITY e "x">]><opml><outline xmlUrl="https://a.example/&e;"/></opml>',
        b'<!DOCTYPE opml [<!ENTITY e "x">]><opml><outline xmlUrl="https://a.example/?a&b"/></opml>',
    ],
)
def test_opml_with_dtd_is_refused(body: bytes) -> None:
    with pytest.raises(SourceError):
        extract_urls(body, "opml")


def test_extract_lines() -> None:
    body = b"# comment\n\nhttps://a.example/feed\n  https://b.example/rss  \r\n"
    assert extract_urls(body, "lines") == ["https://a.example/feed", "https://b.example/rss"]


def test_extract_proposals_reads_only_accepted_feeds() -> None:
    body = (
        b"# header\n---\nurl: https://a.example/feed\naccept: true\n"
        b"---\nurl: https://b.example/rss\naccept: false\n"
        b"---\nurl: https://c.example/rss\naccept: 'true'\n"  # a string, not a yes
        b"---\nurl: https://d.example/rss\n"  # not reviewed
    )
    assert extract_urls(body, "proposals") == ["https://a.example/feed"]
    assert [f["url"] for f in proposal_documents(body)][1:] == [
        "https://b.example/rss",
        "https://c.example/rss",
        "https://d.example/rss",
    ]


@pytest.mark.parametrize(
    "body",
    [
        b"---\nurl: [unclosed\n",
        b"---\n- a list\n",
        b"---\ntitle: no url\n",
        b"---\nurl: !!python/object/apply:os.system [echo]\n",  # safe_load refuses tags
    ],
)
def test_a_malformed_proposal_file_is_refused(body: bytes) -> None:
    with pytest.raises(SourceError):
        extract_urls(body, "proposals")


# --- inputs ----------------------------------------------------------------------------------


def test_collect_inputs_credits_most_permissive_source() -> None:
    mit_a, isc, cc0 = src("mit-a"), src("isc", license="ISC"), src("cc0", license="CC0-1.0")
    downloaded = [
        (mit_a, raw("mit-a"), ["https://a.example/f", "https://Both.example/f", "bad url"]),
        (mit_a, raw("mit-a", "2.txt"), ["https://tie.example/f"]),
        (isc, raw("isc"), ["https://both.example/f", "https://tie.example/f"]),
        (cc0, raw("cc0"), ["https://BOTH.example/f", "https://c.example/f"]),
    ]
    result = collect_inputs(cfg(), downloaded)
    rows = {r.url: r for r in result.rows}
    assert set(rows) == {
        "https://a.example/f",
        "https://both.example/f",
        "https://tie.example/f",
        "https://c.example/f",
    }
    # found in MIT, ISC and CC0 lists: CC0 wins; the source is the file it was found in
    assert rows["https://both.example/f"] == InputRow(
        "https://both.example/f", raw("cc0"), "CC0-1.0"
    )
    assert rows["https://tie.example/f"] == InputRow(
        "https://tie.example/f", raw("mit-a", "2.txt"), "MIT"
    )
    assert result.invalid["mit-a"] == 1
    assert result.found == {"mit-a": 3, "isc": 2, "cc0": 2}
    assert result.attributed == {"mit-a": 2, "cc0": 2}


def test_submissions_file_is_valid() -> None:
    """data/submissions.txt (user-submitted feeds): every entry is a valid, unique URL."""
    path = REPO_CONFIG.parent / "submissions.txt"
    entries = extract_urls(path.read_bytes(), "lines")
    valid, invalid = valid_urls(entries)
    assert invalid == 0, "submissions.txt has invalid URLs"
    assert len(valid) == len(entries), "submissions.txt has duplicate URLs"
    config = load_config(REPO_CONFIG)
    (source,) = [s for s in config.sources if s.name == "submissions"]
    assert source.files == ("submissions.txt",) and not source.urls  # read from the checkout
    assert source is config.sources[-1]  # listed last: other lists keep credit on ties


def test_proposals_files_are_valid() -> None:
    """data/survivors/*/*.yaml (proposals from `indexer proposals`, merged after review): every
    feed says accept: true or false, every accepted one is a valid https URL, none twice across
    the files, and the source reads them all from the checkout."""
    paths = sorted((REPO_CONFIG.parent / "survivors").glob("*/*.yaml"))
    assert paths and not list((REPO_CONFIG.parent / "survivors").glob("*/*.txt"))
    for path in paths:
        feeds = proposal_documents(path.read_bytes())
        assert all(isinstance(f.get("accept"), bool) for f in feeds), path
    entries = [url for path in paths for url in extract_urls(path.read_bytes(), "proposals")]
    valid, invalid = valid_urls(entries)
    assert invalid == 0, "data/survivors/ has invalid URLs"
    assert len(valid) == len(entries), "data/survivors/ has duplicate URLs"
    assert all(url.startswith("https://") for url, _host in valid)
    config = load_config(REPO_CONFIG)
    (source,) = [s for s in config.sources if s.name == "survivors"]
    assert source.files == ("survivors/*/*.yaml",) and not source.urls
    assert source.format == "proposals"
    assert source.license == "LicenseRef-CommonCrawl-ToU"


async def test_local_file_source(tmp_path: Path) -> None:
    (tmp_path / "submitted.txt").write_text("# comment\nhttps://s.example/feed\n")
    source = Source(
        name="mine",
        license="MIT",
        format="lines",
        urls=(),
        files=("submitted.txt", "missing.txt"),
        homepage="https://github.com/o/r/blob/main/data/submitted.txt",
    )
    ok = Source(
        name="mine",
        license="MIT",
        format="lines",
        urls=(),
        files=("submitted.txt",),
        homepage="https://github.com/o/r/blob/main/data/submitted.txt",
    )
    files = FakeFiles({})
    config = SourcesConfig((ok,), TIERS, tmp_path)
    downloaded, failures = await download(config, files)
    assert failures == {} and files.requested == []  # nothing fetched over the network
    assert downloaded == [(ok, ok.homepage, ["https://s.example/feed"])]
    rows = collect_inputs(config, downloaded).rows
    assert rows == [InputRow("https://s.example/feed", ok.homepage, "MIT")]

    _, failures = await download(SourcesConfig((source,), TIERS, tmp_path), files)
    assert "missing.txt" in failures["mine"]


async def test_a_files_glob_reads_every_match(tmp_path: Path) -> None:
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "2.txt").write_text("https://b.example/feed\n")
    (tmp_path / "runs" / "1.txt").write_text("https://a.example/feed\n# https://c.example/feed\n")
    (tmp_path / "runs" / ".gitkeep").write_text("")
    glob = Source("runs", "MIT", "lines", (), ("runs/*.txt",), homepage="https://h.example/")
    downloaded, failures = await download(SourcesConfig((glob,), TIERS, tmp_path), FakeFiles({}))
    assert failures == {}
    assert [urls for _source, _where, urls in downloaded] == [
        ["https://a.example/feed"],  # sorted by name; a commented-out URL is not loaded
        ["https://b.example/feed"],
    ]
    empty = Source("none", "MIT", "lines", (), ("nothing/*.txt",))
    assert await download(SourcesConfig((empty,), TIERS, tmp_path), FakeFiles({})) == ([], {})
    (tmp_path / "outside.txt").write_text("https://x.example/feed\n")
    (tmp_path / "runs" / "link.txt").symlink_to(tmp_path / "outside.txt")
    nested = Source("runs", "MIT", "lines", (), ("*.txt",))
    _, failures = await download(SourcesConfig((nested,), TIERS, tmp_path / "runs"), FakeFiles({}))
    assert "outside" in failures["runs"]  # a symlink out of the config folder


def test_repo_config_excludes_youtube_and_reddit() -> None:
    config = load_config(REPO_CONFIG)
    assert {"youtube.com", "reddit.com"} <= config.exclude_domains
    assert not any("youtube" in u for s in config.sources for u in s.urls)


def test_collect_inputs_skips_excluded_domains() -> None:
    a = src("a")
    config = SourcesConfig((a,), TIERS, exclude_domains=EXCLUDE)
    result = collect_inputs(config, [(a, raw("a"), PLATFORM_URLS + LOOKALIKES)])
    assert sorted(r.url for r in result.rows) == sorted(LOOKALIKES)
    assert result.excluded["a"] == 3 and result.found["a"] == 3


def test_commoncrawl_exclude_keywords_loaded(tmp_path: Path) -> None:
    text = GOOD.replace(
        "[[source]]", '[commoncrawl]\nexclude_keywords = [" Porn "]\n\n[[source]]', 1
    )
    assert load_config(write(tmp_path, text)).commoncrawl_exclude_keywords == {"porn"}
    bad = GOOD.replace("[[source]]", '[commoncrawl]\nexclude_keywords = "porn"\n\n[[source]]', 1)
    with pytest.raises(SourceError, match="keywords"):
        load_config(write(tmp_path, bad))


def test_repo_config_excludes_adult_keywords() -> None:
    assert {"porn", "rape", "fetish"} <= load_config(REPO_CONFIG).commoncrawl_exclude_keywords


def test_exclude_domains_loaded(tmp_path: Path) -> None:
    text = GOOD.replace("[[source]]", '[exclude]\ndomains = ["YouTube.com."]\n\n[[source]]', 1)
    assert load_config(write(tmp_path, text)).exclude_domains == {"youtube.com"}
