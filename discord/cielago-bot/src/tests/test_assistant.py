import json
from datetime import datetime, timedelta, timezone

import pytest

from cielago.assistant.classify import (
    CAT_BUG,
    CAT_CHATTER,
    CAT_FEATURE,
    CAT_QUESTION,
    CAT_REPORT,
    SEV_HIGH,
    SEV_LOW,
    SEV_NORMAL,
    SEV_URGENT,
    classify_category,
    classify_severity,
    is_actionable,
    looks_like_report,
    normalize_question,
    summarize_title,
)
from cielago.assistant.dedup import best_match
from cielago.assistant.embeddings import HashEmbedder, cosine, pack, unpack
from cielago.assistant.migrate import migrate_tracker_json
from cielago.assistant.store import (
    OPEN_TICKET_STATES,
    TICKET_RESOLVED,
    FeatureRequest,
    SupportStore,
    Ticket,
)

# --- classify ---


def test_classify_category():
    assert classify_category("the map page is broken and crashes") == CAT_BUG
    assert classify_category("please add a dark mode toggle") == CAT_FEATURE
    assert classify_category("how do i join the deep desert?") == CAT_QUESTION
    assert classify_category("this player is cheating and griefing") == CAT_REPORT
    assert classify_category("good morning sietch") == CAT_CHATTER


def test_report_beats_bug():
    # A report about a cheater stays a report even with bug-ish words.
    assert classify_category("this hacker broke the match") == CAT_REPORT


def test_feature_loses_to_bug():
    assert classify_category("the new feature is broken") == CAT_BUG


def test_classify_severity():
    assert classify_severity("the server is down for everyone") == SEV_URGENT
    assert classify_severity("found an exploit / dupe") == SEV_URGENT
    assert classify_severity("my game crashed twice", CAT_BUG) == SEV_HIGH
    assert classify_severity("please add filters", CAT_FEATURE) == SEV_LOW
    assert classify_severity("the icon is slightly off", CAT_BUG) == SEV_NORMAL
    assert classify_severity("someone is harassing me", CAT_REPORT) == SEV_URGENT
    assert classify_severity("this person is rude", CAT_REPORT) == SEV_HIGH


def test_actionable():
    assert is_actionable(CAT_BUG)
    assert is_actionable(CAT_FEATURE)
    assert not is_actionable(CAT_CHATTER)


def test_summarize_and_report_helpers():
    assert summarize_title("hello   world\nsecond") == "hello world"
    assert len(summarize_title("x" * 200, limit=20)) == 20
    assert looks_like_report("the portal is down for me")
    assert not looks_like_report("ok")


def test_normalize_question():
    assert normalize_question("Where is the SPICE??") == "where is the spice"


# --- embeddings + dedup ---


def test_hash_embedder_deterministic_and_normalized():
    emb = HashEmbedder(dim=128)
    a1 = emb.embed(["the spice map is broken"])[0]
    a2 = emb.embed(["the spice map is broken"])[0]
    assert a1 == a2
    assert abs(sum(x * x for x in a1) - 1.0) < 1e-6  # L2-normalized
    assert len(a1) == 128


def test_hash_embedder_similarity_ranks_paraphrase_above_unrelated():
    emb = HashEmbedder()
    q = emb.embed(["the deep desert map is broken"])[0]
    near = emb.embed(["deep desert map seems broken"])[0]
    far = emb.embed(["how do i buy spice on the exchange"])[0]
    assert cosine(q, near) > cosine(q, far)


def test_pack_unpack_roundtrip():
    vec = [0.1, -0.2, 0.3, 0.0]
    out = unpack(pack(vec))
    assert len(out) == 4
    assert all(abs(a - b) < 1e-6 for a, b in zip(vec, out, strict=True))


def test_cosine_dim_mismatch_is_zero():
    assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_best_match_threshold():
    cands = [(1, [1.0, 0.0]), (2, [0.0, 1.0])]
    assert best_match([1.0, 0.0], cands, 0.9) == (1, 1.0)
    assert best_match([0.7, 0.7], cands, 0.95) is None


# --- store ---


def _store(tmp_path):
    s = SupportStore(str(tmp_path / "support.sqlite"))
    s.connect()
    return s


