"""`indexer proposals`: feeds advertised in a ~5-year-old Common Crawl crawl that are still
active today, proposed for review.

    1. pick the crawl closest to `years_ago` and one random WAT file of it      wat.py
    2. download the file's first slice and list the feeds its pages advertise,  wat.py,
       minus those the URL rules skip                                          feed_links.py
    3. drop the URLs we already know; at most 10 per site, the shortest first
    4. check them in random order, a batch at a time, keep those that pass      filters.py
       and, of those on one site, the shortest URL
    5. stop once `max_feeds` sites have one

The fetching, checking and "do we know this URL" functions are passed in (the CLI gives the real
ones, tests give fakes).
"""

import logging
import random
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from indexer.check.feed import RecentPost
from indexer.check.runner import CheckRow
from indexer.proposals.feed_links import WatScan, scan_wat
from indexer.proposals.filters import (
    Reason,
    Rules,
    rejection,
    served_url,
)
from indexer.proposals.wat import DATA_URL, Fetch, complete_prefix, pick_wat_file

# Progress of a run: `indexer proposals` shows it (INFO) on stderr.
log = logging.getLogger("indexer.proposals")

_CHECK_BATCH = 200  # feeds checked at a time, so a run can stop once it has enough
_MAX_CATEGORIES = 8  # categories shown per proposed feed
_MAX_AUTHORS = 5  # authors shown per proposed feed
_MAX_PER_SITE = 10  # feeds checked per site, shortest URLs first (the shortest that passes wins)

type _Check = Callable[[Sequence[str]], Awaitable[Iterable[CheckRow]]]
# Which of these URLs are already proposed or in the inputs table.
type _Known = Callable[[Sequence[str]], Awaitable[set[str]]]


@dataclass(frozen=True, slots=True)
class Proposal:
    """A feed proposed for review: the URL to add, and what a reviewer needs to judge it."""

    url: str  # as served today (https, after redirects)
    wat_url: str  # the WAT file it was advertised in
    title: str | None
    language: str | None  # detected on the items: "en, fr"
    last_item_date: datetime
    subtitle: str | None = None
    recent_posts: tuple[RecentPost, ...] = ()  # newest first (untrusted text)
    categories: tuple[tuple[str, int], ...] = ()  # the authors' categories: (name, posts), top
    posts_read: int = 0  # posts whose categories were counted
    score: float | None = None  # probability of being accepted (training.py's model)
    site_url: str | None = None  # the website the feed names (untrusted, never fetched)
    authors: tuple[tuple[str, int], ...] = ()  # the posts' authors: (name, posts), top
    future_dated: bool = False  # some post dated in the future (that date ignored)


@dataclass(slots=True)
class ProposalsResult:
    crawl: str
    scan: WatScan  # the WAT slice that was read
    candidates: int = 0  # distinct feed URLs
    known: int = 0  # already proposed or in inputs (before or after redirects): skipped
    over_site_limit: int = 0  # new, but beyond a site's _MAX_PER_SITE shortest: not checked
    checked: int = 0  # fewer than candidates when the run stopped early
    rejected: Counter[Reason] = field(default_factory=Counter)  # checked, failed a rule
    rows: list[Proposal] = field(default_factory=list)  # passed every rule: the proposals

    # The funnel, for the summary: each count is the previous one minus a rejection reason.
    @property
    def working(self) -> int:
        return self.checked - self.rejected["failed"]

    @property
    def active(self) -> int:
        return self.working - self.rejected["inactive"]

    @property
    def english(self) -> int:
        return self.active - self.rejected["not_english"]


