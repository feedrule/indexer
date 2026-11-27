"""Scoring proposals with the model trained on past reviews (training.py), to drop the likely
rejections before anyone reviews them."""

from dataclasses import replace
from datetime import date

from indexer.proposals import files
from indexer.proposals.run import Proposal
from indexer.proposals.training import ProposalModel, parse_proposals


def keep_likely(
    proposals: list[Proposal], model: ProposalModel, crawl: str, today: date
) -> tuple[list[Proposal], int]:
    """The proposals scoring at least the model's threshold, each with its score, best first,
    and how many were dropped. Each is scored on its text exactly as a reviewer reads it in the
    proposal file."""
    texts = [p.text for p in parse_proposals(files.render_entries(proposals, crawl, today))]
    scored = [
        replace(s, score=score) for s, score in zip(proposals, model.scores(texts), strict=True)
    ]
    kept = sorted(
        (s for s in scored if (s.score or 0.0) >= model.threshold), key=lambda s: -(s.score or 0.0)
    )
    return kept, len(scored) - len(kept)
