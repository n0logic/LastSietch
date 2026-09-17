"""Portal identity-quiz generator + verifier (P4).

Constraints (no question may depend on data the player cannot see):
- Every question MUST be answerable from data the player can read in-game.
- Disqualified kinds (coords, account_id, JSONB internals, IDs, etc.) are
  hardcoded into DISQUALIFIED_KINDS and unit-asserted.

v1 scope (per team-lead defaults):
- 5 allowed kinds: char_level, current_map, faction_alignment, pledged_house,
  character_name.
- Primary 3 questions: char_level / current_map / faction_alignment.
- Fallback ladder when a primary kind is unavailable: pledged_house, then
  character_name (last-resort). DISQUALIFIED kinds never appear.

Live state reads ALWAYS go through routers.dune cached helpers — zero direct
SQL against dune.* from portal code.

Answer normalization: NFKD ASCII lowercase strip. Correct-answer hashes
(sha256 hex) are stored in portal_link_attempts; the route re-hashes the
submitted answer with the SAME normalization and uses hmac.compare_digest.
The plaintext correct answer never leaves this module after generation.
"""
import hashlib
import hmac
import logging
import random
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

_SHARD_SUFFIX_RE = re.compile(r"_\d+$")

import mirror
from routers.dune import (
    MAP_DISPLAY_NAMES,
    _cached_player_tags,
    _cached_progression_snapshot_full,
    cached_player_progress,
)

logger = logging.getLogger(__name__)

ALLOWED_KINDS = frozenset(
    {"char_level", "current_map", "faction_alignment", "pledged_house", "character_name"}
)

DISQUALIFIED_KINDS = frozenset(
    {
        "world_x", "world_y", "world_z",
        "partition", "dimension",
        "account_id", "controller_id", "pawn_id", "entity_id",
        "xp_to_next", "exact_xp",
        "sietch", "subregion",
        "last_login_ts",
        "actor_jsonb",
        "ip_addr", "hwid", "steam_id", "playfab_id",
    }
)

FACTION_CHOICES = ("Atreides", "Harkonnen", "Unaligned")


@dataclass
class GeneratedQuestion:
    kind: str
    prompt: str
    format: str          # 'radio' (selection box), 'text', or 'numeric'
    correct: str         # plaintext (server-side only; never leaves module)
    choices: Optional[list]  # None for numeric; list[str] for radio
    input_name: str      # 'q0', 'q1', 'q2'


@dataclass
class GeneratedQuiz:
    questions: list      # list[GeneratedQuestion]
    correct_hashes: list # parallel list[str]; same length as questions
    kinds: list          # parallel list[str]


def _normalize(answer: str) -> str:
    """NFKD-decompose, strip non-ASCII (handles diacritics on character names
    per architect Q3 recommendation), lowercase, strip."""
    if answer is None:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(answer))
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return ascii_only.strip().lower()


def hash_answer(answer: str) -> str:
    return hashlib.sha256(_normalize(answer).encode("utf-8")).hexdigest()


def verify_answer(submitted: str, expected_hash: str) -> bool:
    """Constant-time compare of SHA-256 hex strings."""
    if not expected_hash:
        return False
    submitted_hash = hash_answer(submitted)
    return hmac.compare_digest(submitted_hash, expected_hash)


# --- Player-state extraction (via cached relay helpers) ---


async def _find_player_in_snapshot(account_id: int) -> Optional[dict]:
    """Returns raw player dict (with account_id, char_name, lvl, online_status)
    from the snapshot, or None if no row exists for this account."""
    try:
        raw = await _cached_progression_snapshot_full()
    except Exception as exc:
        logger.warning("portal_quiz: snapshot fetch failed: %s", exc)
        return None
    for p in (raw.get("players") or []):
        if int(p.get("account_id") or 0) == int(account_id):
            return p
    return None


async def _read_player_tags(account_id: int) -> list:
    """Returns the tags list, or [] if unavailable."""
    try:
        # Fast path: local mirror tags section (self-gates on flag + staleness).
        raw = mirror.get_section(account_id, "tags")
        if raw is None:
            raw = await _cached_player_tags(str(account_id))
    except Exception as exc:
        logger.warning("portal_quiz: tags fetch failed: %s", exc)
        return []
    tags = raw.get("tags") or []
    # Tag entries may be strings or dicts depending on relay shape — normalize
    # to a flat list of strings (best-effort).
    flat: list = []
    for t in tags:
        if isinstance(t, str):
            flat.append(t)
        elif isinstance(t, dict):
            name = t.get("name") or t.get("tag") or t.get("id")
            if name:
                flat.append(str(name))
    return flat


