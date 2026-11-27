"""indexer clusters: cluster the sentences of recent posts (the posts branch's and the rejected
proposals') into data/clusters/posts.{npy,json,md} with MiniLM and k-means."""

import argparse
import sys
from pathlib import Path

from indexer.sources import SourceError, load_config
from indexer.text import clusters
from indexer.text.embeddings import load_embedder, model_paths

NAME = "clusters"
HELP = "cluster the sentences of recent posts (posts branch and rejected proposals) with k-means"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--posts", type=Path, required=True, help="the posts branch's data/posts/ directory"
    )
    parser.add_argument(
        "--proposals",
        type=Path,
        default=Path("data/proposals"),
        help="the reviewed proposal files (default data/proposals: the proposals branch's)",
    )
    parser.add_argument(
        "--sources",
        type=Path,
        default=Path("data/sources.toml"),
        help="for [commoncrawl] exclude_keywords (default data/sources.toml)",
    )
    parser.add_argument(
        "-k", type=int, default=clusters.K, help=f"number of clusters (default {clusters.K})"
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=clusters.CLUSTERS_DIR,
        help=f"where the files are written (default {clusters.CLUSTERS_DIR})",
    )
    parser.add_argument(
        "--posts-commit", help="the posts branch commit the posts come from (recorded)"
    )


async def run(args: argparse.Namespace) -> int:
    if args.k < 2:
        print("error: -k must be at least 2", file=sys.stderr)
        return 2
    try:
        keywords = load_config(args.sources).commoncrawl_exclude_keywords
        sentences = clusters.Sentences()
        clusters.read_posts(args.posts, sentences)
        clusters.read_rejects(args.proposals, keywords, sentences)
        print(
            f"{len(sentences.feeds)} sentences from {len(sentences.listed)} listed feeds"
            f" ({sentences.files_with_posts} of {sentences.files} posts files have posts)"
            f" and {len(sentences.rejected)} rejected proposals",
            flush=True,
        )
        model, tokenizer = model_paths(None, None)
        count = clusters.build(
            sentences,
            load_embedder(model, tokenizer),
            model,
            tokenizer,
            args.directory,
            args.k,
            args.posts_commit,
        )
    except (OSError, SourceError, clusters.ClustersError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out = args.directory / clusters.NAME
    print(f"{count} sentences in {args.k} clusters: {out}.npy, .json and .md")
    return 0
