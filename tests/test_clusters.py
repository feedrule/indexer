"""Sentence clusters (indexer.text.clusters) with a fake embedder: no MiniLM, no network."""

import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from indexer.cli import parser
from indexer.text import clusters
from indexer.text.clusters import ClustersError, Sentences, build, kmeans, split

TOPICS = ("cats", "cars", "cakes")


def fake_embed(texts: Sequence[str]) -> np.ndarray:
    """One axis per topic word in the text, plus a little per-text noise; unit rows."""
    rows = []
    for text in texts:
        v = np.array([float(t in text) for t in TOPICS] + [0.0] * 5, dtype=np.float32)
        v[3 + len(text) % 5] += 0.1
        rows.append(v / np.linalg.norm(v))
    return np.array(rows, dtype=np.float32)


def posts_file(directory: Path, name: str, url: str, posts: list[dict[str, object]]) -> None:
    path = directory / name[:2] / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "posts": posts}), encoding="utf-8")


def test_split_keeps_sentences_of_three_words_or_more() -> None:
    assert split("  I like cats.  Me too!\nCars are fast, really fast? ok") == [
        "I like cats.",
        "Cars are fast, really fast?",
    ]
    assert split(None) == [] and split("") == []


def test_read_posts_takes_post_titles_and_descriptions(tmp_path: Path) -> None:
    posts_file(
        tmp_path,
        "aa01",
        "https://a.example/feed",
        [
            {
                "link": "https://a.example/1",
                "title": "All about cats today",
                "description": "Cats sleep a lot. Short one. They also purr loudly.",
            },
        ],
    )
    posts_file(tmp_path, "bb02", "https://b.example/feed", [])  # no posts
    posts_file(
        tmp_path,
        "cc03",
        "https://c.example/feed",
        [
            {"link": None, "title": None, "description": "Cats sleep a lot."},
        ],
    )
    found = Sentences()
    clusters.read_posts(tmp_path, found)
    assert (found.files, found.files_with_posts) == (3, 2)
    assert found.listed == {"https://a.example/feed", "https://c.example/feed"}
    assert found.feeds == {
        "All about cats today": {"https://a.example/feed"},
        "Cats sleep a lot.": {"https://a.example/feed", "https://c.example/feed"},  # once
        "They also purr loudly.": {"https://a.example/feed"},
    }


