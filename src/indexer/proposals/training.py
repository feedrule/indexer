"""A filter for proposals, learned from past reviews: the reviewed proposal files listed in
data/proposals/training.toml are read as labelled examples, a small model is trained on them
(up to a minute) and kept in data/models/ (not committed; `cached_model`), and `indexer
proposals` drops the new proposals it scores lowest (about the bottom CUT of them).

A proposal file (proposals/files.py) holds one YAML document per feed: what the reviewer
saw, and `accept: true` if they accepted it; a rejected feed may say why: `reason: <reason>`
(one of REJECT_REASONS; a note for people, checked but not used by the model). Each feed
becomes a `Reviewed`: the URL, the label, the reason and the text the reviewer read (title,
about, categories, the recent posts' titles and descriptions; not the bookkeeping: language,
dates, the WAT file, post links, a score), all in memory.

The model is the classic cheap text classifier: TF-IDF over words and word pairs, then a
logistic regression over CLASSES (accepted or rejected), trained in numpy. A proposal's score is
its probability of "accepted". The cut-off is the CUT quantile of the examples' cross-validated
scores, so it drops about CUT of proposals that look like the examples. Measured on 300 fresh
proposals (trained on 500): AUC 0.81; dropping the lowest 30% removed about half of the rejects
and 12% of the accepted feeds. One class per reason scored the same within noise (2026-11-22,
0.816 vs 0.812) but needs a reason on every reject; two classes learn from plain reviews. Training
is deterministic, so every run on the same files gets the same model.

The stored model's file name is a hash of the config, every listed file and this module: a file
that is there is up to date by construction, and any change to a review or to the code makes a
new name, so `indexer proposals` trains again (once) instead of using an old model.
"""

import hashlib
import logging
import re
import tomllib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from indexer.sources import SourceError, proposal_documents

TRAINING_CONFIG = Path("data/proposals/training.toml")  # on the proposals branch
MODEL_DIR = Path("data/models")  # the stored model (git-ignored)
CUT = 0.30  # drop about this share of proposals: those scored lowest
# Why a feed was rejected, as a reviewer may note it (`reason:`, optional; not used by the model,
# which only learns accepted or rejected).
REJECT_REASONS = (
    "commercial",  # a business's own marketing, shop or service blog
    "seo",  # generic SEO, affiliate or guest-post content
    "press",  # press releases, PR wires, promotion
    "republished",  # syndicated or aggregated content, radio content farms
    "machine",  # automated listings: packages, jobs, products, fonts, maps
    "forum",
    "notices",  # an organisation's, club's, council's or school's notices
    "unsafe",  # scams, spam, conspiracy, gambling, adult content
    "other",
)


CLASSES = ("accepted", "rejected")
MIN_DF = 2  # a term must occur in this many examples to be used
L2 = 1e-3  # weight penalty
STEPS = 400  # Adam steps (full batch)
FOLDS = 5  # cross-validation folds for the cut-off

Matrix = npt.NDArray[np.float32]


log = logging.getLogger(__name__)


class TrainingError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Reviewed:
    url: str
    accepted: bool
    text: str  # what the reviewer saw, as one line of plain text
    reason: str | None = None  # why it was rejected (REJECT_REASONS), if the reviewer said


def training_files(config: Path = TRAINING_CONFIG) -> list[Path]:
    """The proposal files data/proposals/training.toml lists, relative to it, in its order."""
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    files = data.get("files")
    if not isinstance(files, list) or not all(isinstance(f, str) and f for f in files):
        raise TrainingError(f"{config}: files must be a list of paths")
    return [config.parent / f for f in files]


def parse_proposals(text: str) -> list[Reviewed]:
    """Every feed in a proposal file's text, in order: its URL, whether it was accepted, the
    text the reviewer saw and the reason it was rejected, if given. TrainingError on a malformed
    file, an `accept` that isn't true or false, an unknown reason or a reason on an accepted
    feed."""
    try:
        feeds = proposal_documents(text.encode("utf-8"))
    except SourceError as exc:
        raise TrainingError(str(exc)) from exc
    proposals = []
    for feed in feeds:
        url, accepted, reason = feed["url"], feed.get("accept"), feed.get("reason")
        if not isinstance(accepted, bool):
            raise TrainingError(f"{url}: accept must be true or false")
        if reason is not None:
            if accepted:
                raise TrainingError(f"{url}: accepted, but has a rejection reason")
            if reason not in REJECT_REASONS:
                raise TrainingError(f"{url}: unknown rejection reason {reason!r}")
        proposals.append(Reviewed(url, accepted, reviewed_text(feed), reason))
    return proposals


