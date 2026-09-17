"""Sietch community blueprint market: portal-side data layer.

Thin layer over admin.db (portal_blueprint_market + portal_blueprint_publish_log)
plus on-disk blueprint JSON blobs under BLUEPRINT_BLOB_DIR. This is OUR metadata,
not game-state. A player publishes one of their OWN bases; the blueprint JSON is a
SERVER-SIDE re-export via the ownership-gated relay (the route does that), so every
listing is authentic. Authorization + the relay re-export + ownership clamp are
enforced by the caller/route; this module never touches dune.* and never trusts a
client-supplied blueprint.

Blobs live off-row (one '<publish_id>.json' per publish) so admin.db stays small and
the ~448 KB-class JSON streams straight from disk. Filenames are integer-derived;
request input never enters a filesystem path.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from config import BLUEPRINT_BLOB_DIR
from database import get_db

logger = logging.getLogger("blueprint_market")

TITLE_MAX = 80
DESC_MAX = 400
TAGS_MAX = 8
PURPOSE_MAX = 3
PUBLISH_BLOB_MAX = 2_000_000   # bytes; ~4x the largest sampled real blueprint

# The closed vocabulary the publish form offers, and the only tags a player may
# write. They live in their own `user_tags` column rather than in `tags` because
# every publish re-derives `tags` from the blueprint pieces, which would eat
# them. Fixed order so the intersect + cap below keeps the same three whatever
# order the client sent.
PURPOSE_TAGS = ["Starter", "Main base", "Outpost", "Spice camp",
                "Refinery", "Defensive", "Showcase", "PvP"]

_CATALOG_PATH = os.path.join(
    os.path.dirname(__file__), "static", "js", "data", "solido-piece-catalog.json")

# Structural catalog-category -> friendly tag (spec vocabulary). Functional
# building_type substrings (below) override these for Storage/Crafting/Defense.
_CAT_TAG = {
    "foundation": "Foundation", "foundation_wedge": "Foundation",
    "floor": "Floor",
    "wall": "Wall", "wall_half": "Wall",
    "door": "Door",
    "roof": "Roof",
    "ramp": "Stairs",
    "railing": "Decoration", "pillar": "Decoration", "corner": "Decoration",
    "prop": "Decoration", "prop_large": "Decoration",
}
_FUNCTIONAL = (
    ("Storage", ("storage", "container", "chest", "crate")),
    ("Crafting", ("fabricator", "refinery", "extraction", "station",
                  "bench", "crafting", "workbench")),
    ("Defense", ("turret", "cannon", "defense", "blaster")),
)
# Fixed display order so tags + the cap are deterministic.
_TAG_ORDER = ["Foundation", "Wall", "Floor", "Roof", "Stairs", "Door",
              "Storage", "Crafting", "Defense", "Decoration"]


def _now() -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS', matching SQLite datetime('now')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_catalog_cache = None


def _catalog() -> dict:
    global _catalog_cache
    if _catalog_cache is None:
        try:
            with open(_CATALOG_PATH, "r", encoding="utf-8") as fh:
                _catalog_cache = json.load(fh)
        except Exception:
            logger.warning("blueprint_market: catalog load failed", exc_info=True)
            _catalog_cache = {"patterns": [], "factions": {}}
    return _catalog_cache


def _category_of(bt: str, patterns: list) -> str:
    name = (bt or "").lower()
    for needle, cat in patterns:
        if needle in name:
            return cat
    return "default"


def _faction_of(bt: str, factions: dict) -> str:
    name = (bt or "").lower()
    for key, f in factions.items():
        for p in (f.get("prefixes") or []):
            if name.startswith(p):
                return key
    return "neutral"


def _size_band(piece_count: int) -> str:
    if piece_count < 200:
        return "Small"
    if piece_count < 1000:
        return "Medium"
    if piece_count < 3000:
        return "Large"
    return "Massive"


# C0/C1 controls and DEL, minus tab/LF/CR: those three survive one step longer
# so the whitespace collapse below turns them into a space instead of gluing the
# words either side together.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _clean_description(text: str | None) -> str | None:
    """Player free text bound for a public card: drop control characters,
    collapse every whitespace run to one space, then cap. Truncated, never
    rejected: someone who pastes 401 characters gets 400 of them back, not a
    refusal they have to re-edit to get past."""
    if not text:
        return None
    return " ".join(_CONTROL_RE.sub("", text).split())[:DESC_MAX] or None


def _clean_user_tags(tags) -> list:
    """Intersect the client's purpose tags against the closed vocabulary and
    cap. Anything outside PURPOSE_TAGS is dropped silently, so a stale client
    cannot write a tag the filter rail will never offer back."""
    if not tags:
        return []
    wanted = {t for t in tags if isinstance(t, str)}
    return [t for t in PURPOSE_TAGS if t in wanted][:PURPOSE_MAX]


def _merge_tags(derived: list, purpose: list) -> list:
    """Purpose tags first, then the server-derived ones in their stored
    _TAG_ORDER sequence, capped at TAGS_MAX. Purpose is already capped at
    PURPOSE_MAX, well under that, so the truncation only ever eats derived tags:
    what the player said their own base is FOR always survives."""
    merged = list(purpose)
    for t in derived:
        if t not in merged:
            merged.append(t)
    return merged[:TAGS_MAX]


def derive_metadata(blueprint: dict) -> dict:
    """Compute card metadata from the authentic re-export. Everything here is
    server-derived from the blueprint pieces — no client free-text except the
    title/description handled in create_or_update."""
    instances = blueprint.get("instances") or []
    placeables = blueprint.get("placeables") or []
    pentashields = blueprint.get("pentashields") or []
    instance_count = len(instances)
    placeable_count = len(placeables)
    pentashield_count = len(pentashields)
    piece_count = instance_count + placeable_count + pentashield_count

    cat = _catalog()
    patterns = cat.get("patterns") or []
    factions = cat.get("factions") or {}

    tag_set = set()
    faction_tally = {}
    has_paid = False
    for it in list(instances) + list(placeables):
        bt = it.get("building_type") or ""
        low = bt.lower()
        if low.startswith("mtx"):
            has_paid = True
        # functional tag takes priority, else structural category tag
        functional = None
        for tag, needles in _FUNCTIONAL:
            if any(n in low for n in needles):
                functional = tag
                break
        if functional:
            tag_set.add(functional)
        else:
            ckey = _category_of(bt, patterns)
            mapped = _CAT_TAG.get(ckey)
            if mapped:
                tag_set.add(mapped)
        fk = _faction_of(bt, factions)
        if fk != "neutral":
            faction_tally[fk] = faction_tally.get(fk, 0) + 1
    if pentashield_count > 0:
        tag_set.add("Defense")

    tags = [t for t in _TAG_ORDER if t in tag_set]
    tags.append(_size_band(piece_count))
    tags = tags[:TAGS_MAX]

    faction = max(faction_tally, key=faction_tally.get) if faction_tally else "neutral"

    return {
        "instance_count": instance_count,
        "placeable_count": placeable_count,
        "pentashield_count": pentashield_count,
        "piece_count": piece_count,
        "tags": tags,
        "faction": faction,
        "has_paid_pieces": has_paid,
        "default_title": (blueprint.get("name") or "Untitled Base"),
    }


def _blob_path(publish_id: int) -> str:
    return os.path.join(BLUEPRINT_BLOB_DIR, f"{publish_id}.json")


def thumb_path(publish_id: int) -> str:
    """Listing thumbnail PNG (publisher-rendered at publish time). Filename is
    integer-derived; request input never enters the path."""
    return os.path.join(BLUEPRINT_BLOB_DIR, f"{int(publish_id)}_thumb.png")


def has_thumbnail(publish_id: int) -> bool:
    return os.path.exists(thumb_path(publish_id))


def save_thumbnail(publish_id: int, body: bytes) -> None:
    os.makedirs(BLUEPRINT_BLOB_DIR, exist_ok=True)
    tmp = thumb_path(publish_id) + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(body)
    os.replace(tmp, thumb_path(publish_id))


def _write_blob(publish_id: int, body: bytes) -> int:
    os.makedirs(BLUEPRINT_BLOB_DIR, exist_ok=True)
    with open(_blob_path(publish_id), "wb") as fh:
        fh.write(body)
    return len(body)


def create_or_update(*, account_id: int, discord_id: str, author_name: str,
                     game_bp_id: int, title: str, description: str,
                     blueprint: dict, meta: dict,
                     user_tags: list[str] | None = None) -> dict:
    """Upsert the LIVE row for (account_id, game_bp_id) and (re)write its blob.

    Re-publishing a still-live listing updates the existing row in place and
    preserves download_count. A previously unpublished/removed listing does NOT
    block a fresh publish (per the partial unique index) and yields a brand-new
    row with a fresh download counter. title/description are trimmed + capped
    here; tags JSON-encoded. user_tags are the player's purpose tags, intersected
    against PURPOSE_TAGS and written to their own column so the derived `tags`
    rewritten above them cannot eat them; None means the caller has nothing to
    say about them and an existing row keeps what it has, which is what stops the
    V1 publish lane wiping tags it does not know exist. The caller has already
    enforced the rate limit, the relay re-export, the ownership clamp, and the
    blob size cap."""
    title = (title or "").strip()[:TITLE_MAX] or meta["default_title"][:TITLE_MAX]
    description = _clean_description(description)
    tags_json = json.dumps(meta["tags"])
    body = json.dumps(blueprint, ensure_ascii=False).encode("utf-8")
    now = _now()
    account_id = int(account_id)
    game_bp_id = int(game_bp_id)

    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT publish_id, user_tags FROM portal_blueprint_market "
            "WHERE account_id = ? AND game_bp_id = ? AND status = 'published'",
            (account_id, game_bp_id),
        ).fetchone()

        if existing:
            publish_id = int(existing["publish_id"])
            # A list, empty included, sets the purpose tags. None leaves the
            # column exactly as it is: silence is not an instruction to clear.
            user_tags_json = (existing["user_tags"] if user_tags is None
                              else json.dumps(_clean_user_tags(user_tags)))
            blob_bytes = _write_blob(publish_id, body)
            conn.execute(
                """UPDATE portal_blueprint_market SET
                       discord_id = ?, author_name = ?, title = ?,
                       description = ?, tags = ?, user_tags = ?, faction = ?,
                       has_paid_pieces = ?, instance_count = ?,
                       placeable_count = ?, pentashield_count = ?,
                       piece_count = ?, blob_path = ?, blob_bytes = ?,
                       status = 'published', updated_at = ?
                   WHERE publish_id = ?""",
                (discord_id or None, author_name, title, description, tags_json,
                 user_tags_json, meta["faction"], 1 if meta["has_paid_pieces"] else 0,
                 meta["instance_count"], meta["placeable_count"],
                 meta["pentashield_count"], meta["piece_count"],
                 f"{publish_id}.json", blob_bytes, now, publish_id),
            )
        else:
            # A brand new row has nothing to preserve, so None is simply empty.
            user_tags_json = json.dumps(_clean_user_tags(user_tags))
            cur = conn.execute(
                """INSERT INTO portal_blueprint_market
                       (account_id, discord_id, author_name, game_bp_id, title,
                        description, tags, user_tags, faction, has_paid_pieces,
                        instance_count, placeable_count, pentashield_count,
                        piece_count, blob_path, blob_bytes, download_count,
                        status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, 0,
                           'published', ?, ?)""",
                (account_id, discord_id or None, author_name, game_bp_id, title,
                 description, tags_json, user_tags_json, meta["faction"],
                 1 if meta["has_paid_pieces"] else 0, meta["instance_count"],
                 meta["placeable_count"], meta["pentashield_count"],
                 meta["piece_count"], now, now),
            )
            publish_id = int(cur.lastrowid)
            blob_bytes = _write_blob(publish_id, body)
            conn.execute(
                "UPDATE portal_blueprint_market SET blob_path = ?, blob_bytes = ? "
                "WHERE publish_id = ?",
                (f"{publish_id}.json", blob_bytes, publish_id),
            )

        conn.execute(
            "INSERT INTO portal_blueprint_publish_log "
            "(account_id, publish_id, game_bp_id, published_at) VALUES (?, ?, ?, ?)",
            (account_id, publish_id, game_bp_id, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM portal_blueprint_market WHERE publish_id = ?",
            (publish_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row)


def sync_listing_title_on_rename(account_id: int, game_bp_id: int,
                                 new_title: str, old_name: str | None) -> int | None:
    """Bring a still-published listing's title along when the player renames the
    in-game blueprint — but ONLY when that title was the default-ish one (empty,
    "Untitled Base", or the old in-game name it was derived from), so a
    deliberately-custom listing title is never clobbered. Returns the updated
    publish_id, or None if nothing was synced."""
    new_title = (new_title or "").strip()[:TITLE_MAX]
    if not new_title:
        return None
    account_id = int(account_id)
    game_bp_id = int(game_bp_id)
    syncable = {"", "untitled base"}
    if old_name and old_name.strip():
        syncable.add(old_name.strip().lower())

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT publish_id, title FROM portal_blueprint_market "
            "WHERE account_id = ? AND game_bp_id = ? AND status = 'published'",
            (account_id, game_bp_id),
        ).fetchone()
        if not row:
            return None
        current = (row["title"] or "").strip()
        if current.lower() not in syncable or current == new_title:
            return None  # custom title (or already in sync) — leave it
        conn.execute(
            "UPDATE portal_blueprint_market SET title = ?, updated_at = ? "
            "WHERE publish_id = ?",
            (new_title, _now(), int(row["publish_id"])),
        )
        conn.commit()
        return int(row["publish_id"])
    finally:
        conn.close()


def _decode(row) -> dict:
    """One row with both tag columns resolved. The merge lands in `tags`, the
    field every read path already renders, so nothing downstream has to learn a
    second one; `user_tags` rides alongside because the publish form has to know
    which of the closed vocabulary the player picked."""
    d = dict(row)
    try:
        derived = json.loads(d.get("tags") or "[]")
    except (ValueError, TypeError):
        derived = []
    try:
        purpose = json.loads(d.get("user_tags") or "[]")
    except (ValueError, TypeError):
        purpose = []
    d["user_tags"] = purpose
    d["tags"] = _merge_tags(derived, purpose)
    return d


def tag_catalog() -> dict:
    """Everything the gallery filter rail and the publish form are allowed to
    offer, read out of the three places the writes already use. Derived rather
    than restated so a tag can never be offered that no listing can carry: one
    probe per band through _size_band means a moved boundary or a renamed band
    follows on its own."""
    bands = []
    for probe in (0, 200, 1000, 3000):
        band = _size_band(probe)
        if band not in bands:
            bands.append(band)
    return {"tags": list(_TAG_ORDER), "size_bands": bands,
            "purposes": list(PURPOSE_TAGS)}


def list_published(sort: str, tag: str | None, limit: int, offset: int) -> list[dict]:
    """Published listings only. sort in {'new','popular'}."""
    order = ("download_count DESC, created_at DESC" if sort == "popular"
             else "created_at DESC")
    params = []
    where = "status = 'published'"
    if tag:
        # One chip to the client, two columns underneath: a purpose tag lives in
        # user_tags and a derived one in tags, and a filter that scanned only the
        # derived column would return nothing for half the rail.
        where += " AND (tags LIKE ? OR user_tags LIKE ?)"
        like = f'%"{tag}"%'
        params.extend([like, like])
    params.extend([int(limit), int(offset)])
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT * FROM portal_blueprint_market WHERE {where} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [_decode(r) for r in rows]


def get_public(publish_id: int) -> dict | None:
    """One published row, or None if missing/unpublished/removed."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM portal_blueprint_market "
            "WHERE publish_id = ? AND status = 'published'",
            (int(publish_id),),
        ).fetchone()
    finally:
        conn.close()
    return _decode(row) if row else None


