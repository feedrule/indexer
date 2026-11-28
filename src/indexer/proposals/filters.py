"""Which checked Common Crawl feeds `indexer proposals` proposes (the URL rules before checking
are in feed_links.py).

`rejection(row, rules)` gives the first rule a feed fails, or None if it passes:
    failed       the check failed (fetch or parse)
    inactive     no item from the last `active_days` days
    not_english  English is not the only language detected on its items
    untitled     the feed has no title
    keyword      an excluded keyword (`[commoncrawl] exclude_keywords`) in its title, or 3 in its
                 subtitle and posts
    no_text      none of its recent posts has a title or a description
    business     its title or description names a business or an organisation (_BUSINESS:
                 service(s), agency, shop, company, law firm, radio station, parish, ...)
    job_board    its description starts "Jobs from" (job-board software)
    wiki         a recent post links to a wiki edit (do=diff, rev=, oldid=, doku.php, ?title=)
    tracking     a recent post link has utm_ tracking parameters
    seo_footer   a post says "appeared first on" or "first appeared on" (an SEO plugin's footer,
                 also left by sites that copy other feeds)
    sample_post  a recent post is WordPress's untouched sample post ("Hello world!"): an
                 abandoned or hijacked install
    repeated     two recent posts have the same title and text (the same post published again)
    seo_summary  two recent posts' descriptions start like an SEO meta description ("Discover
                 ...", "Learn ...", "Explore ...", "Understand ..."): content farms
    title_echo   two recent posts' descriptions start with the post's own title: generated text
    reposted     a post credits another outlet ("... article appeared in the Guardian")
    unwanted_url served from a URL the link rules reject (feed_links.is_unwanted), reached by a
                 redirect
    not_https    not served over https today (or redirected to an excluded domain)

The rules after `keyword` were agreed on 2026-11-21 from the reviews so far: simple, and
they cost some good feeds, which is fine (Common Crawl has far more than we check).
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from indexer.check.feed import RecentPost
from indexer.check.runner import CheckRow
from indexer.net.urls import drop_excluded, strip_session, valid_urls
from indexer.proposals.feed_links import is_unwanted

_MIN_TEXT_KEYWORDS = 3  # excluded words in a feed's subtitle and posts that exclude it

type Reason = Literal[
    "failed",
    "inactive",
    "not_english",
    "untitled",
    "keyword",
    "no_text",
    "business",
    "job_board",
    "wiki",
    "tracking",
    "seo_footer",
    "sample_post",
    "repeated",
    "seo_summary",
    "title_echo",
    "reposted",
    "unwanted_url",
    "not_https",
]

_WIKI_EDIT = re.compile(r"do=diff|[?&]rev=|oldid=|doku\.php|index\.php\?title=", re.IGNORECASE)
# Words that mark the site of a company or an organisation rather than a writer (agreed
# 2026-11-21: on the listed feeds about 1 in 140 harmless feeds has one; in two 200-feed
# proposals batches they caught 25 company, law-firm, radio and parish sites).
_BUSINESS_WORDS = [
    "service", "services", "attorney", "attorneys", "agency", "book now", "supplies",
    "equipment", "shop", "company", "store", "consultants",
    "law firm", "law group", "llc",
    "radio station", "station for",
    "congregation", "parish",
]  # fmt: skip
_BUSINESS = re.compile(rf"\b({'|'.join(_BUSINESS_WORDS)})\b", re.IGNORECASE)
_SAMPLE_POST = "welcome to wordpress. this is your first post"
_SEO_SUMMARY = re.compile(r"(discover|learn|explore|understand|uncover|find out)\b", re.IGNORECASE)
_REPOSTED = re.compile(r"article\s+appeared\s+in", re.IGNORECASE)
_SEO_FOOTER = re.compile(r"appeared\s+first\s+on|first\s+appeared\s+on", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Rules:
    now: datetime
    active_days: int  # the newest item must be at most this old
    exclude: frozenset[str] = frozenset()  # excluded domains (`[exclude] domains`)
    keywords: frozenset[str] = frozenset()  # excluded title words, lower case


def rejection(row: CheckRow, rules: Rules) -> Reason | None:
    """The first rule the checked feed fails, or None if it passes them all."""
    if row.error is not None:
        return "failed"
    if not _is_active(row, rules.now, rules.active_days):
        return "inactive"
    if not _is_english(row):
        return "not_english"
    if not row.title:
        return "untitled"
    if _has_keyword(row, rules.keywords):
        return "keyword"
    if not any(post.title or post.description for post in row.recent_posts):
        return "no_text"
    if _BUSINESS.search(f"{row.title} {row.subtitle}"):
        return "business"
    if (row.subtitle or "").strip().lower().startswith("jobs from"):
        return "job_board"
    links = [post.link for post in row.recent_posts]
    if any(_WIKI_EDIT.search(link) for link in links):
        return "wiki"
    if any("utm_" in link.lower() for link in links):
        return "tracking"
    texts = [*row.item_texts, *(post.description or "" for post in row.recent_posts)]
    if any(_SEO_FOOTER.search(text) for text in texts):
        return "seo_footer"
    if any(_SAMPLE_POST in text.lower() for text in texts):
        return "sample_post"
    posts = [(post.title, post.description) for post in row.recent_posts if post.title]
    if len(posts) != len(set(posts)):
        return "repeated"
    summaries = [post.description or "" for post in row.recent_posts]
    if sum(bool(_SEO_SUMMARY.match(text)) for text in summaries) >= 2:
        return "seo_summary"
    if sum(_echoes_title(post) for post in row.recent_posts) >= 2:
        return "title_echo"
    if any(_REPOSTED.search(text) for text in texts):
        return "reposted"
    served = served_url(row, rules.exclude)
    if served is None:
        return "not_https"
    if is_unwanted(served):
        return "unwanted_url"
    return None


# --- the rules after checking ---------------------------------------------------------------


def served_url(row: CheckRow, exclude: frozenset[str] = frozenset()) -> str | None:
    """The URL the feed is served from today (after redirects, session parameters removed) if
    it is a valid https URL
    outside the `exclude` domains (a redirect can lead anywhere); else None."""
    valid, _invalid = valid_urls([strip_session(row.final_url or row.url)])
    valid, _excluded = drop_excluded(valid, exclude)
    if not valid or not valid[0][0].startswith("https://"):
        return None
    return valid[0][0]


def _is_active(row: CheckRow, now: datetime, active_days: int) -> bool:
    """Parsed today, and the newest item is from the last `active_days` days. A date more than a
    day in the future is not trusted (the parser already ignores those: a guard)."""
    date = row.last_item_date
    if row.error is not None or date is None:
        return False
    return now - timedelta(days=active_days) <= date <= now + timedelta(days=1)


def _is_english(row: CheckRow) -> bool:
    """English is the only language detected on the feed's items (`en`, see `indexer.text.language`;
    agreed 2026-11-21: mixed-language feeds were mostly not worth it). What the feed declares is
    not read."""
    return {t.strip() for t in (row.language or "").split(",")} == {"en"}


def _has_keyword(row: CheckRow, keywords: frozenset[str]) -> bool:
    """One of `keywords` (lower case) is in the feed's title, or they appear _MIN_TEXT_KEYWORDS
    times or more in its subtitle, posts, recent posts' titles and the posts' categories together
    (every occurrence counts, so a recent post's title that is also among the posts counts twice,
    and a category counts once per post that has it). Whole words only:
    "sex" matches "Sex Toys" and "sex-ed" but not "Essex". Accents are ignored: "casino" matches
    "Casinò"."""
    if not keywords:
        return False
    folded = sorted({_fold(k) for k in keywords}, key=len, reverse=True)
    words = "|".join(re.escape(k) for k in folded)
    pattern = re.compile(rf"(?<![^\W_])(?:{words})(?![^\W_])")
    if pattern.search(_fold(row.title or "")):
        return True
    recent_titles = [post.title or "" for post in row.recent_posts]
    categories = [tag for tags in row.item_tags for tag in tags]
    text = _fold(" ".join([row.subtitle or "", *row.item_texts, *recent_titles, *categories]))
    return len(pattern.findall(text)) >= _MIN_TEXT_KEYWORDS


def _echoes_title(post: RecentPost) -> bool:
    """The post's description starts with its title (case and spacing ignored)."""
    title = " ".join((post.title or "").split()).lower()
    description = " ".join((post.description or "").split()).lower()
    return bool(title) and description.startswith(title)


def _fold(text: str) -> str:
    """Lower case without accents, for matching words: "Casinò" -> "casino"."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
