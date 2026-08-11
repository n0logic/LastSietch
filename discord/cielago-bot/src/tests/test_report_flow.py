"""End-to-end-ish test of the /report filing path.

Runs the REAL Reports.file_report against a REAL SupportStore on a temp sqlite,
with only Discord stubbed. Source-scanning this would prove nothing: the whole
value of the path is that a filled-in form ends up as a queryable row, and that
is exactly the step a grep cannot see.

Also pins the two ways this can quietly go wrong:
  * the structured answers reaching the DB but not the mod embed, or the reverse
  * a mod-channel failure being reported to the player as success
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# config.py builds Settings() at import time. conftest.py does this for pytest;
# repeat it so this file also runs standalone as `python3 src/tests/...`.
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
os.environ.setdefault("LAST_SIETCH_GUILD_ID", "123456789")

from cielago.assistant.embeddings import HashEmbedder  # noqa: E402
from cielago.assistant.store import SupportStore  # noqa: E402
from cielago.cogs import report as R  # noqa: E402


class _Resp:
    def __init__(self):
        self.messages = []
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, ephemeral=False, **kw):
        self._done = True
        self.messages.append(content)


class _User:
    id = 4242
    display_name = "SandRider"

    def __str__(self):
        return "SandRider#0001"


class _Obj:
    def __init__(self, oid):
        self.id = oid


class _Interaction:
    def __init__(self):
        self.user = _User()
        self.guild = _Obj(123456789)
        self.channel = _Obj(999)
        self.response = _Resp()
        self.client = None


class _Field:
    def __init__(self, value):
        self.value = value


class _Modal:
    """Stands in for the submitted ReportModal: same attribute names."""

    def __init__(self, kind, **vals):
        self.kind = kind
        self.ingame_name = _Field(vals.get("ingame_name", ""))
        self.surface = _Field(vals.get("surface", ""))
        self.server = _Field(vals.get("server", ""))
        self.summary = _Field(vals.get("summary", ""))
        self.description = _Field(vals.get("description", ""))


class _Assistant:
    """Minimal stand-in for the Assistant cog: a store, no embedder, and a
    _post_ticket that records what it was handed."""

    def __init__(self, store, fail_post=False, embedder=None):
        self.store = store
        self.posted = []
        self._fail = fail_post
        self.embedder = embedder

    async def _post_ticket(self, t):
        if self._fail:
            raise RuntimeError("mod channel unavailable")
        self.posted.append(t)


class _Bot:
    def __init__(self, assistant):
        self._cogs = {"Assistant": assistant} if assistant else {}

    def get_cog(self, name):
        return self._cogs.get(name)


def _file(kind="bug", assistant_fail=False, no_assistant=False, embedder=None, **vals):
    d = tempfile.mkdtemp()
    store = SupportStore(os.path.join(d, "support.sqlite"))
    store.connect()
    assistant = None if no_assistant else _Assistant(
        store, fail_post=assistant_fail, embedder=embedder)
    cog = R.Reports(_Bot(assistant))
    inter = _Interaction()
    modal = _Modal(kind, **vals)
    asyncio.run(cog.file_report(inter, modal))
    return store, assistant, inter


def test_a_filled_form_becomes_a_queryable_ticket():
    store, assistant, inter = _file(
        kind="bug", ingame_name="IGN: SandRider", surface="the website",
        server="habbanya", summary="Augment chips overflow",
        description="The purple roll chips run past the card edge on mobile.")
    t = store.get_ticket(1)
    assert t is not None
    assert t.category == "bug"
    assert t.ingame_name == "SandRider"          # prefix stripped
    assert t.surface == "portal"               # "the website" normalised
    assert t.server == "Habbanya (PvE)"        # canonicalised
    assert t.reported_via == "report_modal"    # distinguishable from the watcher
    assert t.auto is False
    assert t.title == "Augment chips overflow"
    assert "past the card edge" in t.body
    # and it reached the mod channel
    assert len(assistant.posted) == 1 and assistant.posted[0].id == t.id
    # and the player was told the id
    assert "#1" in inter.response.messages[0]


def test_a_sparse_form_still_files():
    """Only the description is required. A player who cannot remember their
    sietch must not be blocked; an abandoned report is worth nothing."""
    store, _a, inter = _file(kind="bug", description="cannot log in")
    t = store.get_ticket(1)
    assert t.ingame_name is None and t.server is None
    assert t.surface == "unknown"          # recorded as unknown, NOT guessed
    assert t.title == "cannot log in"      # falls back to the description
    assert "#1" in inter.response.messages[0]


def test_feature_requests_are_categorised_separately():
    store, _a, _i = _file(kind="feature", description="please add a map filter")
    assert store.get_ticket(1).category == "feature"


def test_severity_is_never_taken_from_the_player():
    """Self-reported urgency is not comparable between players and would page
    mods on someone's judgement of their own problem."""
    store, _a, _i = _file(kind="bug",
                          description="URGENT!!! EVERYTHING IS BROKEN, CRITICAL")
    assert store.get_ticket(1).severity == "normal"