def get_owned(publish_id: int, account_ids: set) -> dict | None:
    """Row regardless of status IF owned by one of account_ids."""
    ids = [int(a) for a in account_ids]
    if not ids:
        return None
    placeholders = ",".join("?" * len(ids))
    conn = get_db()
    try:
        row = conn.execute(
            f"SELECT * FROM portal_blueprint_market "
            f"WHERE publish_id = ? AND account_id IN ({placeholders})",
            [int(publish_id), *ids],
        ).fetchone()
    finally:
        conn.close()
    return _decode(row) if row else None


def list_by_accounts(account_ids: set) -> list[dict]:
    """The caller's own listings (any status) for inline My Bases state."""
    ids = [int(a) for a in account_ids]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT * FROM portal_blueprint_market "
            f"WHERE account_id IN ({placeholders}) AND status != 'removed' "
            f"ORDER BY updated_at DESC",
            ids,
        ).fetchall()
    finally:
        conn.close()
    return [_decode(r) for r in rows]


def increment_download(publish_id: int) -> None:
    """Single-statement increment; no read-modify-write race."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE portal_blueprint_market SET download_count = download_count + 1 "
            "WHERE publish_id = ? AND status = 'published'",
            (int(publish_id),),
        )
        conn.commit()
    finally:
        conn.close()


def unpublish(publish_id: int, account_ids: set) -> bool:
    """Author self-removal. Blob retained. True if a row changed."""
    ids = [int(a) for a in account_ids]
    if not ids:
        return False
    placeholders = ",".join("?" * len(ids))
    conn = get_db()
    try:
        cur = conn.execute(
            f"UPDATE portal_blueprint_market "
            f"SET status = 'unpublished', updated_at = ? "
            f"WHERE publish_id = ? AND account_id IN ({placeholders}) "
            f"AND status = 'published'",
            [_now(), int(publish_id), *ids],
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def admin_state(publish_id: int) -> dict | None:
    """The moderation-relevant fields of one listing, or None.

    Read BEFORE a takedown so the audit row carries the state that was reversed.
    "status was published, blob 41 KB" is what makes an undo possible six weeks
    later; the row after the write says nothing about what it used to be."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT publish_id, account_id, author_name, title, status, "
            "       blob_path, blob_bytes, download_count, updated_at "
            "  FROM portal_blueprint_market WHERE publish_id = ?",
            (int(publish_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def admin_unpublish(publish_id: int) -> bool:
    """Moderation HIDE. status='unpublished', blob KEPT, no account clamp (the
    admin router is the gate). The reversible half of the takedown pair, and
    admin_republish below is what reverses it. The author cannot: create_or_update
    matches 'published' rows only, so an author who publishes the same base again
    opens a SECOND listing with a fresh counter and orphans this one.

    Unlike unpublish() this does not require the row to be 'published', so a
    second press is a no-op rather than a confusing failure."""
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE portal_blueprint_market SET status = 'unpublished', updated_at = ? "
            "WHERE publish_id = ? AND status != 'removed'",
            (_now(), int(publish_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def admin_republish(publish_id: int) -> bool:
    """Moderation un-hide: 'unpublished' back to 'published'. The only way a
    hidden listing comes back, and the inverse admin_unpublish never had.

    'removed' is deliberately out of scope. That status means the blob was
    deleted, so re-publishing would put a card back on the gallery with nothing
    behind it. The status clause is the refusal, and the router reads
    admin_state first so it can say which of the two happened. A second press
    matches no row and returns False rather than rewriting updated_at."""
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE portal_blueprint_market SET status = 'published', updated_at = ? "
            "WHERE publish_id = ? AND status = 'unpublished'",
            (_now(), int(publish_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def admin_remove(publish_id: int) -> bool:
    """Hard moderation removal: tombstone the row + delete the blob. No account
    clamp (operator-gated by the admin router). Spec'd for the v2 admin hook."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT blob_path FROM portal_blueprint_market WHERE publish_id = ?",
            (int(publish_id),),
        ).fetchone()
        if row is None:
            return False
        cur = conn.execute(
            "UPDATE portal_blueprint_market SET status = 'removed', updated_at = ? "
            "WHERE publish_id = ?",
            (_now(), int(publish_id)),
        )
        conn.commit()
    finally:
        conn.close()
    blob = (row["blob_path"] or "").strip()
    if blob:
        try:
            os.remove(os.path.join(BLUEPRINT_BLOB_DIR, os.path.basename(blob)))
        except OSError:
            pass
    # The preview goes with the blob: a removed row must leave no orphan PNG.
    try:
        os.remove(thumb_path(publish_id))
    except OSError:
        pass
    return cur.rowcount > 0


def recent_publish_count(account_id: int, since_utc: str) -> int:
    """Windowed publish-event count for the daily rate limit (counts every
    publish including re-publishes of the same base)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM portal_blueprint_publish_log "
            "WHERE account_id = ? AND published_at >= ?",
            (int(account_id), since_utc),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"]) if row else 0


def read_blob(row: dict) -> bytes | None:
    """Path-safe blob read; None if missing (callers soft-degrade)."""
    name = os.path.basename((row.get("blob_path") or "").strip())
    if not name:
        return None
    try:
        with open(os.path.join(BLUEPRINT_BLOB_DIR, name), "rb") as fh:
            return fh.read()
    except OSError:
        return None
