"""indexer: secure RSS/Atom feed health checks.

Commands, in pipeline order:
  load-inputs  load the feed URLs from the lists in data/sources.toml into the inputs table
  proposals    propose feeds from a ~5-year-old Common Crawl crawl still active today
  run          check a URL file, or the most outdated inputs, and record one row per URL
  export       write the published lists (output/topic/<topic>/...) and website (output/docs/)
  cleanup      delete health checks older than a few months
  drop-tables  delete every table of this project (all data); needs --yes
  topics       the topics of some text (no feed, no database): to try the topic model
  languages    the languages of some text (no feed, no database): to try the language model
  train        train the proposal filter on the reviewed proposals (data/proposals/training.toml)
  review       apply a model's review (prompts/review-proposals.md) to a proposal file
  clusters     cluster the sentences of recent posts (posts branch, rejected proposals) with k-means
"""

import argparse
import asyncio
import logging
import sys
from typing import Protocol

from indexer.cli import (
    cleanup,
    clusters,
    drop_tables,
    embeddings,
    export,
    languages,
    load_inputs,
    proposals,
    review,
    run,
    topics,
    train,
    undo,
)


class Command(Protocol):
    """What each command module provides."""

    NAME: str  # as typed on the command line
    HELP: str  # one line in `indexer --help`

    def add_arguments(self, parser: argparse.ArgumentParser) -> None: ...

    async def run(self, args: argparse.Namespace) -> int: ...  # the exit code


# In the order `indexer --help` lists them.
COMMANDS: tuple[Command, ...] = (
    load_inputs,
    proposals,
    run,
    export,
    cleanup,
    undo,
    drop_tables,
    topics,
    embeddings,
    train,
    review,
    languages,
    clusters,
)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
        format="%(message)s",
        stream=sys.stderr,
    )
    command: Command = args.command_module
    return asyncio.run(command.run(args))


def parser() -> argparse.ArgumentParser:
    """The `indexer` parser: one subcommand per command module."""
    top = argparse.ArgumentParser(
        prog="indexer", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = top.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        p = sub.add_parser(command.NAME, help=command.HELP)
        command.add_arguments(p)
        p.set_defaults(command_module=command)
    return top