def test_ticket_roundtrip_and_dedup_candidates(tmp_path):
    s = _store(tmp_path)
    emb = HashEmbedder()
    vec = emb.embed(["spice map broken"])[0]
    t = Ticket(id=0, created_at=100, author_id=7, author_name="the owner",
               category=CAT_BUG, severity=SEV_NORMAL, title="spice map broken",
               guild_id=1, channel_id=2, message_id=3)
    saved = s.add_ticket(t, embedding=vec, backend=emb.backend)
    assert saved.id >= 1

    got = s.get_ticket(saved.id)
    assert got is not None and got.title == "spice map broken"
    assert got.jump_url() == "https://discord.com/channels/1/2/3"

    cands = s.candidate_vectors("tickets", emb.backend, len(vec), OPEN_TICKET_STATES)
    assert [c[0] for c in cands] == [saved.id]
    # A different backend tag yields no candidates.
    assert s.candidate_vectors("tickets", "other", len(vec), OPEN_TICKET_STATES) == []


def test_ticket_dedup_bump_and_resolve(tmp_path):
    s = _store(tmp_path)
    t = s.add_ticket(Ticket(id=0, created_at=1, author_id=1, author_name="n",
                            category=CAT_BUG, severity=SEV_NORMAL, title="x"))
    s.bump_ticket_dedup(t.id)
    assert s.get_ticket(t.id).dedup_count == 2
    s.update_ticket(t.id, status=TICKET_RESOLVED, resolution="done")
    assert s.get_ticket(t.id).status == TICKET_RESOLVED
    assert s.list_tickets() == []  # resolved is not in the open set


def test_processed_message_ids_unions_tickets_and_frs(tmp_path):
    s = _store(tmp_path)
    s.add_ticket(Ticket(id=0, created_at=1, author_id=1, author_name="n",
                        category=CAT_BUG, severity=SEV_NORMAL, title="x", message_id=111))
    s.add_feature_request(FeatureRequest(id=0, created_at=1, author_id=1,
                                         author_name="n", title="y", message_id=222))
    assert s.processed_message_ids() == {111, 222}


def test_feature_request_votes(tmp_path):
    s = _store(tmp_path)
    fr = s.add_feature_request(FeatureRequest(id=0, created_at=1, author_id=1,
                                              author_name="n", title="add dark mode"))
    assert s.add_vote(fr.id, 1) == 2
    assert s.add_vote(fr.id, -1) == 1
    assert s.add_vote(fr.id, -5) == 0  # floored at 0
    assert [f.id for f in s.list_feature_requests()] == [fr.id]


def test_counts(tmp_path):
    s = _store(tmp_path)
    s.add_ticket(Ticket(id=0, created_at=1, author_id=1, author_name="n",
                        category=CAT_BUG, severity=SEV_NORMAL, title="open one"))
    s.add_feature_request(FeatureRequest(id=0, created_at=1, author_id=1,
                                         author_name="n", title="fr one"))
    c = s.counts()
    assert c["open_tickets"] == 1
    assert c["open_feature_requests"] == 1


# --- migrate ---


def test_migrate_tracker_json(tmp_path):
    tracker = tmp_path / "tracker.json"
    tracker.write_text(json.dumps({
        "board_message_id": 1, "next_id": 4,
        "items": {
            "1": {"id": 1, "kind": "bug", "title": "portal 500s",
                  "reporter_id": 5, "reporter_name": "Medic", "created_at": 10,
                  "status": "open", "source_message_id": 901},
            "2": {"id": 2, "kind": "feature", "title": "add a kill feed",
                  "reporter_id": 6, "reporter_name": "the owner", "created_at": 11,
                  "status": "open"},
            "3": {"id": 3, "kind": "bug", "title": "fixed thing",
                  "reporter_id": 5, "reporter_name": "Medic", "created_at": 12,
                  "status": "closed"},
        },
    }), encoding="utf-8")
    s = _store(tmp_path)
    emb = HashEmbedder()
    res = migrate_tracker_json(s, emb, str(tracker))
    assert res == {"tickets": 2, "feature_requests": 1, "skipped": 0}

    # Idempotent: a second run imports nothing new.
    again = migrate_tracker_json(s, emb, str(tracker))
    assert again["tickets"] == 0 and again["feature_requests"] == 0

    # Open bug migrated; closed bug resolved; feature became an FR.
    assert 901 in s.processed_message_ids()
    assert [f.title for f in s.list_feature_requests()] == ["add a kill feed"]


