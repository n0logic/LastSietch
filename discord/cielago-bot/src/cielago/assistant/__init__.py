"""Cielago Assistant — retrieval-augmented support / ticketing for Last Sietch.

Phase 0/1 (no external LLM): keyword + local-embedding triage and dedup, a
mod-facing ticket lifecycle, and feature-request intake with upvotes. State lives
in a private support.sqlite (WAL); the bot never writes game state. See
docs/CIELAGO-ASSISTANT-PLAN-2026-06-13.md.
"""
