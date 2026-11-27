"""indexer proposals: propose feeds from a ~5-year-old Common Crawl crawl still active today."""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from indexer.check.runner import CheckRow, run_checks
from indexer.cli.common import (
    add_database_url,
    body_fetch,
    database,
    language_model,
    sources_config,
)
from indexer.net.fetch import FetchConfig, open_fetcher
from indexer.proposals import files
from indexer.proposals import run as proposals_run
from indexer.proposals.scoring import keep_likely
from indexer.proposals.training import TRAINING_CONFIG, TrainingError, cached_model
from indexer.proposals.wat import CommonCrawlError

NAME = "proposals"
HELP = (
    "propose feeds from a ~5-year-old crawl still active today (English, https)"
    " by appending them to --file for review"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("data/sources.toml"),
        help="for [exclude] domains and the [licenses] ranking (default data/sources.toml)",
    )
    parser.add_argument(
        "--years-ago", type=int, default=5, help="age of the crawl to sample (default 5)"
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("data/survivors"),
        help="write proposals to a new <yyyymm>/<dd-HHMMSS>.yaml here (UTC), and the same feeds"
        " as OPML in a .opml file beside it; URLs already in"
        " its files or in inputs are skipped (default data/survivors)",
    )
    parser.add_argument(
        "--training",
        type=Path,
        default=TRAINING_CONFIG,
        help="score each proposal with the model trained on the reviewed proposal files listed"
        " here, drop the lowest-scoring and list the rest best first; skipped if the file is"
        f" missing (default {TRAINING_CONFIG})",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="also write the run's summary as Markdown (for the pull request)",
    )
    parser.add_argument(
        "--slice-mb",
        type=int,
        default=25,
        help="download the first N MB of one random WAT file (default 25)",
    )
    parser.add_argument(
        "--active-days",
        type=int,
        default=243,
        help="keep feeds with an item in the last N days (default 243, about 8 months)",
    )
    parser.add_argument(
        "--max-feeds",
        type=int,
        default=20,
        help="stop checking once this many new feeds pass (default 20)",
    )
    add_database_url(parser)


async def run(args: argparse.Namespace) -> int:
    if min(args.slice_mb, args.years_ago, args.active_days, args.max_feeds) < 1:
        print(
            "error: --slice-mb, --years-ago, --active-days and --max-feeds must be positive",
            file=sys.stderr,
        )
        return 2
    if (config := sources_config(args.config)) is None:
        return 2
    if (languages := language_model()) is None:
        return 2
    started = datetime.now(UTC)
    slice_bytes = args.slice_mb * 1024 * 1024
    download = FetchConfig(deadline=900, max_body_bytes=slice_bytes + 1024 * 1024)
    in_file = files.known_urls(args.dir)
    try:
        async with (
            database(args) as db,
            body_fetch(download, CommonCrawlError) as fetch_once,
            open_fetcher(FetchConfig()) as checker,
        ):

            async def fetch_slice(url: str) -> bytes:
                return await fetch_once(url, {"Range": f"bytes=0-{slice_bytes - 1}"})

            async def check(urls: Sequence[str]) -> list[CheckRow]:
                return await run_checks(urls, checker, languages=languages)

            async def known(urls: Sequence[str]) -> set[str]:
                return {u for u in urls if u in in_file} | await db.known_inputs(list(urls))

            proposals_run.log.setLevel(logging.INFO)  # show the run's progress
            result = await proposals_run.propose(
                fetch_once,
                fetch_slice,
                check,
                years_ago=args.years_ago,
                active_days=args.active_days,
                max_feeds=args.max_feeds,
                known=known,
                exclude=config.exclude_domains,
                keywords=config.commoncrawl_exclude_keywords,
            )
    except CommonCrawlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    new = result.rows
    steps = files.funnel(result, args.slice_mb, args.active_days)
    rejected = files.rejections(result)
    if new and args.training.exists():
        try:
            model = cached_model(args.training)
        except (OSError, ValueError, TrainingError) as exc:
            print(f"warning: proposals not filtered: {exc}", file=sys.stderr)
            dropped = 0
        else:
            new, dropped = keep_likely(new, model, result.crawl, started.date())
        steps.append(f"{dropped} dropped by the model trained on past reviews")
    acceptance = files.acceptance(len(new), result.checked)
    # Checked in random order, so this is also the expected share of all the advertised feed URLs.
    steps.append(
        f"{len(new)} proposed: {acceptance} of the checked,"
        " a random sample of the advertised feed URLs"
    )

    rules = ", ".join(f"{n} {label}" for n, label in rejected) or "none rejected"
    path = args.dir / files.file_name(started)
    print(f"{result.crawl}: {'; '.join(steps)} (rules: {rules}): {len(new)} proposed in {path}")
    for proposal in new:
        print(files.log_line(proposal))
    print(f"Acceptance: {acceptance} of the checked feed URLs proposed")
    if new:
        files.write_files(path, new, result.crawl, started)
        if args.report:
            report = files.render_report(len(new), result.crawl, steps, rejected)
            args.report.write_text(report, encoding="utf-8")
    return 0