def _derive_faction(tags: list) -> Optional[str]:
    """Determine the player's binding faction alignment.

    Funcom tracks two distinct concepts:
      1. Binding alignment: 'DialogueFlags.Factions.AlignedAtreides' or
         '...AlignedHarkonnen'. Exactly one of these is the player's CURRENT
         choice. This is what the quiz should ask about.
      2. Tier rolls: 'Faction.Atreides.Tier_N' / 'Faction.Harkonnen.Tier_N'.
         Both families can coexist (rolls earned with one side before
         pledging the other; carried-over rolls on a transferred char).

    Prefer the binding alignment when present. Fall back to tier presence
    only when the alignment flag is missing (very fresh or pre-pledge char).
    """
    if "DialogueFlags.Factions.AlignedAtreides" in tags:
        return "Atreides"
    if "DialogueFlags.Factions.AlignedHarkonnen" in tags:
        return "Harkonnen"
    # Fallback: tier-only heuristic.
    has_atreides = any(t.startswith("Faction.Atreides") for t in tags)
    has_harkonnen = any(t.startswith("Faction.Harkonnen") for t in tags)
    if has_atreides and not has_harkonnen:
        return "Atreides"
    if has_harkonnen and not has_atreides:
        return "Harkonnen"
    if not has_atreides and not has_harkonnen:
        return "Unaligned"
    return None  # ambiguous AND no binding flag — skip


def _current_faction(progress: Optional[dict], tags: list) -> Optional[str]:
    """Authoritative CURRENT faction alignment.

    The alignment TAGS (`DialogueFlags.Factions.Aligned*`) retain the player's
    ORIGINAL binding flag after a faction CHANGE (start Atreides, later switch to
    Harkonnen -> the AlignedAtreides tag persists), so `_derive_faction(tags)`
    mis-reports switched players and made the quiz reject their correct current
    answer (FurtivePygmy 2026-06-17). The live progression `faction` block tracks
    the CURRENT choice, so prefer it; fall back to the tag heuristic only when
    progression carries no faction (relay miss / genuinely unaligned)."""
    fac = (progress or {}).get("faction") or {}
    name = fac.get("faction_name")
    if name in ("Atreides", "Harkonnen"):
        return name
    fid = fac.get("faction_id")
    if fid == 1:
        return "Atreides"
    if fid == 2:
        return "Harkonnen"
    return _derive_faction(tags or [])


def _derive_pledged_house(tags: list) -> Optional[str]:
    """Look for the Landsraad pledge tag if present. v1 scoped narrowly —
    if the tag namespace shape changes we just fall back to character_name."""
    for t in tags:
        # Pledge tag format observed in Funcom audits: 'Landsraad.PledgedHouse.<HouseName>'
        if t.startswith("Landsraad.PledgedHouse."):
            return t[len("Landsraad.PledgedHouse."):]
    return None


# Keys a roster payload could plausibly carry a partition id under. Read in
# order, player row first, then the map entry. NOTHING is derived from the map
# name: Hagga sietches share a map string, so inferring a partition from it would
# be a guess, and a wrong sietch is worse than no sietch.
_ROSTER_PARTITION_KEYS = ("partition_id", "partition", "partition_index")


def _roster_partition(payload: dict, map_entry: dict, player: dict) -> Optional[int]:
    """The partition (sietch instance) id for one roster row, or None when the
    roster cannot say. Defensive by construction: the deployed roster producer
    (scripts/dune-roster.py) emits no partition at all, so today this answers
    None for everyone. It is written to read one if a payload ever carries one,
    and to keep answering None rather than inventing one if it does not."""
    for src in (player, map_entry):
        if not isinstance(src, dict):
            continue
        for key in _ROSTER_PARTITION_KEYS:
            val = src.get(key)
            if isinstance(val, bool) or val is None:
                continue
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    # A side table joined on the raw map name, used ONLY when it resolves to one
    # partition. Two candidates for a map means the roster cannot place this
    # player, which is a None, not a coin flip.
    rows = payload.get("partitions")
    if isinstance(rows, list):
        want = map_entry.get("map") or ""
        hits = []
        for row in rows:
            if not isinstance(row, dict) or (row.get("map") or "") != want:
                continue
            for key in _ROSTER_PARTITION_KEYS + ("id",):
                val = row.get(key)
                if val is None or isinstance(val, bool):
                    continue
                try:
                    hits.append(int(val))
                except (TypeError, ValueError):
                    pass
                break
        if len(hits) == 1:
            return hits[0]
    return None


