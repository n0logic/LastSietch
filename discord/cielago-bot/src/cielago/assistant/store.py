"""support.sqlite — the assistant's own state (never the game DB, never the mirror).

WAL-mode SQLite holding tickets, feature requests, the (Phase 2) knowledge base,
a (Phase 2/3) answer cache, and an audit trail. All writes land here; the bot is
strictly read-only to game data.

Embeddings are stored as float32 blobs alongside their backend tag + dim so a
backend switch (hash <-> bge) never silently compares incompatible vectors —
candidate_vectors() only returns rows matching the live backend; the rest get
re-embedded by a backfill.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from cielago.assistant.embeddings import pack, unpack

log = structlog.get_logger()

TICKET_OPEN = "open"
TICKET_CLAIMED = "claimed"
TICKET_RESOLVED = "resolved"
TICKET_ESCALATED = "escalated"
TICKET_MERGED = "merged"
TICKET_DISREGARDED = "disregarded"  # dismissed as a false positive by a mod
OPEN_TICKET_STATES = (TICKET_OPEN, TICKET_CLAIMED, TICKET_ESCALATED)

FR_OPEN = "open"
FR_PLANNED = "planned"
FR_DONE = "done"
FR_DECLINED = "declined"
FR_MERGED = "merged"
OPEN_FR_STATES = (FR_OPEN, FR_PLANNED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    message_id INTEGER,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    claimed_by INTEGER,
    claimed_by_name TEXT,
    dedup_count INTEGER NOT NULL DEFAULT 1,
    dedup_into INTEGER,
    resolution TEXT,
    kb_entry_id INTEGER,
    mod_message_id INTEGER,
    auto INTEGER NOT NULL DEFAULT 0,
    embedding BLOB,
    embed_backend TEXT,
    embed_dim INTEGER,
    -- Structured intake (the /report modal). NULL on every ticket the passive
    -- watcher files, because a message in chat carries none of it. That NULL is
    -- meaningful: it says "nobody asked", not "the player declined to answer".
    ingame_name TEXT,
    surface TEXT,      -- portal | game | both | NULL
    server TEXT,       -- sietch/server or map, free text, normalised on intake
    reported_via TEXT  -- watcher | report_modal
);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status);

CREATE TABLE IF NOT EXISTS feature_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    votes INTEGER NOT NULL DEFAULT 1,
    canonical_id INTEGER,
    guild_id INTEGER,
    channel_id INTEGER,
    message_id INTEGER,
    mod_message_id INTEGER,
    embedding BLOB,
    embed_backend TEXT,
    embed_dim INTEGER
);
CREATE INDEX IF NOT EXISTS ix_fr_status ON feature_requests(status);

CREATE TABLE IF NOT EXISTS kb (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    question_patterns TEXT NOT NULL,
    answer_md TEXT NOT NULL,
    sources_json TEXT,
    tags TEXT,
    confidence REAL NOT NULL DEFAULT 0.5,
    usage_count INTEGER NOT NULL DEFAULT 0,
    helpful_up INTEGER NOT NULL DEFAULT 0,
    helpful_down INTEGER NOT NULL DEFAULT 0,
    last_verified INTEGER,
    origin TEXT NOT NULL DEFAULT 'manual',
    embedding BLOB,
    embed_backend TEXT,
    embed_dim INTEGER
);

CREATE TABLE IF NOT EXISTS qa_cache (
    norm_question_hash TEXT PRIMARY KEY,
    answer_md TEXT NOT NULL,
    sources_json TEXT,
    model TEXT,
    created_at INTEGER NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0
);

-- Watcher LEADS. The passive channel watcher used to file a ticket straight
-- from whatever a player typed in chat; those tickets carry no in-game name, no
-- surface, no server, and a keyword classifier's guess at severity. They were
-- also announced with a public quote-reply, which turned a stray "this is
-- broken" into a bot interruption mid-conversation.
--
-- A lead is the same detection, demoted: recorded, reacted to, reviewable, but
-- NOT a ticket and NOT posted to mod-ops individually. A mod promotes the ones
-- that matter, which is the only step that mints a real ticket. Structured
-- reports come through /report instead.
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    message_id INTEGER,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    promoted_ticket_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_leads_status ON leads(status);

CREATE TABLE IF NOT EXISTS audit (
    ts INTEGER NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    target TEXT,
    detail_json TEXT
);
"""


