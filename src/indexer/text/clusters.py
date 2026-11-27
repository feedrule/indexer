"""Unsupervised clusters of the sentences of recent posts (titles and descriptions), from two
sources:

- the posts files (the posts branch's data/posts/<h:2>/<h>.json, written by `indexer export`),
  those with posts: the feeds listed now or before;
- the rejected feeds of the reviewed proposals (data/survivors/**/*.yaml,
  `accept: false`), except those rejected as unsafe or with an excluded keyword
  (`[commoncrawl] exclude_keywords`) in their text, so none of that reaches the examples.

Each distinct sentence is embedded once with MiniLM, then spherical k-means (seeded, so the same
sentences give the same clusters) groups them into K clusters. Written to data/clusters/ (the
binaries branch holds them, see clusters.yml):

    posts.npy   K x 384 cluster centres, unit length, float16 (as an embedding set's matrix)
    posts.json  how they were built, and per cluster: sentences, feeds, rejected feeds, top words
    posts.md    per cluster: counts, top words and example sentences, for reading

Feeds are counted per cluster, not just sentences: a cluster whose sentences come from one or
two feeds says little about other feeds. Everything here is untrusted text: posts.md shows it in
code spans, one line each (no links, mentions or markup).
"""

import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import numpy.typing as npt

from indexer.sources import SourceError, proposal_documents
from indexer.text.embeddings import BATCH, MODEL_NAME, Embed, sha256