async def _roster_index() -> dict:
    """{normalized character name: {map, map_raw, partition}} for every ONLINE
    player, built from the shared TTL-cached roster. Through the cache, never the
    relay directly: this is read on every account card, character card and Home
    read, and an uncached roster fetch there is one SSH round trip per page load
    per player. One pass, so a caller placing many names (the Home sietch row)
    does not rescan the roster once per name. {} on any failure."""
    from routers.dune import cached_roster
    try:
        payload = await cached_roster()
    except Exception as exc:
        logger.warning("portal_quiz: roster fetch failed: %s", exc)
        return {}
    index = {}
    for m in (payload.get("maps") or []):
        raw_map = m.get("map") or ""
        # Strip trailing _<digits> shard suffix before friendly lookup. Use a
        # regex so multi-digit shards (_10, _12, ...) also collapse.
        base = _SHARD_SUFFIX_RE.sub("", raw_map)
        friendly = MAP_DISPLAY_NAMES.get(raw_map) or MAP_DISPLAY_NAMES.get(base) or raw_map
        for p in (m.get("players") or []):
            key = _normalize(p.get("name") or "")
            if not key:
                continue
            # setdefault, not assignment: first match wins, which is what the
            # per-name scan this replaced returned.
            index.setdefault(key, {
                "map": friendly,
                "map_raw": raw_map or None,
                "partition": _roster_partition(payload, m, p),
            })
    return index


async def _roster_map_index() -> dict:
    """{normalized character name: friendly map}, the name-to-map slice of
    `_roster_index` for callers that only need to label a row."""
    return {k: v["map"] for k, v in (await _roster_index()).items()}


async def _derive_current_map(char_name: str) -> Optional[str]:
    """Use the cached roster to locate the character's current map. Roster is
    online-only; offline characters return None."""
    if not char_name:
        return None
    placement = (await _roster_index()).get(_normalize(char_name))
    return placement["map"] if placement else None


# --- Question builders ---


def _level_distractors(level: int, rng: random.Random) -> list:
    """Two distinct plausible level values near the true level (all > 0, never
    equal to it) — so char_level renders as a 3-option selection box (correct + 2)
    instead of a free-text number field (the old format was a common failure point:
    players leveled up mid-flow, or fat-fingered the number)."""
    pool = sorted(
        {level + off for off in (-2, -1, 1, 2, 3, 4, 5) if level + off > 0 and level + off != level}
    )
    rng.shuffle(pool)
    return pool[:2]


def _q_char_level(level: int, rng: random.Random) -> GeneratedQuestion:
    choices = [str(level)] + [str(d) for d in _level_distractors(level, rng)]
    rng.shuffle(choices)
    return GeneratedQuestion(
        kind="char_level",
        prompt="What is your character's current level?",
        format="radio",
        correct=str(level),
        choices=choices,
        input_name="",
    )


def _q_current_map(current_map: str, rng: random.Random) -> GeneratedQuestion:
    all_maps = list(MAP_DISPLAY_NAMES.values())
    distractors = [m for m in all_maps if m != current_map]
    rng.shuffle(distractors)
    choices = [current_map] + distractors[:2]
    rng.shuffle(choices)
    return GeneratedQuestion(
        kind="current_map",
        prompt="What map is your character currently in?",
        format="radio",
        correct=current_map,
        choices=choices,
        input_name="",
    )


def _q_faction(faction: str, rng: random.Random) -> GeneratedQuestion:
    # Faction quiz always shows the three canonical choices in random order —
    # the player picks the one matching their HUD faction icon.
    choices = list(FACTION_CHOICES)
    rng.shuffle(choices)
    return GeneratedQuestion(
        kind="faction_alignment",
        prompt="What faction is your character aligned with?",
        format="radio",
        correct=faction,
        choices=choices,
        input_name="",
    )


def _q_pledged_house(house: str, all_houses: list, rng: random.Random) -> GeneratedQuestion:
    distractors = [h for h in all_houses if h != house]
    rng.shuffle(distractors)
    choices = [house] + distractors[:2]
    rng.shuffle(choices)
    return GeneratedQuestion(
        kind="pledged_house",
        prompt="Which house are you currently pledged to in the Landsraad?",
        format="radio",
        correct=house,
        choices=choices,
        input_name="",
    )


def _q_character_name(char_name: str, name_pool: list, rng: random.Random) -> GeneratedQuestion:
    # Selection box: the real character name among up to three OTHER real
    # character names drawn from the live roster. This replaces the old
    # free-text exact-match, which was the worst offender for false failures
    # (mobile autocapitalize / autocorrect, trailing spaces, etc.). We fall
    # back to free-text only if the roster can't supply 3 distinct distractors.
    target_norm = _normalize(char_name)
    seen = {target_norm}
    distractors: list = []
    pool = list(name_pool)
    rng.shuffle(pool)
    for n in pool:
        nn = _normalize(n)
        if not nn or nn in seen:
            continue
        seen.add(nn)
        distractors.append(n)
        if len(distractors) == 3:
            break
    if len(distractors) < 3:
        # Not enough distinct names for a fair selection — keep free-text.
        # Format 'text' renders <input type="text"> (a name like "Liet-Kynes"
        # would fail HTML5 numeric validation).
        return GeneratedQuestion(
            kind="character_name",
            prompt="What is the exact character name on this account?",
            format="text",
            correct=char_name,
            choices=None,
            input_name="",
        )
    choices = [char_name] + distractors
    rng.shuffle(choices)
    return GeneratedQuestion(
        kind="character_name",
        prompt="Which of these is your character's name?",
        format="radio",
        correct=char_name,
        choices=choices,
        input_name="",
    )