LEAD_NEW = "new"
LEAD_PROMOTED = "promoted"
LEAD_DISMISSED = "dismissed"
OPEN_LEAD_STATES = (LEAD_NEW,)


@dataclass
class Lead:
    """A watcher detection that has NOT been turned into a ticket."""
    id: int
    created_at: int
    author_id: int
    author_name: str
    category: str
    severity: str
    title: str
    body: str = ""
    status: str = LEAD_NEW
    guild_id: int | None = None
    channel_id: int | None = None
    message_id: int | None = None
    promoted_ticket_id: int | None = None

    def jump_url(self) -> str | None:
        if not (self.guild_id and self.channel_id and self.message_id):
            return None
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"


@dataclass
class Ticket:
    id: int
    created_at: int
    author_id: int
    author_name: str
    category: str
    severity: str
    title: str
    body: str = ""
    status: str = TICKET_OPEN
    guild_id: int | None = None
    channel_id: int | None = None
    message_id: int | None = None
    claimed_by: int | None = None
    claimed_by_name: str | None = None
    dedup_count: int = 1
    dedup_into: int | None = None
    resolution: str | None = None
    kb_entry_id: int | None = None
    mod_message_id: int | None = None
    auto: bool = False
    # Structured intake; None means the ticket came from the passive watcher and
    # nobody was ever asked. Do not render None as "not applicable".
    ingame_name: str | None = None
    surface: str | None = None
    server: str | None = None
    reported_via: str | None = None

    def jump_url(self) -> str | None:
        if not (self.guild_id and self.channel_id and self.message_id):
            return None
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"


@dataclass
class FeatureRequest:
    id: int
    created_at: int
    author_id: int
    author_name: str
    title: str
    body: str = ""
    status: str = FR_OPEN
    votes: int = 1
    canonical_id: int | None = None
    guild_id: int | None = None
    channel_id: int | None = None
    message_id: int | None = None
    mod_message_id: int | None = None

    def jump_url(self) -> str | None:
        if not (self.guild_id and self.channel_id and self.message_id):
            return None
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"