CLUSTERS_DIR = Path("data/clusters")
NAME = "posts"  # posts.npy, posts.json, posts.md
K = 500
SEED = 0
ITERATIONS = 40
MIN_WORDS = 3  # shorter sentences ("Here they are.") carry no topic
MIN_SENTENCES_PER_CLUSTER = 10  # refuse to build K clusters from fewer than K x this sentences
EXAMPLES = 6
TOP_WORDS = 10
_STOP_WORDS = (  # left out of a cluster's top words
    "a an the and or but of to in on for with at by from as is are was were be been it its this"
    " that these those i you he she we they my your our their me him her us them not no so if"
    " then than there here what which who whom how when where why all any can will would just"
    " about into out up down over more most some such only also very do does did have has had"
    " get got one two new don didn doesn isn wasn"
)
_STOP = frozenset(_STOP_WORDS.split())
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z][a-z'+#-]{2,}")

Matrix = npt.NDArray[np.float32]


class ClustersError(Exception):
    pass


@dataclass
class Sentences:
    """Distinct sentences, and the feeds each appears in (feed URL -> rejected or not)."""

    feeds: dict[str, set[str]] = field(default_factory=dict)  # sentence -> feed URLs
    rejected: set[str] = field(default_factory=set)  # feed URLs from rejected proposals
    listed: set[str] = field(default_factory=set)  # feed URLs from the posts files
    files: int = 0  # posts files read
    files_with_posts: int = 0

    def add(self, feed: str, texts: Iterable[str | None]) -> None:
        for text in texts:
            for sentence in split(text):
                self.feeds.setdefault(sentence, set()).add(feed)


def split(text: str | None) -> list[str]:
    """Sentences of at least MIN_WORDS words, whitespace collapsed."""
    text = " ".join((text or "").split())
    return [s for s in _SENTENCE_END.split(text) if len(s.split()) >= MIN_WORDS]


def read_posts(directory: Path, into: Sentences) -> None:
    """The post titles and descriptions of every posts file (`export.render_posts`) with posts,
    in directory/<2 hex>/ (the posts branch's data/posts/)."""
    for path in sorted(directory.glob("[0-9a-f][0-9a-f]/*.json")):
        into.files += 1
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            url, posts = doc["url"], doc.get("posts") or []
        except (ValueError, KeyError, TypeError) as exc:
            raise ClustersError(f"{path}: not a posts file: {exc}") from exc
        if not posts:
            continue
        into.files_with_posts += 1
        into.listed.add(url)
        for post in posts:
            into.add(url, (post.get("title"), post.get("description")))


def read_rejects(directory: Path, keywords: frozenset[str], into: Sentences) -> None:
    """The recent posts of every rejected proposal in directory/**/*.yaml, except those rejected
    as unsafe or with an excluded keyword (a whole word) in their title or posts."""
    for path in sorted(directory.rglob("*.yaml")):
        try:
            feeds = proposal_documents(path.read_bytes())
        except SourceError as exc:
            raise ClustersError(f"{path}: {exc}") from exc
        for feed in feeds:
            if feed.get("accept") is not False or feed.get("reason") == "unsafe":
                continue
            texts = [feed.get("title")]
            for post in feed.get("recent_posts") or []:
                description = post.get("description") or []
                if isinstance(description, list):  # the proposal file wraps it in lines
                    description = " ".join(map(str, description))
                texts += [post.get("title"), description]
            words = set(_WORD.findall(" ".join(str(t or "") for t in texts).lower()))
            if words & keywords:
                continue
            into.rejected.add(feed["url"])
            into.add(feed["url"], texts[1:])  # the posts, not the feed's title


def embed_all(texts: Sequence[str], embed: Embed) -> Matrix:
    return np.vstack([embed(texts[i : i + BATCH]) for i in range(0, len(texts), BATCH)])


def kmeans(x: Matrix, k: int, seed: int = SEED, iterations: int = ITERATIONS) -> Matrix:
    """Spherical k-means of unit rows: k unit centres. Starts with k-means++ (seeded); the
    nearest centre is the one with the largest dot product."""
    rng = np.random.default_rng(seed)
    centres = np.empty((k, x.shape[1]), dtype=np.float32)
    centres[0] = x[rng.integers(len(x))]
    distance = 1 - x @ centres[0]
    for j in range(1, k):
        weights = np.maximum(distance, 0) ** 2
        total = weights.sum()
        pick = rng.choice(len(x), p=weights / total) if total > 0 else rng.integers(len(x))
        centres[j] = x[pick]
        distance = np.minimum(distance, 1 - x @ centres[j])
    for _ in range(iterations):
        nearest = assign(x, centres)
        sums = np.zeros_like(centres)
        np.add.at(sums, nearest, x)
        norms = np.linalg.norm(sums, axis=1)
        filled = norms > 0  # a centre that lost every sentence stays where it was
        centres[filled] = sums[filled] / norms[filled, None]
    return centres


def assign(x: Matrix, centres: Matrix) -> npt.NDArray[np.intp]:
    """Each row's nearest centre, in chunks so a large x doesn't need an n x k matrix at once."""
    out = np.empty(len(x), dtype=np.intp)
    for i in range(0, len(x), 20_000):
        out[i : i + 20_000] = (x[i : i + 20_000] @ centres.T).argmax(1)
    return out


def top_words(texts: Sequence[str], labels: npt.NDArray[np.intp], k: int) -> list[list[str]]:
    """The words most typical of each cluster: how many of its sentences use the word, times
    log(k / clusters using it)."""
    counts: list[Counter[str]] = [Counter() for _ in range(k)]
    for text, label in zip(texts, labels, strict=True):
        counts[label].update({w for w in _WORD.findall(text.lower()) if w not in _STOP})
    spread = Counter(w for c in counts for w in c)
    return [
        [
            w
            for w, _ in sorted(
                c.items(), key=lambda kv: (-kv[1] * np.log(k / spread[kv[0]]), kv[0])
            )
        ][:TOP_WORDS]
        for c in counts
    ]


def build(
    sentences: Sentences,
    embed: Embed,
    model: Path,
    tokenizer: Path,
    directory: Path = CLUSTERS_DIR,
    k: int = K,
    posts_commit: str | None = None,
) -> int:
    """Embed, cluster and write posts.npy, posts.json and posts.md to `directory`. Returns the
    number of sentences clustered."""
    texts = sorted(sentences.feeds)
    if len(texts) < k * MIN_SENTENCES_PER_CLUSTER:
        raise ClustersError(
            f"{len(texts)} sentences: too few for {k} clusters"
            f" (at least {k * MIN_SENTENCES_PER_CLUSTER})"
        )
    x = embed_all(texts, embed)
    centres = kmeans(x, k)
    labels = assign(x, centres)
    similarity = (x * centres[labels]).sum(1)
    words = top_words(texts, labels, k)
    clusters = []
    for j in range(k):
        members = np.flatnonzero(labels == j)
        feeds = set().union(*(sentences.feeds[texts[i]] for i in members))
        best = members[np.argsort(-similarity[members], kind="stable")][:EXAMPLES]
        clusters.append(
            {
                "sentences": len(members),
                "feeds": len(feeds),
                "rejected_feeds": len(feeds & sentences.rejected),
                "top_words": words[j],
                "examples": [texts[i] for i in best],
            }
        )
    directory.mkdir(parents=True, exist_ok=True)
    np.save(directory / f"{NAME}.npy", centres.astype(np.float16))
    meta = {
        "rows": k,
        "dim": int(centres.shape[1]),
        "dtype": "float16",
        "model": MODEL_NAME,
        "model_sha256": sha256(model),
        "tokenizer_sha256": sha256(tokenizer),
        "seed": SEED,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "posts_commit": posts_commit,
        "sentences": len(texts),
        "files": sentences.files,
        "files_with_posts": sentences.files_with_posts,
        "listed_feeds": len(sentences.listed),
        "rejected_feeds": len(sentences.rejected),
        "clusters": [{key: c[key] for key in c if key != "examples"} for c in clusters],
    }
    (directory / f"{NAME}.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    (directory / f"{NAME}.md").write_text(render(meta, clusters), encoding="utf-8")
    return len(texts)


def render(meta: dict[str, object], clusters: list[dict[str, object]]) -> str:
    lines = [
        f"# {meta['rows']} clusters of {meta['sentences']} sentences",
        "",
        f"From the recent posts of {meta['listed_feeds']} listed feeds (posts files with posts:"
        f" {meta['files_with_posts']} of {meta['files']}) and {meta['rejected_feeds']}"
        f" rejected proposals. Built {meta['built_at']}. Largest first; examples are the"
        " sentences closest to the centre. Untrusted text.",
        "",
    ]
    order = sorted(range(len(clusters)), key=lambda j: (-int(str(clusters[j]["sentences"])), j))
    for j in order:
        c = clusters[j]
        words = c["top_words"]
        examples = c["examples"]
        assert isinstance(words, list) and isinstance(examples, list)
        lines += [
            f"## Cluster {j}",
            "",
            f"{c['sentences']} sentences, {c['feeds']} feeds, {c['rejected_feeds']} of them"
            f" rejected. Top words: {', '.join(_code(w) for w in words)}",
            "",
            *(f"- {_code(e)}" for e in examples),
            "",
        ]
    return "\n".join(lines)


def _code(text: str) -> str:
    """Untrusted text as one inline code span: printable, one line, no backticks."""
    one_line = " ".join(text.split())  # newlines and tabs become spaces first
    clean = "".join(c for c in one_line if c.isprintable()).replace("`", "'")
    return f"`{clean[:200]}`"
