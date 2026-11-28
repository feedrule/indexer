"""indexer review: have a model review a proposal file (prompts/review-proposals.md), or apply
an answer it gave."""

import argparse
import shlex
import sys
from pathlib import Path

from indexer.proposals.review import (
    BATCH_SIZE,
    PARALLEL,
    RETRIES,
    REVIEW_PROMPT,
    REVIEWER,
    ReviewError,
    apply_file,
    review_file,
)

NAME = "review"
HELP = (
    "review every feed of a proposal FILE with a model (in batches, no tools) and set accept,"
    " learn and reason from its answer, or apply an ANSWER given (one '<url> | <note> |"
    " <accept|reject|unsure> <reason or ->' line per feed); all or nothing"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", type=Path, help="the proposal file (data/proposals/.../x.yaml)")
    parser.add_argument(
        "answer",
        type=Path,
        nargs="?",
        help="apply this answer (- for stdin) instead of asking the model",
    )
    parser.add_argument(
        "--prompt", type=Path, default=REVIEW_PROMPT, help=f"default {REVIEW_PROMPT}"
    )
    parser.add_argument(
        "--reviewer",
        default=shlex.join(REVIEWER),
        help="the command that reads prompt and batch on stdin and prints the answer"
        " (default: %(default)s)",
    )
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="feeds per call")
    parser.add_argument("--parallel", type=int, default=PARALLEL, help="calls at once")
    parser.add_argument("--retries", type=int, default=RETRIES, help="more calls for a bad batch")
    parser.add_argument("--save-answer", type=Path, help="also write the model's answer here")


async def run(args: argparse.Namespace) -> int:
    try:
        if args.answer is None:
            answer = review_file(
                args.file.read_text("utf-8"),
                args.prompt.read_text("utf-8"),
                shlex.split(args.reviewer),
                args.batch_size,
                args.parallel,
                args.retries,
            )
            if args.save_answer:
                args.save_answer.write_text(answer, "utf-8")
        elif str(args.answer) == "-":
            answer = sys.stdin.read()
        else:
            answer = args.answer.read_text("utf-8")
        counts = apply_file(args.file, answer)
    except (OSError, ReviewError) as exc:
        print(f"error: {args.file} not changed: {exc}", file=sys.stderr)
        return 1
    print(f"{args.file}: " + ", ".join(f"{n} {verdict}" for verdict, n in counts.items()))
    return 0