# Columns added after the table first shipped. SQLite has no
# ADD COLUMN IF NOT EXISTS, and CREATE TABLE IF NOT EXISTS silently does nothing
# to an existing table, so a live support.sqlite would keep the old shape and
# every read of a new field would raise. Additive only, never destructive.
_LATE_COLUMNS = {
    "tickets": {
        "ingame_name": "TEXT",
        "surface": "TEXT",
        "server": "TEXT",
        "reported_via": "TEXT",
    },
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _LATE_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                log.info("assistant.store_migrated", table=table, column=name)


def _col(r: sqlite3.Row, name: str):
    """Row value or None when the column predates this build."""
    try:
        return r[name]
    except (IndexError, KeyError):
        return None


def _ticket_from_row(r: sqlite3.Row) -> Ticket:
    return Ticket(
        id=r["id"], created_at=r["created_at"], author_id=r["author_id"],
        author_name=r["author_name"], category=r["category"], severity=r["severity"],
        title=r["title"], body=r["body"], status=r["status"], guild_id=r["guild_id"],
        channel_id=r["channel_id"], message_id=r["message_id"], claimed_by=r["claimed_by"],
        claimed_by_name=r["claimed_by_name"], dedup_count=r["dedup_count"],
        dedup_into=r["dedup_into"], resolution=r["resolution"], kb_entry_id=r["kb_entry_id"],
        mod_message_id=r["mod_message_id"], auto=bool(r["auto"]),
        ingame_name=_col(r, "ingame_name"), surface=_col(r, "surface"),
        server=_col(r, "server"), reported_via=_col(r, "reported_via"),
    )


def _lead_from_row(r: sqlite3.Row) -> Lead:
    return Lead(
        id=r["id"], created_at=r["created_at"], author_id=r["author_id"],
        author_name=r["author_name"], category=r["category"], severity=r["severity"],
        title=r["title"], body=r["body"], status=r["status"], guild_id=r["guild_id"],
        channel_id=r["channel_id"], message_id=r["message_id"],
        promoted_ticket_id=r["promoted_ticket_id"],
    )


def _fr_from_row(r: sqlite3.Row) -> FeatureRequest:
    return FeatureRequest(
        id=r["id"], created_at=r["created_at"], author_id=r["author_id"],
        author_name=r["author_name"], title=r["title"], body=r["body"], status=r["status"],
        votes=r["votes"], canonical_id=r["canonical_id"], guild_id=r["guild_id"],
        channel_id=r["channel_id"], message_id=r["message_id"],
        mod_message_id=r["mod_message_id"],
    )


class SupportStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        if self._conn is not None:
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        _add_missing_columns(conn)
        conn.commit()
        self._conn = conn
        log.info("assistant.store_ready", path=self.path)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    # --- tickets ---

    def add_ticket(self, t: Ticket, embedding: list[float] | None = None,
                   backend: str | None = None) -> Ticket:
        blob = pack(embedding) if embedding else None
        dim = len(embedding) if embedding else None
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO tickets
                   (created_at, author_id, author_name, guild_id, channel_id, message_id,
                    category, severity, title, body, status, dedup_count, auto,
                    embedding, embed_backend, embed_dim,
                    ingame_name, surface, server, reported_via)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (t.created_at, t.author_id, t.author_name, t.guild_id, t.channel_id,
                 t.message_id, t.category, t.severity, t.title, t.body, t.status,
                 t.dedup_count, int(t.auto), blob, backend, dim,
                 t.ingame_name, t.surface, t.server, t.reported_via),
            )
            self.conn.commit()
            t.id = int(cur.lastrowid)
        return t

    def get_ticket(self, ticket_id: int) -> Ticket | None:
        r = self.conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return _ticket_from_row(r) if r else None

    def update_ticket(self, ticket_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE tickets SET {cols} WHERE id=?", (*fields.values(), ticket_id)
            )
            self.conn.commit()

    def bump_ticket_dedup(self, ticket_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE tickets SET dedup_count = dedup_count + 1 WHERE id=?", (ticket_id,)
            )
            self.conn.commit()

    def list_tickets(self, statuses: tuple[str, ...] = OPEN_TICKET_STATES) -> list[Ticket]:
        ph = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"SELECT * FROM tickets WHERE status IN ({ph}) ORDER BY id", statuses
        ).fetchall()
        return [_ticket_from_row(r) for r in rows]

    # --- leads ---

    def add_lead(self, ld: "Lead") -> "Lead":
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO leads
                   (created_at, author_id, author_name, guild_id, channel_id,
                    message_id, category, severity, title, body, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (ld.created_at, ld.author_id, ld.author_name, ld.guild_id,
                 ld.channel_id, ld.message_id, ld.category, ld.severity,
                 ld.title, ld.body, ld.status),
            )
            self.conn.commit()
            ld.id = int(cur.lastrowid)
        return ld

    def get_lead(self, lead_id: int) -> "Lead | None":
        r = self.conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return _lead_from_row(r) if r else None

    def list_leads(self, statuses: tuple[str, ...] = OPEN_LEAD_STATES,
                   limit: int = 25) -> list["Lead"]:
        ph = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"SELECT * FROM leads WHERE status IN ({ph}) ORDER BY id DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()
        return [_lead_from_row(r) for r in rows]

    def update_lead(self, lead_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE leads SET {cols} WHERE id=?",
                              (*fields.values(), lead_id))
            self.conn.commit()

    def lead_message_ids(self) -> set[int]:
        rows = self.conn.execute(
            "SELECT message_id FROM leads WHERE message_id IS NOT NULL").fetchall()
        return {r["message_id"] for r in rows}

    def tracked_message_ids(self) -> set[int]:
        rows = self.conn.execute(
            "SELECT message_id FROM tickets WHERE message_id IS NOT NULL"
        ).fetchall()
        return {r["message_id"] for r in rows}

    def processed_message_ids(self) -> set[int]:
        """Source message ids already turned into a ticket OR a feature request —
        the de-dup guard for on_message and the daily sweep."""
        ids: set[int] = set()
        for table in ("tickets", "feature_requests", "leads"):
            rows = self.conn.execute(
                f"SELECT message_id FROM {table} WHERE message_id IS NOT NULL"
            ).fetchall()
            ids |= {r["message_id"] for r in rows}
        return ids

    def ticket_by_mod_message(self, mod_message_id: int) -> Ticket | None:
        r = self.conn.execute(
            "SELECT * FROM tickets WHERE mod_message_id=?", (mod_message_id,)
        ).fetchone()
        return _ticket_from_row(r) if r else None

    # --- feature requests ---

    def add_feature_request(self, fr: FeatureRequest, embedding: list[float] | None = None,
                            backend: str | None = None) -> FeatureRequest:
        blob = pack(embedding) if embedding else None
        dim = len(embedding) if embedding else None
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO feature_requests
                   (created_at, author_id, author_name, title, body, status, votes,
                    guild_id, channel_id, message_id, embedding, embed_backend, embed_dim)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fr.created_at, fr.author_id, fr.author_name, fr.title, fr.body, fr.status,
                 fr.votes, fr.guild_id, fr.channel_id, fr.message_id, blob, backend, dim),
            )
            self.conn.commit()
            fr.id = int(cur.lastrowid)
        return fr

    def get_feature_request(self, fr_id: int) -> FeatureRequest | None:
        r = self.conn.execute(
            "SELECT * FROM feature_requests WHERE id=?", (fr_id,)
        ).fetchone()
        return _fr_from_row(r) if r else None

    def fr_by_mod_message(self, mod_message_id: int) -> FeatureRequest | None:
        r = self.conn.execute(
            "SELECT * FROM feature_requests WHERE mod_message_id=?", (mod_message_id,)
        ).fetchone()
        return _fr_from_row(r) if r else None

    def add_vote(self, fr_id: int, delta: int = 1) -> int:
        with self._lock:
            self.conn.execute(
                "UPDATE feature_requests SET votes = MAX(0, votes + ?) WHERE id=?",
                (delta, fr_id),
            )
            self.conn.commit()
        r = self.conn.execute(
            "SELECT votes FROM feature_requests WHERE id=?", (fr_id,)
        ).fetchone()
        return r["votes"] if r else 0

    def update_feature_request(self, fr_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE feature_requests SET {cols} WHERE id=?", (*fields.values(), fr_id)
            )
            self.conn.commit()

    def list_feature_requests(
        self, statuses: tuple[str, ...] = OPEN_FR_STATES
    ) -> list[FeatureRequest]:
        ph = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"SELECT * FROM feature_requests WHERE status IN ({ph}) ORDER BY votes DESC, id",
            statuses,
        ).fetchall()
        return [_fr_from_row(r) for r in rows]

    # --- dedup candidate vectors ---

    def candidate_vectors(self, table: str, backend: str, dim: int,
                          statuses: tuple[str, ...]) -> list[tuple[int, list[float]]]:
        """Open rows that carry an embedding from the *live* backend/dim, for
        cosine dedup. Mismatched-backend rows are skipped (they get backfilled)."""
        if table not in ("tickets", "feature_requests"):
            raise ValueError(table)
        ph = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"""SELECT id, embedding FROM {table}
                WHERE status IN ({ph}) AND embedding IS NOT NULL
                AND embed_backend=? AND embed_dim=?""",
            (*statuses, backend, dim),
        ).fetchall()
        return [(r["id"], unpack(r["embedding"])) for r in rows]

    def set_embedding(self, table: str, row_id: int, embedding: list[float],
                      backend: str) -> None:
        if table not in ("tickets", "feature_requests", "kb"):
            raise ValueError(table)
        with self._lock:
            self.conn.execute(
                f"UPDATE {table} SET embedding=?, embed_backend=?, embed_dim=? WHERE id=?",
                (pack(embedding), backend, len(embedding), row_id),
            )
            self.conn.commit()

    # --- audit ---

    def record_audit(self, actor: str | None, action: str, target: str | None = None,
                     **detail) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO audit (ts, actor, action, target, detail_json) VALUES (?,?,?,?,?)",
                (int(time.time()), actor, action, target,
                 json.dumps(detail, ensure_ascii=False) if detail else None),
            )
            self.conn.commit()

    # --- stats (weekly digest) ---

    def counts(self) -> dict[str, int]:
        c = self.conn
        return {
            "open_tickets": c.execute(
                "SELECT COUNT(*) FROM tickets WHERE status IN ('open','claimed','escalated')"
            ).fetchone()[0],
            "resolved_tickets": c.execute(
                "SELECT COUNT(*) FROM tickets WHERE status='resolved'"
            ).fetchone()[0],
            "open_feature_requests": c.execute(
                "SELECT COUNT(*) FROM feature_requests WHERE status IN ('open','planned')"
            ).fetchone()[0],
        }