async def propose(
    fetch: Fetch,
    fetch_slice: Fetch,
    check: _Check,
    *,
    years_ago: int,
    active_days: int,
    max_feeds: int | None = None,
    known: _Known | None = None,
    exclude: frozenset[str] = frozenset(),
    keywords: frozenset[str] = frozenset(),
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> ProposalsResult:
    """Runs steps 1-5 above. `fetch_slice` is the range request for the WAT file; `known` (if
    given) drops URLs we already have; `exclude` holds excluded domains, `keywords` excluded
    title words. At most `max_feeds` proposals are returned (all of them if None)."""
    rng = rng or random.Random()
    now = now or datetime.now(UTC)
    known = known or _nothing_known
    rules = Rules(now, active_days, exclude, keywords)

    # 1-2. One WAT slice and the feeds it advertises.
    target = now - timedelta(days=round(365.25 * years_ago))  # no Feb 29 surprise
    crawl, path = await pick_wat_file(fetch, target, rng)
    wat_url = DATA_URL + path
    log.info(
        "crawl %s (ended closest to %s): downloading the start of %s", crawl, target.date(), wat_url
    )
    data = await fetch_slice(wat_url)
    scan = scan_wat(complete_prefix(data), path, exclude)
    log.info(
        "%.1f MB: %d pages, %d advertise a feed",
        len(data) / 1e6,
        scan.pages,
        scan.pages_with_feed,
    )
    result = ProposalsResult(crawl, scan)

    # 3. The URLs worth checking.
    to_check = await _candidates(scan.page_feeds, result, known, rng)
    log.info(
        "%d candidates (%d already known, %d beyond %d per site), %d to check",
        result.candidates,
        result.known,
        result.over_site_limit,
        _MAX_PER_SITE,
        len(to_check),
    )

    # 4-5. Check a batch at a time until enough pass.
    by_site: dict[str, Proposal] = {}
    for start in range(0, len(to_check), _CHECK_BATCH):
        batch = to_check[start : start + _CHECK_BATCH]
        result.checked += len(batch)
        new = [s for row in await check(batch) if (s := _judge(row, rules, wat_url, result))]
        for proposal in await _unknown(new, known, result):
            site = _site(proposal.url)
            best = by_site.get(site)
            if best is None or (len(proposal.url), proposal.url) < (len(best.url), best.url):
                by_site[site] = proposal
        log.info("checked %d of %d: %d sites pass", result.checked, len(to_check), len(by_site))
        if max_feeds is not None and len(by_site) >= max_feeds:
            break
    result.rows = list(by_site.values())[:max_feeds]
    return result


async def _candidates(
    page_feeds: Iterable[list[str]], result: ProposalsResult, known: _Known, rng: random.Random
) -> list[str]:
    """Every page's feed links, each once, minus the URLs we already know, at most
    _MAX_PER_SITE per site, in random order (so stopping early keeps a random subset)."""
    urls = {url for links in page_feeds for url in links}  # one site's pages repeat its feed
    result.candidates = len(urls)
    already = await known(sorted(urls))
    result.known += len(already)
    new = _shortest_per_site(urls - already, _MAX_PER_SITE)
    result.over_site_limit = len(urls) - len(already) - len(new)
    order = sorted(new)  # sorted first, so a seeded rng gives the same order every time
    rng.shuffle(order)
    return order


def _shortest_per_site(urls: Iterable[str], limit: int) -> list[str]:
    """The `limit` shortest URLs of each site (ties by name)."""
    by_site: dict[str, list[str]] = defaultdict(list)
    for url in urls:
        by_site[_site(url)].append(url)
    return [
        url
        for site_urls in by_site.values()
        for url in sorted(site_urls, key=lambda url: (len(url), url))[:limit]
    ]


def _judge(row: CheckRow, rules: Rules, wat_url: str, result: ProposalsResult) -> Proposal | None:
    """The proposal for a checked feed that passes every rule; else None, its reason counted."""
    if reason := rejection(row, rules):
        result.rejected[reason] += 1
        return None
    url = served_url(row, rules.exclude)
    assert url is not None and row.last_item_date is not None  # rejection() made sure
    return Proposal(
        url,
        wat_url,
        row.title,
        row.language,
        row.last_item_date,
        row.subtitle,
        row.recent_posts,
        _top_categories(row.item_tags),
        len(row.item_tags),
        site_url=row.site_url,
        authors=_top_authors(row.item_authors),
        future_dated=row.future_dated,
    )


def _top_categories(item_tags: Sequence[Sequence[str]]) -> tuple[tuple[str, int], ...]:
    """The categories the authors gave the feed's first posts, most used first (ties by name):
    (name, number of posts), case ignored, at most _MAX_CATEGORIES. Untrusted text, shown to
    reviewers only."""
    counts = Counter(tag for tags in item_tags for tag in {t.strip().lower() for t in tags} if tag)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(ranked[:_MAX_CATEGORIES])


def _top_authors(item_authors: Sequence[str | None]) -> tuple[tuple[str, int], ...]:
    """Who wrote the feed's first posts, most posts first (ties by name): (name as first written,
    number of posts), case ignored, at most _MAX_AUTHORS; posts without an author are left out.
    Untrusted text, shown to reviewers only."""
    counts: Counter[str] = Counter()
    names: dict[str, str] = {}
    for author in item_authors:
        if author and (key := author.casefold()):
            counts[key] += 1
            names.setdefault(key, author)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple((names[key], n) for key, n in ranked[:_MAX_AUTHORS])


async def _unknown(
    proposals: list[Proposal], known: _Known, result: ProposalsResult
) -> list[Proposal]:
    """Drops proposals whose served URL we already have (a redirect can lead to one)."""
    if not proposals:
        return []
    already = await known([s.url for s in proposals])
    result.known += len(already)
    return [s for s in proposals if s.url not in already]


def _site(url: str) -> str:
    """The host without www., so users of shared hosts (x.blog.fc2.com, y.free.fr) stay apart."""
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


async def _nothing_known(urls: Sequence[str]) -> set[str]:
    return set()