# --- Public API ---


async def generate_quiz(account_id: int, *, rng: Optional[random.Random] = None) -> Optional[GeneratedQuiz]:
    """Generate a 3-question quiz for the given account. Returns None if we
    cannot read enough player state to build at least 3 distinct questions
    (e.g. relay unreachable + no cached snapshot). The caller surfaces a
    503-style 'try again shortly' page in that case."""
    rng = rng or random.Random()

    # Single snapshot fetch (cached): locate this player's row AND collect a
    # pool of other character names to use as character_name selection-box
    # distractors.
    try:
        raw = await _cached_progression_snapshot_full()
    except Exception as exc:
        logger.warning("portal_quiz: snapshot fetch failed: %s", exc)
        return None
    snapshot = None
    name_pool: list = []
    for p in (raw.get("players") or []):
        nm = p.get("char_name") or p.get("name") or ""
        if int(p.get("account_id") or 0) == int(account_id):
            snapshot = p
        elif nm:
            name_pool.append(nm)
    if not snapshot:
        return None
    char_name = snapshot.get("char_name") or snapshot.get("name") or ""
    level = snapshot.get("lvl")

    tags = await _read_player_tags(account_id)
    # Faction from the progression block (current choice), NOT the alignment tags
    # (which keep the ORIGINAL flag after a faction switch). See _current_faction.
    # Mirror-first (same source as the account card's _load_progress): the live
    # relay returns faction only while the player is online, but the mirror
    # retains the last-known faction, so this stays correct for offline linkers.
    progress = None
    try:
        import mirror
        progress = mirror.get_section(account_id, "progress")
        if progress is None:
            progress = await cached_player_progress(account_id)
    except Exception as exc:
        logger.warning("portal_quiz: progress fetch failed: %s", exc)
    faction = _current_faction(progress, tags)
    pledged_house = _derive_pledged_house(tags)
    current_map = await _derive_current_map(char_name)

    candidates: list = []

    if isinstance(level, int) and level > 0:
        candidates.append(_q_char_level(level, rng))

    if current_map:
        candidates.append(_q_current_map(current_map, rng))

    if faction in FACTION_CHOICES:
        candidates.append(_q_faction(faction, rng))

    if pledged_house:
        # We don't have a known-houses list at runtime; use a static set of
        # major Landsraad houses for distractors. Acceptable since the pledge
        # tag itself is the source of truth.
        major_houses = ["Atreides", "Harkonnen", "Corrino", "Vernius", "Moritani", "Richese"]
        candidates.append(_q_pledged_house(pledged_house, major_houses, rng))

    if char_name:
        # Always available; serves as the universal fallback to fill to 3.
        candidates.append(_q_character_name(char_name, name_pool, rng))

    # Drop duplicates by kind (character_name+faction_alignment etc never
    # overlap, but defensive) and clamp to 3.
    seen_kinds: set = set()
    selected: list = []
    for q in candidates:
        if q.kind in seen_kinds:
            continue
        if q.kind not in ALLOWED_KINDS:
            # Hard refusal — anything that snuck past ALLOWED_KINDS is a bug.
            continue
        seen_kinds.add(q.kind)
        selected.append(q)
        if len(selected) == 3:
            break

    if len(selected) < 3:
        # Not enough material to verify identity safely; refuse to issue.
        return None

    # Assign positional input_name + compute hashes.
    for i, q in enumerate(selected):
        q.input_name = f"q{i}"
    correct_hashes = [hash_answer(q.correct) for q in selected]
    kinds = [q.kind for q in selected]

    return GeneratedQuiz(questions=selected, correct_hashes=correct_hashes, kinds=kinds)


def assert_all_allowed(kinds: list) -> None:
    """Defensive runtime invariant — every emitted kind must be in ALLOWED_KINDS
    and must NOT be in DISQUALIFIED_KINDS. Raises RuntimeError if violated."""
    for k in kinds:
        if k not in ALLOWED_KINDS:
            raise RuntimeError(f"portal_quiz: kind {k!r} not in ALLOWED_KINDS")
        if k in DISQUALIFIED_KINDS:
            raise RuntimeError(f"portal_quiz: kind {k!r} is in DISQUALIFIED_KINDS")
