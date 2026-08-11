"""The watcher files LEADS, not tickets.

The behaviour under test is mostly about what does NOT happen: no ticket, no
mod-ops post, no reply in the channel. Those are easy to reintroduce by accident
and invisible in a passing suite unless asserted directly.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
os.environ.setdefault("LAST_SIETCH_GUILD_ID", "123456789")

from cielago.assistant import store as S  # noqa: E402
from cielago.assistant.store import Lead, SupportStore  # noqa: E402


def _store():
    d = tempfile.mkdtemp()
    st = SupportStore(os.path.join(d, "support.sqlite"))
    st.connect()
    return st


def _lead(st, **kw):
    base = dict(id=0, created_at=1, author_id=7, author_name="player",
                category="bug", severity="normal", title="thing broke",
                body="the thing broke when I did the other thing",
                guild_id=1, channel_id=2, message_id=3)
    base.update(kw)
    return st.add_lead(Lead(**base))


def test_a_lead_round_trips_and_is_not_a_ticket():
    st = _store()
    ld = _lead(st)
    assert ld.id == 1
    got = st.get_lead(1)
    assert got.title == "thing broke"
    assert got.status == S.LEAD_NEW
    assert got.promoted_ticket_id is None
    # 🔴 the point of the whole change: no ticket was created
    assert st.list_tickets() == []


def test_leads_are_listed_newest_first_and_only_while_new():
    st = _store()
    for i in range(3):
        _lead(st, message_id=100 + i, title=f"lead {i}")
    assert [l.id for l in st.list_leads()] == [3, 2, 1]
    st.update_lead(2, status=S.LEAD_DISMISSED)
    assert [l.id for l in st.list_leads()] == [3, 1]


def test_a_leaded_message_is_not_processed_twice():
    """processed_message_ids() is the guard for on_message AND the nightly
    sweep. If leads were left out of it, every restart would re-lead the entire
    backlog and re-react to weeks-old messages -- the exact failure that hit
    tickets on 2026-07-25 (74 filed in 80 seconds)."""
    st = _store()
    _lead(st, message_id=4242)
    assert 4242 in st.processed_message_ids()


def test_promotion_links_both_ways_and_is_not_repeatable():
    st = _store()
    _lead(st)
    from cielago.assistant.store import Ticket
    t = st.add_ticket(Ticket(id=0, created_at=2, author_id=7, author_name="player",
                             category="bug", severity="normal", title="thing broke",
                             reported_via="lead_promoted"))
    st.update_lead(1, status=S.LEAD_PROMOTED, promoted_ticket_id=t.id)
    ld = st.get_lead(1)
    assert ld.status == S.LEAD_PROMOTED
    assert ld.promoted_ticket_id == t.id
    assert st.get_ticket(t.id).reported_via == "lead_promoted"
    # no longer offered for review
    assert st.list_leads() == []


def test_the_watcher_routes_to_leads_by_default():
    """Source-level, because the routing decision is a config default that a
    future edit could silently flip back."""
    from cielago.config import settings
    assert settings.cielago_assistant_watcher_files_tickets is False

    src = Path(__file__).resolve().parents[1] / "cielago" / "cogs" / "assistant.py"
    body = src.read_text()
    proc = body.split("async def _process_message(", 1)[1].split("\n    async def ", 1)[0]
    assert "cielago_assistant_watcher_files_tickets" in proc
    assert "_handle_lead(" in proc
    # the lead path must return BEFORE anything that files or posts
    assert proc.index("_handle_lead(") < proc.index("_handle_ticket(")


def test_the_lead_handler_never_posts_or_replies():
    """🔴 The noise fix. _handle_lead may add a reaction and nothing else: no
    message.reply (the in-channel interruption), no _post_ticket (a mod-ops
    entry for an unreviewed keyword hit)."""
    src = Path(__file__).resolve().parents[1] / "cielago" / "cogs" / "assistant.py"
    body = src.read_text()
    handler = body.split("async def _handle_lead(", 1)[1].split("\n    async def ", 1)[0]
    assert "add_reaction" in handler
    assert "message.reply" not in handler
    assert "_post_ticket" not in handler
    assert "_ack_player" not in handler
    assert "add_ticket" not in handler


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    failures = 0
    for fn in _all_tests():
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(_all_tests()) - failures}/{len(_all_tests())} passed")
    raise SystemExit(1 if failures else 0)
