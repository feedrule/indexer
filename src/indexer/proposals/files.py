"""Feeds found by `indexer proposals`, proposed for review in data/proposals/<timestamp>.yaml on
the `proposals` branch (checked out so that its data/proposals/ is this data/proposals/).

`indexer proposals` does not insert into the database. Each run writes its finds to a new file in
data/proposals/<year><month>/, so no file grows huge and two open pull requests never conflict;
the workflow proposes the file as a pull request against the proposals branch. The file is
YAML: a comment header, then one document per feed, separated by `---`, with what a reviewer
needs:

    # Proposed 2026-11-12 from CC-MAIN-2021-49.
    # Set accept: true on each feed to keep. ...
    ---
    url: https://missfrugalmommy.com/feed/
    accept: false
    title: Miss Frugal Mommy
    language: en
    last_post: 2026-11-12
    about: Saving money with kids, one small habit at a time, every week
    posts_read: 30
    categories:
      budgeting: 12
      kids: 9
    found_in: https://data.commoncrawl.org/crawl-data/CC-MAIN-2021-49/.../...warc.wat.gz
    recent_posts:
      - link: https://missfrugalmommy.com/ten-ways/
        title: Ten ways to save on groceries
        description:
          - We spend too much on food. Here are ten things that we
          - changed...

A post's description is a list of lines of at most 60 characters, for reading on a phone.

A reviewer sets `accept: true` on each feed they want (optionally `reason: <reason>` on a
rejected one, see training.py REJECT_REASONS) and merges; the daily input job (which checks out
the proposals branch too) then loads the accepted URLs (the `proposals` source, format
`proposals`). Proposals left at `accept: false` still count as known, so a feed left out is not
proposed again. Reviewed files are also the proposal model's training data.

Titles, categories and links come from the feeds, so they are untrusted: each is flattened to
one line of printable text and written by yaml.safe_dump, which quotes whatever YAML would
otherwise read as something else, so no feed's text can add a key, a feed or `accept: true`.
The pull request only says how many feeds were proposed and the run's summary. Next to the .yaml
file, a run also writes the feeds as OPML (<dd-HHMMSS>.opml, escaped XML attributes; not read as
a source), which the pull request links to, for trying them in a feed reader while reviewing.
"""

import textwrap
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import yaml

from indexer.proposals.filters import Reason
from indexer.proposals.run import Proposal, ProposalsResult
from indexer.proposals.training import REJECT_REASONS
from indexer.sources import proposal_documents

_MAX_TITLE = 200
_MAX_SUBTITLE = 400
_MAX_POST_LINK = 600
_MAX_DESCRIPTION = 1000
_WIDTH = 60  # characters per line of a post's description, for phones


# The rules' rejection counters and how the run summary names them, in rule order.
REJECTIONS: tuple[tuple[Reason, str], ...] = (
    ("untitled", "without a title"),
    ("keyword", "with an excluded keyword"),
    ("no_text", "whose posts have no text"),
    ("business", "businesses or organisations"),
    ("job_board", "job boards"),
    ("wiki", "wiki edits"),
    ("tracking", "with tracking links"),
    ("seo_footer", "with an SEO footer"),
    ("sample_post", "with WordPress's sample post"),
    ("repeated", "repeating a post"),
    ("seo_summary", "with SEO summaries"),
    ("title_echo", "echoing titles"),
    ("reposted", "reposting other outlets"),
    ("unwanted_url", "redirected to an unwanted URL"),
)


def known_urls(directory: Path) -> set[str]:
    """The URLs of every feed in the .yaml files under the folder, accepted or not."""
    return {
        str(feed["url"])
        for path in sorted(directory.rglob("*.yaml"))
        for feed in proposal_documents(path.read_bytes())
    }


def file_name(when: datetime) -> str:
    """A run's file under data/proposals/, from its UTC start time: 202611/21-041105.yaml."""
    return when.astimezone(UTC).strftime("%Y%m/%d-%H%M%S") + ".yaml"


def render_entries(proposals: Iterable[Proposal], crawl: str, today: date) -> str:
    """A run's file in data/proposals/: the header, then one YAML document per feed."""
    header = (
        f"# Proposed {today.isoformat()} from {crawl}.\n"
        "# Set accept: true on each feed to keep. A rejected feed may say why, with\n"
        f"# reason: one of {', '.join(REJECT_REASONS)}.\n"
    )
    return header + "".join("---\n" + _dump(_entry(s)) for s in proposals)


