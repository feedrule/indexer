"""indexer export: write the published lists (output/topic/...), the website (output/docs/) and
the feeds' posts files (into a checkout of the posts branch)."""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from indexer.cli.common import add_database_url, database
from indexer.publish.export import (
    DATA_URL,
    POSTS_URL,
    SITE_URL,
    ExportError,
    with_synonyms,
    write_output,
)
from indexer.text.topics import TOPICS_DIR, read_synonyms

NAME = "export"
HELP = "write the data files (output/topic/<topic>/...) and the website (output/docs/)"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="cleared first (default output)",
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=Path("SPEC.md"),
        help="copied into the output as SPEC.md (default SPEC.md)",
    )
    parser.add_argument(
        "--synonyms",
        type=Path,
        default=TOPICS_DIR / "synonyms.tsv",
        help="topic names mapped to their canonical name before publishing"
        f" (default {TOPICS_DIR / 'synonyms.tsv'})",
    )
    parser.add_argument(
        "--site-url",
        default=SITE_URL,
        help=f"where output/docs/ is served, ending with / (default {SITE_URL})",
    )
    parser.add_argument(
        "--data-url",
        default=DATA_URL,
        help=f"where the output's data files are served, ending with / (default {DATA_URL})",
    )
    parser.add_argument(
        "--posts-dir",
        type=Path,
        help="a checkout of the posts branch: each feed's posts file is written there when its"
        " posts changed, and the previews link to it (default: none, no preview has a posts_url)",
    )
    parser.add_argument(
        "--posts-url",
        default=POSTS_URL,
        help=f"where the posts branch is served, ending with / (default {POSTS_URL})",
    )
    parser.add_argument(
        "--top-topics", type=int, help="publish only this many topics, the largest (default: all)"
    )
    parser.add_argument(
        "--success-days",
        type=int,
        default=180,
        help="list feeds checked successfully within this many days (default 180, about 6 months)",
    )
    parser.add_argument(
        "--item-years",
        type=int,
        default=1,
        help="list feeds whose newest item is younger than this (default 1)",
    )
    add_database_url(parser)


async def run(args: argparse.Namespace) -> int:
    if (args.top_topics is not None and args.top_topics < 1) or min(
        args.success_days, args.item_years
    ) < 1:
        print(
            "error: --top-topics, --success-days and --item-years must be positive",
            file=sys.stderr,
        )
        return 2
    try:
        synonyms = read_synonyms(args.synonyms)
    except OSError as exc:
        print(f"error: cannot read the synonyms ({args.synonyms}): {exc}", file=sys.stderr)
        return 1
    async with database(args) as db:
        feeds = with_synonyms(await db.listed_feeds(args.success_days, args.item_years), synonyms)
    try:
        topics = write_output(
            args.output_dir,
            feeds,
            top=args.top_topics,
            generated=datetime.now(UTC),
            success_days=args.success_days,
            item_years=args.item_years,
            spec=args.spec,
            site_url=args.site_url,
            data_url=args.data_url,
            posts_dir=args.posts_dir,
            posts_url=args.posts_url,
        )
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    in_topics = len({f.url for t in topics for f in t.feeds})
    print(
        f"{len(feeds)} feeds qualify (all in all/); wrote {len(topics)} topics covering"
        f" {in_topics} feeds to {args.output_dir}/"
    )
    return 0
