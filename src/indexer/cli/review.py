"""indexer review: apply a model's review of a proposal file (prompts/review-proposals.md)."""

import argparse
import sys
from pathlib import Path

from indexer.proposals.review import ReviewError, apply_file

NAME = "review"
HELP = (
    "set accept, learn and reason on every feed of a proposal FILE from a model's ANSWER"
    " (one '<accept|reject|unsure> <reason or -> <url>' line per feed); all or nothing"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", type=Path, help="the proposal file (data/proposals/.../x.yaml)")
    parser.add_argument("answer", type=Path, help="the model's answer, or - for stdin")


async def run(args: argparse.Namespace) -> int:
    try:
        answer = sys.stdin.read() if str(args.answer) == "-" else args.answer.read_text("utf-8")
        counts = apply_file(args.file, answer)
    except (OSError, ReviewError) as exc:
        print(f"error: {args.file} not changed: {exc}", file=sys.stderr)
        return 1
    print(f"{args.file}: " + ", ".join(f"{n} {verdict}" for verdict, n in counts.items()))
    return 0