def _entry(s: Proposal) -> dict[str, Any]:
    """One feed's document, keys in reading order; untrusted text flattened to one line."""
    entry: dict[str, Any] = {"url": s.url, "accept": False}
    if s.score is not None:
        entry["score"] = round(s.score, 3)
    entry["title"] = _one_line(s.title, _MAX_TITLE) or None
    entry["language"] = _one_line(s.language, 200) or None
    entry["last_post"] = s.last_item_date.date()
    if subtitle := _one_line(s.subtitle, _MAX_SUBTITLE):
        entry["about"] = subtitle
    if s.posts_read:
        entry["posts_read"] = s.posts_read
        categories: dict[str, int] = {}
        for name, n in s.categories:
            key = _one_line(name, 50)
            categories[key] = categories.get(key, 0) + n  # two long names may cut alike
        entry["categories"] = categories
    entry["found_in"] = s.wat_url
    posts = []
    for post in s.recent_posts:
        item: dict[str, Any] = {"link": _one_line(post.link, _MAX_POST_LINK)}
        if title := _one_line(post.title, _MAX_TITLE):
            item["title"] = title
        if description := _one_line(post.description, _MAX_DESCRIPTION):
            item["description"] = textwrap.wrap(
                description, _WIDTH, break_long_words=False, break_on_hyphens=False
            )
        posts.append(item)
    entry["recent_posts"] = posts
    return entry


class _Dumper(yaml.SafeDumper):
    """safe_dump's writer, with nested lists indented under their key and never an alias
    (&id001 / *id001) when two values happen to be the same object."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _dump(entry: dict[str, Any]) -> str:
    # Block style (one key or list item per line), keys in the order given, text as is (not
    # \u escapes), and no folding of long lines (a URL or title stays on one line).
    return yaml.dump(
        entry,
        Dumper=_Dumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=1_000_000,
    )


def log_line(s: Proposal) -> str:
    """One line for the run's log: the URL, the newest item's date and the title."""
    title = _one_line(s.title, _MAX_TITLE) or "(no title)"
    return f"  + {s.url}  {s.last_item_date.date().isoformat()}  {title}"


def acceptance(proposed: int, checked: int) -> str:
    """The share of the checked feed URLs that were proposed, e.g. "0.87% (104 of 11944)"."""
    share = f"{100 * proposed / checked:.2f}%" if checked else "n/a"
    return f"{share} ({proposed} of {checked})"


def render_report(
    proposed: int, crawl: str, steps: Sequence[str], rejected: Sequence[tuple[int, str]]
) -> str:
    """Markdown for the pull request: how many feeds were proposed, the run's steps and what each
    rule rejected (most first, zero counts left out), as bullet lists. The feeds themselves are
    reviewed in the proposal file (untrusted text stays out of the PR)."""
    lines = [f"**{proposed} feeds** from `{crawl}`, still active today.", "", "**Run**", ""]
    lines += [f"- {step}" for step in steps]
    lines += ["", "**Rejected by the rules**", ""]
    by_count = sorted(rejected, key=lambda r: -r[0])
    lines += [f"- {n} {label}" for n, label in by_count] or ["- none"]
    return "\n".join(lines) + "\n"


def render_opml(proposals: Iterable[Proposal], crawl: str, generated: datetime) -> str:
    """The proposed feeds as OPML (the .opml file next to the run's .yaml), in proposal order,
    for importing into a feed reader during review. Titles are flattened to one printable line
    (no control characters, which XML forbids) and every value is escaped."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        f"    <title>{escape(f'Proposed feeds from {crawl}')}</title>",
        f"    <dateCreated>{format_datetime(generated)}</dateCreated>",
        "  </head>",
        "  <body>",
    ]
    for s in proposals:
        title = quoteattr(_one_line(s.title, _MAX_TITLE) or s.url)
        lines.append(
            f'    <outline type="rss" text={title} title={title} xmlUrl={quoteattr(s.url)}/>'
        )
    lines += ["  </body>", "</opml>"]
    return "\n".join(lines) + "\n"


def funnel(result: ProposalsResult, slice_mb: int, active_days: int) -> list[str]:
    """The run's steps, from the WAT slice to the feeds that passed the rules, one line each,
    for the log and the pull request."""
    passed = len(result.rows)
    stopped = (
        " (stopped: enough feeds)" if result.checked < result.candidates - result.known else ""
    )
    return [
        f"{slice_mb} MB of {result.scan.path.rsplit('/', 1)[-1]}",
        f"{result.candidates} feed URLs ({result.known} already known)",
        f"{result.checked} checked{stopped}",
        f"{result.working} working today",
        f"{result.active} with an item in the last {active_days} days",
        f"{result.english} in English",
        f"{passed} passed the rules: {acceptance(passed, result.checked)} of the checked",
    ]


def rejections(result: ProposalsResult) -> list[tuple[int, str]]:
    """How many feeds each rule rejected, with its name, in rule order; rules that rejected none
    are left out."""
    return [(n, label) for key, label in REJECTIONS if (n := result.rejected[key])]


def write_files(path: Path, proposals: Sequence[Proposal], crawl: str, started: datetime) -> None:
    """The proposal file at `path` (for review) and the same feeds as OPML beside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_entries(proposals, crawl, started.date()), "utf-8")
    path.with_suffix(".opml").write_text(render_opml(proposals, crawl, datetime.now(UTC)), "utf-8")


def _one_line(text: str | None, limit: int) -> str:
    """Untrusted text as one line of printable characters, at most `limit` long."""
    printable = "".join(
        c if unicodedata.category(c)[0] not in "CZ" or c == " " else " " for c in text or ""
    )
    flat = " ".join(printable.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"
