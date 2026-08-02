"""Semantic dedup decision — pure, backend-agnostic.

Given a query embedding and a list of (id, embedding) candidates (the open
tickets or feature requests carrying live-backend vectors), return the closest
match above threshold. The threshold itself comes from the embedder backend
(`Embedder.dup_threshold`) since hash and bge cosines live on different scales.
"""

from __future__ import annotations

from cielago.assistant.embeddings import cosine


def best_match(
    query: list[float],
    candidates: list[tuple[int, list[float]]],
    threshold: float,
) -> tuple[int, float] | None:
    """(id, score) of the single best candidate at or above threshold, else None."""
    best_id = -1
    best_score = -1.0
    for cid, vec in candidates:
        score = cosine(query, vec)
        if score > best_score:
            best_id, best_score = cid, score
    if best_id >= 0 and best_score >= threshold:
        return best_id, best_score
    return None
