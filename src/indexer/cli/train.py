"""indexer train: train the proposal filter on the files data/proposals/training.toml lists."""

import argparse
import sys
from pathlib import Path

from indexer.proposals.training import TRAINING_CONFIG, TrainingError, train_model

NAME = "train"
HELP = (
    "train the proposal filter on the reviewed proposal files --training lists and print its"
    " size and cut-off (`indexer proposals` trains it the same way and keeps it in data/models/;"
    " this writes nothing)"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--training",
        type=Path,
        default=TRAINING_CONFIG,
        help=f"the list of reviewed proposal files (default {TRAINING_CONFIG})",
    )


async def run(args: argparse.Namespace) -> int:
    try:
        model = train_model(args.training)
    except (OSError, ValueError, TrainingError) as exc:
        print(f"error: cannot train the proposal filter: {exc}", file=sys.stderr)
        return 1
    print(f"proposal filter: {len(model.vocab)} terms, threshold {model.threshold:.3f}")
    return 0
