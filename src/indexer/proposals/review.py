"""Applying a model's review (prompts/review-proposals.md) to a proposal file.

The model reads the file with no tools and answers one line per feed:

    accept - <url>
    reject <reason> <url>
    unsure - <url>

`apply_decisions` sets only the `accept`, `learn` and `reason` keys of each feed: accept gives
`accept: true` and `learn: true`, reject `accept: false`, `learn: true` and the reason, unsure
`accept: false` and `learn: false` (no reason; a feed nobody could judge doesn't teach the
proposal filter). Every other line of the file stays as it was, byte for byte.

The answer is untrusted (feed text may have steered the model), so it is all or nothing: an
unknown word or reason, a URL not in the file or given twice, or a feed left without a line
fails with ReviewError and the file is not written. The edit itself is checked too: each edited
feed is read back and must equal the original apart from those three keys.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from indexer.proposals.training import REJECT_REASONS
from indexer.sources import SourceError, proposal_documents

Verdict = Literal["accept", "reject", "unsure"]
VERDICTS: tuple[Verdict, ...] = ("accept", "reject", "unsure")

_DOCUMENT_START = re.compile(r"^---[ \t]*$", re.MULTILINE)
_REVIEW_KEY = re.compile(r"^(accept|learn|reason):")  # a top-level key: column 0
_KEYS = ("accept", "learn", "reason")


class ReviewError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    reason: str | None  # for a reject only


def parse_decisions(text: str) -> dict[str, Decision]:
    """The model's answer, by URL. Blank lines and code fences (```) are ignored; anything else
    must be `<accept|reject|unsure> <reason or -> <url>`. ReviewError otherwise."""
    decisions: dict[str, Decision] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.strip().startswith("```"):
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ReviewError(f"line {number}: expected '<verdict> <reason or -> <url>'")
        word, reason, url = fields
        verdict = next((v for v in VERDICTS if v == word), None)
        if verdict is None:
            raise ReviewError(f"line {number}: unknown verdict {word!r}")
        if word == "reject" and reason not in REJECT_REASONS:
            raise ReviewError(f"line {number}: unknown reason {reason!r}")
        if word != "reject" and reason != "-":
            raise ReviewError(f"line {number}: only a reject takes a reason")
        if url in decisions:
            raise ReviewError(f"line {number}: {url} decided twice")
        decisions[url] = Decision(verdict, reason if verdict == "reject" else None)
    return decisions


def apply_decisions(text: str, decisions: Mapping[str, Decision]) -> str:
    """The proposal file's text with every feed's review keys set from its decision."""
    starts = [m.start() for m in _DOCUMENT_START.finditer(text)]
    if not starts:
        raise ReviewError("no feed in the file")
    pieces = [text[: starts[0]]]
    seen: set[str] = set()
    for start, end in zip(starts, [*starts[1:], len(text)], strict=True):
        original = text[start:end]
        feed = _feed(original)
        url = feed["url"]
        if url in seen:
            raise ReviewError(f"{url} is in the file twice")
        seen.add(url)
        if url not in decisions:
            raise ReviewError(f"no decision for {url}")
        edited = _edit(original, decisions[url])
        expected = {k: v for k, v in feed.items() if k not in _KEYS} | _keys(decisions[url])
        if _feed(edited) != expected:
            raise ReviewError(f"{url}: the edit would change more than accept, learn and reason")
        pieces.append(edited)
    if unknown := sorted(set(decisions) - seen):
        raise ReviewError(f"not in the file: {', '.join(unknown)}")
    return "".join(pieces)


def apply_file(path: Path, answer: str) -> dict[Verdict, int]:
    """Apply the model's answer to the proposal file at `path` (atomically: a failure leaves it
    as it was) and count the verdicts."""
    decisions = parse_decisions(answer)
    edited = apply_decisions(path.read_text("utf-8"), decisions)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(edited, "utf-8")
    os.replace(tmp, path)
    counts = dict.fromkeys(VERDICTS, 0)
    for d in decisions.values():
        counts[d.verdict] += 1
    return counts


def _keys(decision: Decision) -> dict[str, Any]:
    keys: dict[str, Any] = {"accept": decision.verdict == "accept"}
    keys["learn"] = decision.verdict != "unsure"
    if decision.reason is not None:
        keys["reason"] = decision.reason
    return keys


def _edit(document: str, decision: Decision) -> str:
    """The document with its review keys removed and the decision's written after its url."""
    lines = [line for line in document.splitlines(keepends=True) if not _REVIEW_KEY.match(line)]
    url_line = next((i for i, line in enumerate(lines) if line.startswith("url:")), None)
    if url_line is None:
        raise ReviewError("a feed without a url line")
    review = "".join(
        yaml.safe_dump({k: v}, default_flow_style=False) for k, v in _keys(decision).items()
    )
    if not lines[url_line].endswith("\n"):
        lines[url_line] += "\n"
    lines.insert(url_line + 1, review)
    return "".join(lines)


def _feed(document: str) -> dict[str, Any]:
    try:
        feeds = proposal_documents(document.encode("utf-8"))
    except SourceError as exc:
        raise ReviewError(str(exc)) from exc
    if len(feeds) != 1:
        raise ReviewError("expected one feed per document")
    return feeds[0]