def test_a_mod_channel_failure_is_not_reported_as_success():
    """🔴 The ticket is saved either way, but telling the player 'a mod will pick
    it up' when the post failed is a lie that loses the report."""
    store, assistant, inter = _file(kind="bug", assistant_fail=True,
                                    description="something broke")
    assert store.get_ticket(1) is not None      # still saved
    assert assistant.posted == []
    said = inter.response.messages[0]
    assert "#1" in said
    assert "A mod will pick it up" not in said
    assert "mod" in said.lower()                # tells them to chase it


def test_no_store_means_an_honest_refusal_not_a_silent_drop():
    store, _a, inter = _file(kind="bug", no_assistant=True, description="x")
    assert store.get_ticket(1) is None
    assert "not saved" in inter.response.messages[0].lower()


def test_the_ticket_is_embedded_so_dedup_can_see_it():
    """🔴 REGRESSION 2026-08-04, found on the first real ticket (#148). The call
    was embedder.encode(text); the real API is embed([text]) -> [[float]]. The
    except swallowed the AttributeError, so the ticket filed happily with NO
    vector and was invisible to dedup forever -- two players reporting the same
    bug through the form would each get their own ticket, and a form ticket
    would never dedup against a watcher one.

    Uses the REAL HashEmbedder, not a hand-rolled stub. A stub of my own
    invention is what let this through: the original test gave the Assistant no
    embedder at all, so this path never ran."""
    emb = HashEmbedder()
    store, _a, _i = _file(kind="bug", embedder=emb,
                          description="the augment chips overflow on mobile")
    row = store.conn.execute(
        "SELECT embedding, embed_backend, embed_dim FROM tickets WHERE id=1").fetchone()
    assert row["embedding"] is not None, "ticket stored with no embedding: dedup is blind"
    assert row["embed_backend"] == emb.backend
    assert row["embed_dim"] and row["embed_dim"] > 0
    # and it must be the SAME shape the watcher produces, or the two paths
    # cannot be compared against each other by candidate_vectors()
    assert row["embed_dim"] == len(emb.embed(["x"])[0])


def test_the_mod_embed_shows_what_the_player_was_asked_for():
    """Collecting the fields and not rendering them wastes the ask: the mod
    still has to go and find out who the player is and where it happened. Seen
    on the first real ticket (#148) -- the player saw the context strip, the
    mod embed did not."""
    from cielago.cogs.assistant import build_ticket_embed
    store, _a, _i = _file(kind="bug", ingame_name="SandRider", surface="both",
                          server="habbanya", summary="broke",
                          description="things broke but this is a test")
    embed = build_ticket_embed(store.get_ticket(1))
    rendered = " | ".join(f"{f.name}:{f.value}" for f in embed.fields)
    assert "SandRider" in rendered
    assert "Both" in rendered
    assert "Habbanya (PvE)" in rendered
    # a watcher ticket has none of it and must NOT gain an empty field
    store2, _b, _j = _file(kind="bug", description="no structure here")
    t2 = store2.get_ticket(1)
    t2.ingame_name = t2.server = None
    t2.surface = "unknown"
    names = [f.name for f in build_ticket_embed(t2).fields]
    assert "Reported" not in names


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