def reviewed_text(feed: dict[str, Any]) -> str:
    """What the reviewer read about a feed, as one line: title, about, categories, and each
    recent post's title and description."""
    parts = [str(feed.get("title") or ""), f"about: {feed['about']}" if feed.get("about") else ""]
    if "categories" in feed:
        names = ", ".join(f"{k} {n}" for k, n in (feed["categories"] or {}).items())
        parts.append(f"categories: {names or 'none'}")
    for post in feed.get("recent_posts") or []:
        parts += [str(post.get("title") or ""), " ".join(post.get("description") or [])]
    return " ".join(" ".join(parts).split())


def read_examples(config: Path = TRAINING_CONFIG) -> list[Reviewed]:
    """Every proposal in the training files, in the files' order."""
    return [p for path in training_files(config) for p in parse_proposals(path.read_text("utf-8"))]


def tokens(text: str) -> list[str]:
    """Lower-case ASCII words and pairs of neighbouring words ("press_release")."""
    # The model can't read sentences; it only sees which "terms" a text contains. Single words
    # lose their context ("press" alone is ambiguous), so we also add each pair of neighbouring
    # words: "press_release" is a much stronger hint of a PR feed than "press" or "release".
    words = re.findall(r"[a-z0-9]+", text.lower())
    return words + [f"{a}_{b}" for a, b in pairwise(words)]


@dataclass(frozen=True, slots=True)
class ProposalModel:
    vocab: dict[str, int]
    idf: Matrix
    weights: Matrix  # (terms + 1) x len(CLASSES); the last row is the bias
    threshold: float  # proposals scoring below it are dropped

    def scores(self, texts: Sequence[str]) -> list[float]:
        """Each text's probability of "accepted"."""
        if not texts:
            return []
        # Using a trained model ("inference") is cheap: turn each text into the same kind of
        # number row as during training, multiply by the learned weights to get one raw score
        # per class, and let softmax turn those raw scores into probabilities that add up to 1.
        # Column 0 is "accepted" (see CLASSES).
        x = _features([tokens(t) for t in texts], self.vocab, self.idf)
        return [float(p) for p in _softmax(_with_bias(x) @ self.weights)[:, 0]]


def train_model(config: Path = TRAINING_CONFIG) -> ProposalModel:
    """The model trained on every example of the files `config` lists, in memory; TrainingError
    unless both accepted and rejected examples occur."""
    examples = read_examples(config)
    # Step 1, the answers: y holds what reviewers decided, one number per example (0 accepted,
    # 1 rejected). The whole job of training is to find weights that reproduce these answers.
    y = np.array([_label(p) for p in examples], dtype=np.int64)
    if not (y == 0).any() or (y == 0).all():
        raise TrainingError("training needs both accepted and rejected examples")
    # Step 2, the vocabulary: every term seen in at least MIN_DF examples gets a column number.
    # A term seen only once can't teach anything general (it would just memorise that one feed).
    # idf ("inverse document frequency") is a weight per term: rare terms get a high weight and
    # terms in nearly every example ("the", "posts") a low one, because they tell feeds apart
    # less well.
    docs = [tokens(p.text) for p in examples]
    df = Counter(term for doc in docs for term in set(doc))
    vocab = {t: i for i, t in enumerate(sorted(t for t, n in df.items() if n >= MIN_DF))}
    idf = np.array([np.log((1 + len(docs)) / (1 + df[t])) + 1 for t in vocab], dtype=np.float32)
    x = _features(docs, vocab, idf)
    # Step 3, the cut-off. We want to drop about CUT of new proposals, so we need to know what
    # scores new proposals get. Scoring the training examples would lie: the model has seen
    # them and scores them too confidently. So we use cross-validation: split the examples into
    # FOLDS groups, train on all groups but one, score the left-out group, and repeat for each
    # group. Every example then has an honest score from a model that never saw it, and the
    # CUT quantile of those scores is the threshold.
    held_out = np.zeros(len(examples), dtype=np.float32)
    folds = np.arange(len(examples)) % FOLDS
    for k in range(FOLDS):
        seen = folds != k
        w = _fit(x[seen], y[seen], len(CLASSES))
        held_out[~seen] = _softmax(_with_bias(x[~seen]) @ w)[:, 0]
    # Step 4, the real model: trained on every example, since more examples make better weights.
    return ProposalModel(vocab, idf, _fit(x, y, len(CLASSES)), float(np.quantile(held_out, CUT)))


