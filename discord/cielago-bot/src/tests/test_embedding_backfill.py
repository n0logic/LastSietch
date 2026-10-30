"""Embedder changes must not silently destroy dedup.

Background, 2026-08-26: <web-host> had been running the lexical `HashEmbedder`
since deploy. `CIELAGO_ASSISTANT_EMBED_MODEL_DIR` was unset, no onnx model was
staged, and `onnxruntime` was not installed, so semantic dedup had never actually
run in production. Nothing surfaced it, because `load_embedder`'s guard is
`if prefer_onnx and model_dir:` and an absent dir skips the `try` entirely, so the
`embedder_onnx_unavailable` warning inside it could never fire.

The trap was that the obvious fix made it worse. `candidate_vectors()` filters on
the live `embed_backend` AND `embed_dim`, `set_embedding()` had zero call sites,
and no backfill existed, so staging the model would have flipped the live pair from
(hash, 256) to (bge-small-onnx, 384) and orphaned every stored vector: no
candidates, no matches, dedup off entirely, no error.

The first test here proves that defect is real. Without it the rest prove nothing.
"""
import pytest

from cielago.assistant.embeddings import (
    HashEmbedder,
    backfill_embeddings,
    load_embedder,
)
from cielago.assistant.classify import CAT_BUG, SEV_NORMAL
from cielago.assistant.store import OPEN_TICKET_STATES, SupportStore, Ticket


class _FakeOnnx:
    """Stands in for OnnxEmbedder: different backend tag, different dim, no model
    file needed. The dim difference is the point; 256 vs 384 is what orphans rows."""

    backend = "bge-small-onnx"
    dup_threshold = 0.84

    def __init__(self) -> None:
        self.dim = 384
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            vec = [0.0] * self.dim
            for i, ch in enumerate(t[: self.dim]):
                vec[i] = (ord(ch) % 17) / 17.0
            out.append(vec)
        return out


def _store(tmp_path):
    s = SupportStore(str(tmp_path / "support.sqlite"))
    s.connect()
    return s


def _ticket(title, body=""):
    return Ticket(id=0, created_at=1, author_id=1, author_name="n",
                  category=CAT_BUG, severity=SEV_NORMAL, title=title, body=body)


def _seed_hash_tickets(store, n=3):
    emb = HashEmbedder()
    ids = []
    for i in range(n):
        vec = emb.embed([f"spice map broken {i}"])[0]
        t = store.add_ticket(_ticket(f"spice map broken {i}"),
                             embedding=vec, backend=emb.backend)
        ids.append(t.id)
    return emb, ids


# --------------------------------------------------------------------------- #
# The defect
# --------------------------------------------------------------------------- #

def test_backend_switch_orphans_every_vector_without_a_backfill(tmp_path):
    """🔴 Prove the trap is real before proving the fix.

    Rows embedded under (hash, 256) become invisible the moment the live backend
    is (bge-small-onnx, 384). Not degraded: invisible. `candidate_vectors` returns
    an empty list, so `best_match` has nothing to compare and every duplicate is
    filed as new, with nothing logged."""
    s = _store(tmp_path)
    hash_emb, ids = _seed_hash_tickets(s)

    # Under the old backend they are all candidates.
    before = s.candidate_vectors("tickets", hash_emb.backend, hash_emb.dim,
                                 OPEN_TICKET_STATES)
    assert sorted(c[0] for c in before) == sorted(ids)

    # Under the new one, nothing is.
    onnx = _FakeOnnx()
    after = s.candidate_vectors("tickets", onnx.backend, onnx.dim, OPEN_TICKET_STATES)
    assert after == [], "the orphaning this module exists to prevent did not occur"


def test_backfill_restores_dedup_after_a_backend_switch(tmp_path):
    """The same switch, with the backfill run. Every row is a candidate again."""
    s = _store(tmp_path)
    _, ids = _seed_hash_tickets(s)
    onnx = _FakeOnnx()

    moved = backfill_embeddings(s, onnx)
    assert moved.get("tickets") == len(ids)

    after = s.candidate_vectors("tickets", onnx.backend, onnx.dim, OPEN_TICKET_STATES)
    assert sorted(c[0] for c in after) == sorted(ids)
    # And the vectors really are the new width, not the old blob relabelled.
    assert all(len(vec) == onnx.dim for _, vec in after)


# --------------------------------------------------------------------------- #
# Properties the backfill has to hold
# --------------------------------------------------------------------------- #

def test_backfill_is_idempotent(tmp_path):
    """A second run must be a no-op. If it is not, every restart re-embeds the whole
    table, which on the real embedder is CPU-bound work on the shared mail host."""
    s = _store(tmp_path)
    _seed_hash_tickets(s)
    onnx = _FakeOnnx()

    first = backfill_embeddings(s, onnx)
    assert first.get("tickets", 0) > 0

    onnx.calls.clear()
    second = backfill_embeddings(s, onnx)
    assert second == {}
    # Only the warmup call, nothing else.
    assert onnx.calls == [["warmup"]]