def test_migrate_feature_with_source_id_is_idempotent(tmp_path):
    # Regression: the feature branch used to drop source_message_id, so the row
    # landed with message_id NULL and the msg-id skip never matched it — the same
    # FR re-imported on every startup (produced 12 copies in prod).
    tracker = tmp_path / "tracker.json"
    tracker.write_text(json.dumps({
        "board_message_id": 1, "next_id": 2,
        "items": {
            "1": {"id": 1, "kind": "feature", "title": "more starting volume",
                  "reporter_id": 6, "reporter_name": "the owner", "created_at": 11,
                  "status": "open", "source_message_id": 902,
                  "source_channel_id": 77},
        },
    }), encoding="utf-8")
    s = _store(tmp_path)
    emb = HashEmbedder()
    assert migrate_tracker_json(s, emb, str(tracker))["feature_requests"] == 1

    frs = s.list_feature_requests()
    assert [f.message_id for f in frs] == [902]
    assert [f.channel_id for f in frs] == [77]
    assert 902 in s.processed_message_ids()

    for _ in range(3):
        assert migrate_tracker_json(s, emb, str(tracker))["feature_requests"] == 0
    assert len(s.list_feature_requests()) == 1


def test_migrate_missing_file_is_noop(tmp_path):
    s = _store(tmp_path)
    res = migrate_tracker_json(s, HashEmbedder(), str(tmp_path / "nope.json"))
    assert res["tickets"] == 0


def test_ack_channels_narrow_the_watch_list_but_never_widen_it(monkeypatch):
    from cielago.config import Settings

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test")
    monkeypatch.setenv("LAST_SIETCH_GUILD_ID", "1")
    monkeypatch.setenv("CIELAGO_ASSISTANT_WATCH_CHANNEL_IDS", "10,20,30")

    # Blank = the pre-gate behaviour, ack anywhere it listens.
    monkeypatch.setenv("CIELAGO_ASSISTANT_ACK_CHANNEL_IDS", "")
    assert Settings().assistant_ack_channel_ids == {10, 20, 30}

    # Set = ack in a subset, keep filing in all three.
    monkeypatch.setenv("CIELAGO_ASSISTANT_ACK_CHANNEL_IDS", "10,20")
    s = Settings()
    assert s.assistant_ack_channel_ids == {10, 20}
    assert s.assistant_watch_channel_ids == {10, 20, 30}

    # An unwatched id can't be acked into existence — the gate only ever narrows.
    monkeypatch.setenv("CIELAGO_ASSISTANT_ACK_CHANNEL_IDS", "10,999")
    assert Settings().assistant_ack_channel_ids == {10}


# --- nightly sweep age guard ---


def test_sweep_cutoff_bounds_how_far_back_the_sweep_reaches():
    from cielago.cogs.assistant import sweep_cutoff

    before = datetime.now(timezone.utc)
    cutoff = sweep_cutoff(48)
    after = datetime.now(timezone.utc)
    assert cutoff is not None
    # 48h back, bracketed so the clock ticking mid-call can't flake it.
    assert before - timedelta(hours=48) <= cutoff <= after - timedelta(hours=48)

    # 0 and negatives are the documented escape hatch: no bound at all.
    assert sweep_cutoff(0) is None
    assert sweep_cutoff(-1) is None


@pytest.mark.asyncio
async def test_newer_than_stops_at_the_first_stale_message():
    from cielago.cogs.assistant import newer_than

    now = datetime.now(timezone.utc)

    class _Msg:
        def __init__(self, hours_ago):
            self.created_at = now - timedelta(hours=hours_ago)
            self.hours_ago = hours_ago

    # Newest-first, the order Discord hands history back in.
    ages = [1, 6, 47, 49, 200, 2]

    async def _history():
        for a in ages:
            yield _Msg(a)

    cutoff = now - timedelta(hours=48)
    seen = [m.hours_ago async for m in newer_than(_history(), cutoff)]

    # Stops at 49h. The 2h message behind it is NOT rescued: the contract is
    # "stop", not "filter", and it holds because Discord orders newest-first.
    assert seen == [1, 6, 47]

    # No cutoff = the pre-guard behaviour, the whole page.
    assert [m.hours_ago async for m in newer_than(_history(), None)] == ages
