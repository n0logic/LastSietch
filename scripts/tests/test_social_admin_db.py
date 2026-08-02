#!/usr/bin/env python3
"""Unit tests for the social-layer admin.db modules (no game DB, no fastapi):
mailbox, lfg_seekers, guild_join, and the guild_recruiting Signal-Board columns.

Prod-safe: runs entirely against a throwaway admin.db in a temp dir (LASTSIETCH_DB_PATH),
never touches the real DB. Asserts the account_id-never-leaked contract and the
idempotency / rate / expiry behaviours.

Run:  python3 scripts/tests/test_social_admin_db.py     (also pytest-compatible)
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ADMIN = os.path.join(REPO, "admin-backend")
sys.path.insert(0, ADMIN)

# admin.db in a temp dir + the two env vars config.py requires at import.
os.environ.setdefault("LASTSIETCH_DB_PATH", os.path.join(tempfile.mkdtemp(), "admin.db"))
os.environ.setdefault("LASTSIETCH_RELAY_API_KEY", "test")
os.environ.setdefault("LASTSIETCH_SESSION_SECRET", "test")

import database  # noqa: E402
import mailbox  # noqa: E402
import lfg_seekers  # noqa: E402
import guild_join  # noqa: E402
import guild_recruiting  # noqa: E402
import player_profile  # noqa: E402

database.init_db()


def test_recruiting_structured_fields():
    r = guild_recruiting.set_recruiting(
        7, recruiting=True, blurb="hi", contact_note="c",
        account_id=1001, discord_id="42", char_name="Stilgar",
        playstyle="PvP", timezone="NA-East", language="EN",
        new_player_friendly=True, discord_url="https://discord.gg/x")
    assert r["playstyle"] == "PvP"
    assert r["new_player_friendly"] == 1
    assert r["discord_url"].startswith("https://")


def test_recruiting_discord_url_scheme_validation():
    # javascript:/data: and non-Discord hosts must be dropped (stored NULL).
    for bad in ("javascript:alert(1)", "data:text/html,x",
                "http://discord.gg/x", "https://evil.example/x"):
        r = guild_recruiting.set_recruiting(
            8, recruiting=True, blurb=None, contact_note=None,
            account_id=1, discord_id="1", char_name="X", discord_url=bad)
        assert r["discord_url"] is None, f"should reject {bad}"
    good = guild_recruiting.set_recruiting(
        8, recruiting=True, blurb=None, contact_note=None,
        account_id=1, discord_id="1", char_name="X",
        discord_url="https://discord.gg/abc123")
    assert good["discord_url"] == "https://discord.gg/abc123"


def test_lfg_upsert_and_no_account_id_leak():
    s = lfg_seekers.upsert(2001, char_name="Duncan", playstyle="PvE",
                           timezone="EU", role="fighter", note="lf", ttl_hours=48)
    assert "account_id" not in s
    assert s["char_name"] == "Duncan"
    active = lfg_seekers.list_active()
    assert len(active) == 1 and "account_id" not in active[0]
    lfg_seekers.delete(2001)
    assert lfg_seekers.list_active() == []


def test_join_request_idempotent_and_lifecycle():
    c1 = guild_join.create_request(
        requester_account_id=3001, guild_id=9, requester_char_name="Gurney",
        requester_discord_id="99", note="pls")
    assert c1["is_new"] and c1["request_id"] > 0
    # duplicate pending -> not new, same id
    c2 = guild_join.create_request(
        requester_account_id=3001, guild_id=9, requester_char_name="Gurney",
        requester_discord_id="99", note="again")
    assert not c2["is_new"] and c2["request_id"] == c1["request_id"]
    pend = guild_join.list_pending(9)
    assert len(pend) == 1 and "requester_account_id" not in pend[0]
    got = guild_join.get_pending(c1["request_id"], 9)
    assert got and got["requester_account_id"] == 3001
    assert guild_join.mark_invited(c1["request_id"], 1001) == 1
    assert guild_join.list_pending(9) == []
    # re-request allowed once the prior one is invited
    c3 = guild_join.create_request(
        requester_account_id=3001, guild_id=9, requester_char_name="Gurney",
        requester_discord_id="99", note="retry")
    assert c3["is_new"]


def test_join_request_rate_limit():
    acct = 4001
    for i in range(guild_join.RATE_MAX_PER_WINDOW):
        guild_join.create_request(
            requester_account_id=acct, guild_id=100 + i,
            requester_char_name="R", requester_discord_id="1", note=None)
    assert guild_join.rate_ok(acct) is False
    assert guild_join.rate_ok(acct + 1) is True


def test_mailbox_post_and_guild_read_state():
    mid = mailbox.post("guild", 12, "notification", subject="s", body="b",
                       payload={"kind": "join_request", "request_id": 77,
                                "requester_account_id": 3001})
    assert mid > 0
    # guild-wide single-row read flip
    assert mailbox.mark_read_by_payload(12, 77, 1001) == 1
    assert mailbox.mark_read_by_payload(12, 77, 1001) == 0
    # gift receipt (claimed) rides the same table
    gid = mailbox.post("player", 5005, "gift", state="claimed",
                       payload={"amount": 100, "currency": "solari"})
    assert gid > 0


def test_join_request_payload_has_no_account_id():
    # Regression for HIGH-1: mailbox.post must strip any account_id-ish key at the
    # source, so the officer-visible guild inbox never stores/leaks it. Even if a
    # caller passes requester_account_id, it must not survive to the stored row.
    import json
    from database import get_db
    mid = mailbox.post("guild", 12, "notification",
                       payload={"kind": "join_request", "request_id": 5,
                                "requester_account_id": 3001})
    conn = get_db()
    try:
        row = conn.execute("SELECT payload FROM portal_messages WHERE id = ?",
                           (mid,)).fetchone()
    finally:
        conn.close()
    stored = json.loads(row["payload"])
    assert "requester_account_id" not in stored
    assert not any("account_id" in k.lower() for k in stored)
    # the useful keys survive
    assert stored.get("request_id") == 5 and stored.get("kind") == "join_request"


def test_mailbox_strips_arbitrary_account_id_keys():
    import json
    from database import get_db
    mid = mailbox.post("player", 999, "notification",
                       payload={"kind": "x", "sender_account_id": 7,
                                "target_account_id": 8, "guild_id": 3})
    conn = get_db()
    try:
        row = conn.execute("SELECT payload FROM portal_messages WHERE id = ?",
                           (mid,)).fetchone()
    finally:
        conn.close()
    stored = json.loads(row["payload"])
    assert not any("account_id" in k.lower() for k in stored)
    assert stored.get("guild_id") == 3  # non-account_id keys preserved


def test_player_directory_default_public_and_optout():
    # Two linked players, neither has a profile row yet.
    conn = database.get_db()
    conn.execute("INSERT INTO ls_account_links (discord_id, account_id, "
                 "character_name, discord_handle) VALUES ('900','5001','Muadib','m#1')")
    conn.execute("INSERT INTO ls_account_links (discord_id, account_id, "
                 "character_name, discord_handle) VALUES ('901','5002','Chani','c#1')")
    conn.commit(); conn.close()

    # Default PUBLIC: both appear, and NO account_id leaks in the projection.
    names = {p["char_name"] for p in player_profile.list_public()}
    assert {"Muadib", "Chani"} <= names
    for p in player_profile.list_public():
        assert set(p.keys()) == {"char_name", "blurb"}
    assert player_profile.is_listed(5001) is True   # no row -> public

    # One-click opt-out: Chani goes private -> drops from the directory + is_listed False.
    player_profile.set_profile(5002, char_name="Chani", listed=False, blurb="leave me be")
    names2 = {p["char_name"] for p in player_profile.list_public()}
    assert "Muadib" in names2 and "Chani" not in names2
    assert player_profile.is_listed(5002) is False

    # Re-list + blurb round-trips (and is length-capped).
    prof = player_profile.set_profile(5002, char_name="Chani", listed=True, blurb="x" * 500)
    assert prof["listed"] is True
    assert len(prof["blurb"]) <= player_profile.BLURB_MAX
    assert "Chani" in {p["char_name"] for p in player_profile.list_public()}


def test_player_directory_name_search():
    conn = database.get_db()
    conn.execute("INSERT INTO ls_account_links (discord_id, account_id, "
                 "character_name, discord_handle) VALUES ('902','5003','Gurney','g#1')")
    conn.commit(); conn.close()
    hits = {p["char_name"] for p in player_profile.list_public(q="gur")}  # case-insensitive
    assert "Gurney" in hits
    assert "Muadib" not in hits


def test_read_body_json_and_form():
    # Guards the JSON-sent-but-form-read bug class end to end at the body reader:
    # a JSON request must parse to a dict of fields (not an empty body), a form
    # request returns the form mapping, and malformed / non-dict JSON degrades to
    # {} instead of crashing. (Regression: DMs/gifts/member-ops posted JSON while
    # handlers read request.form() -> "Message body is required.")
    import asyncio
    from http_body import read_body

    class FakeReq:
        def __init__(self, ctype, json_data=None, form_data=None, raise_json=False):
            self.headers = {"content-type": ctype}
            self._json = json_data
            self._form = form_data if form_data is not None else {}
            self._raise_json = raise_json

        async def json(self):
            if self._raise_json:
                raise ValueError("bad json")
            return self._json

        async def form(self):
            return self._form

    run = asyncio.run
    r = run(read_body(FakeReq("application/json",
                              json_data={"body": "hi", "recipient_char_name": "X"})))
    assert r.get("body") == "hi" and r.get("recipient_char_name") == "X"
    # charset suffix still counts as JSON
    r = run(read_body(FakeReq("application/json; charset=utf-8", json_data={"a": 1})))
    assert r.get("a") == 1
    # form-encoded returns the form mapping (.get works)
    r = run(read_body(FakeReq("application/x-www-form-urlencoded", form_data={"body": "fm"})))
    assert r.get("body") == "fm"
    # malformed JSON -> {} (no crash), non-dict JSON -> {}
    assert run(read_body(FakeReq("application/json", raise_json=True))) == {}
    assert run(read_body(FakeReq("application/json", json_data=[1, 2]))) == {}


def test_routers_have_no_raw_request_form():
    # Every portal write handler must read the body via _read_body (JSON-or-form),
    # NEVER raw request.form() (which sees a JSON client as an empty body).
    # http_body.py is the ONLY place request.form() may appear.
    for rel in ("routers/portal.py", "routers/messages.py"):
        with open(os.path.join(ADMIN, rel)) as f:
            src = f.read()
        assert "request.form()" not in src, (
            f"{rel} uses raw request.form(); route the body through _read_body")


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