def test_backfill_is_resumable(tmp_path):
    """A crash mid-run must leave the job finishable, not corrupt. Each batch commits,
    and the pending set is re-read from the database on every pass."""
    s = _store(tmp_path)
    _, ids = _seed_hash_tickets(s, n=5)
    onnx = _FakeOnnx()

    # Simulate a partial run: embed only the first two rows by hand.
    partial = s.rows_needing_embedding("tickets", onnx.backend, onnx.dim, limit=2)
    assert len(partial) == 2
    for row_id, text in partial:
        s.set_embedding("tickets", row_id, onnx.embed([text])[0], onnx.backend)

    # Resume. It picks up exactly the remainder.
    moved = backfill_embeddings(s, onnx)
    assert moved.get("tickets") == len(ids) - 2
    after = s.candidate_vectors("tickets", onnx.backend, onnx.dim, OPEN_TICKET_STATES)
    assert sorted(c[0] for c in after) == sorted(ids)


def test_backfill_walks_past_a_full_batch(tmp_path):
    """The loop must not stop at the first batch boundary. With batch=2 and 5 rows it
    has to make three passes."""
    s = _store(tmp_path)
    _, ids = _seed_hash_tickets(s, n=5)
    onnx = _FakeOnnx()
    moved = backfill_embeddings(s, onnx, batch=2)
    assert moved.get("tickets") == len(ids)
    assert s.rows_needing_embedding("tickets", onnx.backend, onnx.dim) == []


def test_rows_needing_embedding_catches_rows_with_no_vector_at_all(tmp_path):
    """`IS NOT` rather than `<>`: an unembedded row has NULL in these columns, and
    `<>` does not match NULL. A row with no vector is exactly the row that needs one."""
    s = _store(tmp_path)
    bare = s.add_ticket(_ticket("no vector ever"))  # no embedding argument
    onnx = _FakeOnnx()
    pending = s.rows_needing_embedding("tickets", onnx.backend, onnx.dim)
    assert bare.id in [row_id for row_id, _ in pending]


def test_backfill_embeds_the_same_text_the_insert_path_does(tmp_path):
    """The insert path embeds `content`, and promotion embeds `body or title`. If the
    backfill embedded something else, its vectors would never match a freshly filed
    ticket, which looks exactly like dedup being broken."""
    s = _store(tmp_path)
    s.add_ticket(_ticket("short title", body="the full body text that gets embedded"))
    s.add_ticket(_ticket("title only, no body"))
    onnx = _FakeOnnx()

    pending = dict(s.rows_needing_embedding("tickets", onnx.backend, onnx.dim))
    texts = sorted(pending.values())
    assert texts == sorted(["the full body text that gets embedded",
                            "title only, no body"])


def test_backfill_covers_every_embedding_bearing_table(tmp_path):
    """kb and feature_requests carry vectors too. A backfill that only fixed tickets
    would leave the KB unsearchable after the same switch."""
    s = _store(tmp_path)
    onnx = _FakeOnnx()
    for table in ("tickets", "feature_requests", "kb"):
        # The reader must accept all three without raising.
        assert s.rows_needing_embedding(table, onnx.backend, onnx.dim) == []
    with pytest.raises(ValueError):
        s.rows_needing_embedding("qa_cache", onnx.backend, onnx.dim)


# --------------------------------------------------------------------------- #
# The silent fallback that hid all of this
# --------------------------------------------------------------------------- #

def test_absent_model_dir_warns_instead_of_falling_back_silently(monkeypatch):
    """The original bug was not the fallback, it was that the fallback was invisible.
    `if prefer_onnx and model_dir:` means an unset dir never enters the `try`, so the
    warning inside it can never fire. Production ran degraded for months on one INFO
    line. An unset dir must now say so."""
    import cielago.assistant.embeddings as mod

    warnings: list[tuple] = []

    class _Log:
        def warning(self, *a, **k):
            warnings.append((a, k))

        def info(self, *a, **k):
            pass

    monkeypatch.setattr(mod, "log", _Log())
    emb = mod.load_embedder(None, prefer_onnx=True)

    assert isinstance(emb, HashEmbedder)
    assert warnings, "an unset model dir fell back to hashing without warning"
    assert any("not_configured" in str(a) for a, _ in warnings)


def test_configured_dir_that_fails_to_load_still_warns(monkeypatch, tmp_path):
    """The other half: a dir that IS set but cannot load must keep warning through the
    existing except branch. Fixing the silent case must not mute the loud one."""
    import cielago.assistant.embeddings as mod

    warnings: list[tuple] = []

    class _Log:
        def warning(self, *a, **k):
            warnings.append((a, k))

        def info(self, *a, **k):
            pass

    monkeypatch.setattr(mod, "log", _Log())
    emb = mod.load_embedder(str(tmp_path / "nope"), prefer_onnx=True)

    assert isinstance(emb, HashEmbedder)
    assert any("unavailable" in str(a) for a, _ in warnings)


def test_load_embedder_stays_silent_when_onnx_is_not_wanted(monkeypatch):
    """prefer_onnx=False is a deliberate choice, not a misconfiguration. Warning there
    would train people to ignore the warning that matters."""
    import cielago.assistant.embeddings as mod

    warnings: list[tuple] = []

    class _Log:
        def warning(self, *a, **k):
            warnings.append((a, k))

        def info(self, *a, **k):
            pass

    monkeypatch.setattr(mod, "log", _Log())
    emb = mod.load_embedder(None, prefer_onnx=False)
    assert isinstance(emb, HashEmbedder)
    assert warnings == []
