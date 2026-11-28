"""Scoring proposals with the model trained on past reviews (training.py), to show the likely
accepts first. Nothing is dropped (since 2026-11-28: a dropped feed was never seen, so the
model's wrong drops could not be measured); the score only orders the file."""

from dataclasses import replace
from datetime import date

from indexer.proposals import files
from indexer.proposals.run import Proposal
from indexer.proposals.training import ProposalModel, parse_proposals


def sort_by_score(
    proposals: list[Proposal], model: ProposalModel, crawl: str, today: date
) -> list[Proposal]:
    """Every proposal with its score, best first. Each is scored on its text exactly as a
    reviewer reads it in the proposal file."""
    texts = [p.text for p in parse_proposals(files.render_entries(proposals, crawl, today))]
    scored = [
        replace(s, score=score) for s, score in zip(proposals, model.scores(texts), strict=True)
    ]
    return sorted(scored, key=lambda s: -(s.score or 0.0))
