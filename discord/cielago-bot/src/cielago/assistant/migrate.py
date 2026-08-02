"""One-shot import of the retired tracker's JSON state into support.sqlite.

The old tracker (cogs/tracker.py, now removed) persisted bug/feature items to a
flat JSON file. The assistant supersedes it: bugs/other become tickets, features
become feature_requests, closed -> resolved/done. Each migrated row is embedded
with the live backend so it joins dedup immediately.

Idempotent by Discord source message id (manual items, which have none, are keyed
by title+created_at). Safe to call on every startup; it imports only what's new.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import structlog

from cielago.assistant import store as S
from cielago.assistant.embeddings import Embedder
from cielago.assistant.store import FeatureRequest, SupportStore, Ticket

log = structlog.get_logger()


def migrate_tracker_json(store: SupportStore, embedder: Embedder, path: str) -> dict[str, int]:
    p = Path(path)
    if not p.exists():
        return {"tickets": 0, "feature_requests": 0, "skipped": 0}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("assistant.migrate_bad_json", path=path, exc_info=True)
        return {"tickets": 0, "feature_requests": 0, "skipped": 0}

    # Idempotency: skip anything already imported, by source message id (chat
    # reports) or by (title, created_at) for the manual items that have no id.
    # Both tables matter — a feature item lands in feature_requests, not tickets.
    seen_msgs = store.processed_message_ids()
    seen_titles = {
        (t.title, t.created_at) for t in store.list_tickets(statuses=_all_ticket_states())
    }
    seen_titles |= {
        (fr.title, fr.created_at)
        for fr in store.list_feature_requests(statuses=_all_fr_states())
    }
    n_tickets = n_frs = n_skip = 0

    for raw in data.get("items", {}).values():
        msg_id = _opt_int(raw.get("source_message_id"))
        title = str(raw.get("title", "")).strip() or "(no description)"
        created = int(raw.get("created_at", 0)) or int(time.time())
        if msg_id and msg_id in seen_msgs:
            n_skip += 1
            continue
        if not msg_id and (title, created) in seen_titles:
            n_skip += 1
            continue

        kind = str(raw.get("kind", "bug"))
        is_open = str(raw.get("status", "open")) == "open"
        author_id = int(raw.get("reporter_id", 0))
        author_name = str(raw.get("reporter_name", "unknown"))
        vec = embedder.embed([title])[0]

        if kind == "feature":
            fr = FeatureRequest(
                id=0, created_at=created, author_id=author_id, author_name=author_name,
                title=title, status=S.FR_OPEN if is_open else S.FR_DONE,
                channel_id=_opt_int(raw.get("source_channel_id")), message_id=msg_id,
            )
            store.add_feature_request(fr, embedding=vec, backend=embedder.backend)
            n_frs += 1
        else:
            t = Ticket(
                id=0, created_at=created, author_id=author_id, author_name=author_name,
                category="bug", severity="normal", title=title,
                status=S.TICKET_OPEN if is_open else S.TICKET_RESOLVED,
                channel_id=_opt_int(raw.get("source_channel_id")), message_id=msg_id,
                auto=bool(raw.get("auto", False)),
                resolution=("imported-closed" if not is_open else None),
            )
            store.add_ticket(t, embedding=vec, backend=embedder.backend)
            n_tickets += 1
        if msg_id:
            seen_msgs.add(msg_id)
        else:
            seen_titles.add((title, created))

    if n_tickets or n_frs:
        store.record_audit(None, "migrate-tracker", path,
                           tickets=n_tickets, feature_requests=n_frs, skipped=n_skip)
        log.info("assistant.migrated", tickets=n_tickets, feature_requests=n_frs, skipped=n_skip)
    return {"tickets": n_tickets, "feature_requests": n_frs, "skipped": n_skip}


def _all_ticket_states() -> tuple[str, ...]:
    return (S.TICKET_OPEN, S.TICKET_CLAIMED, S.TICKET_RESOLVED, S.TICKET_ESCALATED, S.TICKET_MERGED)


def _all_fr_states() -> tuple[str, ...]:
    return (S.FR_OPEN, S.FR_PLANNED, S.FR_DONE, S.FR_DECLINED, S.FR_MERGED)


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