def test_read_posts_refuses_a_file_that_is_not_a_posts_file(tmp_path: Path) -> None:
    (tmp_path / "aa").mkdir()
    (tmp_path / "aa" / "aa01.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ClustersError):
        clusters.read_posts(tmp_path, Sentences())


def test_read_rejects_takes_rejected_posts_but_not_unsafe_or_keyword_feeds(tmp_path: Path) -> None:
    month = tmp_path / "202611"
    month.mkdir()
    (month / "21-000000.yaml").write_text(
        """# Proposed 2026-11-21.
---
url: https://kept.example/feed
accept: true
title: Kept blog about things
recent_posts:
  - link: https://kept.example/1
    title: A good post about cars
---
url: https://spam.example/feed
accept: false
reason: seo
title: Best deals on cars online
recent_posts:
  - link: https://spam.example/1
    title: Cheap cars for everyone today
    description:
      - Buy cheap cars here and
      - save money. Visit us now please.
---
url: https://unsafe.example/feed
accept: false
reason: unsafe
recent_posts:
  - link: https://unsafe.example/1
    title: Something not to show here
---
url: https://keyword.example/feed
accept: false
title: Hot casino nights for you
recent_posts:
  - link: https://keyword.example/1
    title: Play tonight and win big
""",
        encoding="utf-8",
    )
    found = Sentences()
    clusters.read_rejects(tmp_path, frozenset({"casino"}), found)
    assert found.rejected == {"https://spam.example/feed"}
    assert sorted(found.feeds) == [  # the posts, the description's lines joined; no feed title
        "Buy cheap cars here and save money.",
        "Cheap cars for everyone today",
        "Visit us now please.",
    ]


def test_build_writes_centres_metadata_and_a_readable_report(tmp_path: Path) -> None:
    found = Sentences()
    for i in range(12):
        for topic in TOPICS:
            feed = f"https://{topic}.example/feed{i % 3}"
            found.add(feed, [f"Post number {i} about {topic} and more."])
    found.rejected = {"https://cars.example/feed0"}
    found.listed = {f for fs in found.feeds.values() for f in fs} - found.rejected
    found.add(
        "https://cats.example/feed0", ["My `cats` meet @someone at [a](https://x.example) ok"]
    )
    model, tokenizer = tmp_path / "model.onnx", tmp_path / "tokenizer.json"
    model.write_bytes(b"model")
    tokenizer.write_bytes(b"tokenizer")
    out = tmp_path / "clusters"

    count = build(found, fake_embed, model, tokenizer, out, k=3, posts_commit="abc123")

    assert count == 37
    centres = np.load(out / "posts.npy")
    assert centres.shape == (3, 8) and centres.dtype == np.float16
    assert np.allclose(np.linalg.norm(centres.astype(np.float32), axis=1), 1, atol=1e-2)
    meta = json.loads((out / "posts.json").read_text(encoding="utf-8"))
    assert (meta["rows"], meta["dim"], meta["sentences"], meta["posts_commit"]) == (
        3,
        8,
        37,
        "abc123",
    )
    assert (meta["listed_feeds"], meta["rejected_feeds"]) == (8, 1)
    stats = sorted((c["sentences"], c["feeds"], c["rejected_feeds"]) for c in meta["clusters"])
    assert stats == [(12, 3, 0), (12, 3, 1), (13, 3, 0)]  # one topic each; cars has the reject
    assert all(c["top_words"] for c in meta["clusters"])
    report = (out / "posts.md").read_text(encoding="utf-8")
    assert report.startswith("# 3 clusters of 37 sentences\n")
    examples = [line for line in report.splitlines() if line.startswith("- ")]
    assert len(examples) == 3 * clusters.EXAMPLES
    assert all(line.startswith("- `") and line.endswith("`") for line in examples)
    # the same input gives the same clusters
    build(found, fake_embed, model, tokenizer, tmp_path / "again", k=3)
    assert (tmp_path / "again" / "posts.npy").read_bytes() == (out / "posts.npy").read_bytes()


def test_build_refuses_too_few_sentences(tmp_path: Path) -> None:
    found = Sentences()
    found.add("https://a.example/feed", ["Only a few cats here."])
    with pytest.raises(ClustersError, match="too few"):
        build(found, fake_embed, tmp_path / "m", tmp_path / "t", tmp_path, k=3)


def test_kmeans_centres_are_unit_rows() -> None:
    x = fake_embed([f"{t} {i} words here" for t in TOPICS for i in range(20)])
    centres = kmeans(x, 3)
    assert centres.shape == (3, 8)
    assert np.allclose(np.linalg.norm(centres, axis=1), 1, atol=1e-5)
    assert sorted(np.argmax(centres[:, :3], axis=1)) == [0, 1, 2]  # one centre per topic


def test_clusters_command_defaults() -> None:
    args = parser().parse_args(["clusters", "--posts", "published"])
    assert (args.k, str(args.directory), str(args.proposals)) == (
        500,
        "data/clusters",
        "data/survivors",
    )
    assert args.posts_commit is None


def test_untrusted_text_becomes_one_code_span() -> None:
    # no backtick can end the span, so links, mentions and markup stay plain text
    assert clusters._code("My `cats`\nmeet @someone at [a](https://x.example)\x07") == (
        "`My 'cats' meet @someone at [a](https://x.example)`"
    )
    assert len(clusters._code("word " * 100)) == 202  # capped at 200 characters, plus the ticks