def model_path(config: Path = TRAINING_CONFIG, directory: Path = MODEL_DIR) -> Path:
    """Where the model trained on `config` is stored: proposals-<hash>.npz, the hash covering the
    config, each listed file (path and content) and this module's code."""
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for path in [config, *training_files(config)]:
        digest.update(path.as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return directory / f"proposals-{digest.hexdigest()[:16]}.npz"


def cached_model(config: Path = TRAINING_CONFIG, directory: Path = MODEL_DIR) -> ProposalModel:
    """The model trained on `config`: loaded from `model_path` if that file is there, else trained
    and stored there (older proposals-*.npz files removed). A store that fails (e.g. a read-only
    directory) is only logged: the trained model is still returned."""
    path = model_path(config, directory)
    if path.exists():
        try:
            with np.load(path, allow_pickle=False) as data:
                vocab = {str(t): i for i, t in enumerate(data["terms"])}
                return ProposalModel(
                    vocab,
                    np.asarray(data["idf"], dtype=np.float32),
                    np.asarray(data["weights"], dtype=np.float32),
                    float(data["threshold"]),
                )
        except (OSError, ValueError, KeyError) as exc:
            log.warning("retraining: cannot read %s: %s", path, exc)
    model = train_model(config)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".partial.npz")
        np.savez_compressed(
            partial,
            terms=np.array(list(model.vocab)),
            idf=model.idf,
            weights=model.weights,
            threshold=np.float64(model.threshold),
        )
        partial.replace(path)
        for old in directory.glob("proposals-*.npz"):
            if old != path:
                old.unlink()
    except OSError as exc:
        log.warning("proposal filter not stored in %s: %s", directory, exc)
    return model


def _label(p: Reviewed) -> int:
    return 0 if p.accepted else 1


def _features(docs: Sequence[list[str]], vocab: dict[str, int], idf: Matrix) -> Matrix:
    """TF-IDF rows: (1 + log count) * idf per known term, scaled to unit length."""
    # Models work on numbers, not text, so each text becomes one row of a matrix with a column
    # per vocabulary term. A cell says how much that term matters in that text: how often it
    # occurs (log-damped, so a word repeated 10 times doesn't count 10 times as much) times the
    # term's idf weight. Each row is then scaled to length 1, so long texts and short texts are
    # compared on what they say, not on how much they say. Unknown terms are simply ignored.
    x = np.zeros((len(docs), len(vocab)), dtype=np.float32)
    for row, doc in enumerate(docs):
        for term, count in Counter(doc).items():
            if (col := vocab.get(term)) is not None:
                x[row, col] = 1 + np.log(count)
    x *= idf
    norms = np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-12)
    return np.asarray(x / norms, dtype=np.float32)


def _with_bias(x: Matrix) -> Matrix:
    # A constant column of ones. Its weight is the "bias": the score a text gets before any of
    # its terms count, which lets the model learn that, say, most proposals are rejected.
    return np.hstack([x, np.ones((len(x), 1), dtype=np.float32)])


def _softmax(z: Matrix) -> Matrix:
    # Turns each row of raw scores into probabilities: exponentiate (all positive) and divide
    # by the row's sum (they add up to 1). Subtracting the row's maximum first changes nothing
    # in the result but keeps exp() from overflowing.
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return np.asarray(e / e.sum(axis=1, keepdims=True), dtype=np.float32)


def _fit(x: Matrix, y: npt.NDArray[np.int64], classes: int) -> Matrix:
    """Multinomial logistic regression (weights + bias row): softmax cross-entropy with an L2
    penalty, full-batch Adam from zeros, so the same examples always give the same weights."""
    # The model is just a weight matrix w: one row per term, one column per class. A text's
    # raw score for a class is the sum of the weights of the terms it contains (a matrix
    # product). Training starts with every weight at 0 and nudges them, STEPS times, in the
    # direction that makes the reviewers' answers more likely:
    #   - g, the gradient, says for each weight which way (and how strongly) to move it. For
    #     this model it has a simple form: the features (transposed) times the error, where
    #     the error is predicted probabilities minus answers, averaged over the examples.
    #   - L2 * w is the penalty: it pulls every weight back towards 0, so the model can't put a
    #     huge weight on one term that happened to appear in a few rejected feeds (overfitting).
    #   - Adam is a popular way to choose the step size per weight: m is a running average of
    #     the gradient (momentum), v of its square (how noisy it is), and 0.05 is the learning
    #     rate. Dividing by (1 - 0.9**t) etc. corrects both averages for starting at zero.
    # Nothing here is random, so the same examples always produce the same weights.
    xb = _with_bias(x)
    w = np.zeros((xb.shape[1], classes), dtype=np.float32)
    target = np.eye(classes, dtype=np.float32)[y]
    m, v = np.zeros_like(w), np.zeros_like(w)
    for t in range(1, STEPS + 1):
        g = xb.T @ (_softmax(xb @ w) - target) / len(xb) + L2 * w
        m = np.asarray(0.9 * m + 0.1 * g, dtype=np.float32)
        v = np.asarray(0.999 * v + 0.001 * g * g, dtype=np.float32)
        w -= 0.05 * (m / (1 - 0.9**t)) / (np.sqrt(v / (1 - 0.999**t)) + 1e-8)
    return w
