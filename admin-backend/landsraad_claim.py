"""Portal Landsraad claim: receipt shaping and the Cielago DM queue.

The game-host writer (scripts/dune-landsraad-claim.py) returns one line per thing it
did: `lane` is backpack | exchange | unlock | rep. This module turns that into the
player-facing receipt (friendly names, quantities summed per item and destination) and
keeps applied receipts in admin.db so Cielago can DM them the next time the player is
online. Spec: the design notes (not published)

`shape_receipt` is pure. Nothing identifying leaves it: no order ids, item ids,
controller or account ids, and no internal lane names.
"""
import json
import logging

from database import get_db
from name_lookups import ITEMS as _ITEM_NAMES

logger = logging.getLogger("portal.landsraad_claim")

_IN_CHUNK = 400
SOLARI = "SolarisCoin"

# Player-facing copy. Sentence case, no em dashes.
NOTE_EXCHANGE = ("Your backpack ran out of room, so these wait at the CHOAM Exchange. "
                 "Open any exchange terminal, go to Completed, and press Take item. "
                 "They show as CANCELED, which is expected.")
NOTE_REP = "These did not fit anywhere, so they are still waiting at the house representative."
NOTE_REP_SWATCH = "House swatches are claimed at the house representative in game for now."
NOTE_REP_MIXED = "These are still waiting at the house representative. Claim them there in game."
NOTE_UNLOCK = "Swatches unlock straight into your customization menu."

# Why a line stayed at the rep, in words.
_REP_REASON = {
    "no_room": "no room",
    "swatch_at_rep": "claim swatches at the rep for now",
    "swatch_unmapped": "claim at the rep",
    "unknown_item": "claim at the rep",
}

# Writer error tokens the player can be told about. Every one of these comes from a
# transaction that rolled back or never started, so "nothing was taken" is true.
_ERROR_TEXT = {
    "player_online": "Log out of the game completely first, then wait a few minutes. "
                     "Claiming only works once your character has fully left the world.",
    "disabled": "Portal claiming is not switched on yet. Your rewards are still waiting "
                "at the house representatives.",
    "busy": "The claim service is busy. Nothing was taken. Try again in a moment.",
    "no_character": "We could not find your character. Try again in a moment.",
    "no_backpack": "We could not find your backpack. Log in once, log out, then retry.",
    "withdraw_rejected": "Your rewards changed while we were claiming them. Nothing was "
                         "taken. Refresh and try again.",
}
_DEFINITE = {
    "write_failed", "unlock_write_failed", "stack_catalog_missing", "read_failed",
    "bad_json", "bad_mode", "bad_owner_ctrl", "ns_unresolved", "db_pod_unresolved",
    "rate_limited", "csrf", "bad_request",
}
FAILED_TEXT = "That claim could not be completed. Nothing was taken. Please try again."
# A timeout or a lost response can hide a claim that DID commit. Retrying is safe (the
# ledger is already empty, so a second claim delivers nothing), but telling the player
# nothing happened would be false.
UNCONFIRMED_TEXT = ("We could not confirm the claim. Wait a minute, then check your house "
                    "rewards and your backpack before trying again. Claiming twice never "
                    "delivers twice.")

MIN_INTERVAL_S = {"preview": 3.0, "apply": 10.0}


def _house(raw: str) -> str:
    """Same rule as routers/portal._friendly_house, kept local so this module does not
    import the router: DA_HouseNovebruns -> 'House Novebruns'."""
    base = raw or ""
    if base.startswith("DA_House"):
        base = base[len("DA_House"):]
    elif base.startswith("DA_"):
        base = base[len("DA_"):]
    base = base.strip()
    return f"House {base}" if base else (raw or "Unknown house")


def shape_receipt(result: dict) -> dict:
    """Writer result -> {backpack, exchange, unlocked, at_rep, solari, empty, notes}.
    Lines are summed per (destination, item) because one reward can arrive as several
    stacks, and a player reads "T6 Gun Part x1200", not three rows of 500/500/200."""
    buckets = {"backpack": {}, "exchange": {}, "unlock": {}, "rep": {}}
    solari = 0
    for line in (result or {}).get("lines") or []:
        lane = line.get("lane")
        if lane not in buckets:
            continue
        tid = str(line.get("template") or "")
        try:
            qty = int(line.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if lane == "backpack" and tid == SOLARI:
            solari += qty
            continue
        key = tid if lane != "rep" else (tid, line.get("note") or "")
        row = buckets[lane].setdefault(key, {
            "name": _ITEM_NAMES.lookup_or_synthesize(tid) if tid != SOLARI else "Solari",
            "qty": 0,
            "houses": set(),
        })
        row["qty"] += qty
        row["houses"].add(_house(line.get("house") or ""))
        if lane == "rep":
            row["reason"] = _REP_REASON.get(line.get("note") or "", "claim at the rep")
        if lane == "unlock" and line.get("note") == "already_unlocked":
            row["already"] = True

    def rows(bucket):
        out = []
        for row in sorted(bucket.values(), key=lambda r: r["name"].lower()):
            row = dict(row)
            row["houses"] = sorted(row["houses"])
            out.append(row)
        return out

    receipt = {
        "solari": solari,
        "backpack": rows(buckets["backpack"]),
        "exchange": rows(buckets["exchange"]),
        "unlocked": rows(buckets["unlock"]),
        "at_rep": rows(buckets["rep"]),
    }
    receipt["empty"] = not (solari or receipt["backpack"] or receipt["exchange"]
                            or receipt["unlocked"])
    receipt["notes"] = {
        "exchange": NOTE_EXCHANGE if receipt["exchange"] else None,
        "at_rep": _rep_note(receipt["at_rep"]),
        "unlocked": NOTE_UNLOCK if receipt["unlocked"] else None,
    }
    return receipt


def _rep_note(rows):
    """The at-rep note names the actual reason: 'did not fit' only when room ran out."""
    if not rows:
        return None
    reasons = {r.get("reason") for r in rows}
    if reasons == {_REP_REASON["no_room"]}:
        return NOTE_REP
    if reasons == {_REP_REASON["swatch_at_rep"]}:
        return NOTE_REP_SWATCH
    return NOTE_REP_MIXED


def claim_error(mode: str, token) -> tuple:
    """Writer/relay error token -> (error, message, http_status). Only an apply can be
    'unconfirmed': a preview always rolls back."""
    if token in _ERROR_TEXT:
        status = 409 if token in ("player_online", "disabled", "busy") else 502
        return token, _ERROR_TEXT[token], status
    if mode == "apply" and token not in _DEFINITE:
        return "unconfirmed", UNCONFIRMED_TEXT, 502
    return token or "failed", FAILED_TEXT, 502


def take_slot(store: dict, account_id: int, mode: str, now: float) -> bool:
    """Per-account, per-mode rate limit. Checks and records in one step (no await in
    between), so two requests racing in one worker cannot both pass."""
    key = (int(account_id), mode)
    last = store.get(key)
    if last is not None and now - last < MIN_INTERVAL_S[mode]:
        return False
    store[key] = now
    return True


def parse_accounts(raw: str) -> frozenset:
    """Same parse as config.LANDSRAAD_CLAIM_ACCOUNTS: int ids plus the '*' wildcard."""
    return frozenset(t if t == "*" else int(t)
                     for t in (raw or "").replace(",", " ").split() if t == "*" or t.isdigit())


def enabled_for(enabled: bool, accounts: frozenset, account_id) -> bool:
    """Portal-side gate: off, on for an allowlist, or on for everyone with '*'. An
    empty allowlist is nobody (fail closed, like the game-host gate file, which is
    still the authority on the write)."""
    if not enabled:
        return False
    return "*" in accounts or int(account_id) in accounts


# ------------------------------------------------------------ admin.db queue --

def last_claim_at(account_id: int):
    """UTC time of this account's latest stored claim, or None. Readers use it to refuse
    a mirror snapshot taken before the claim (the mirror lags the game by minutes, so
    the page would otherwise reload the rewards that were just taken)."""
    try:
        conn = get_db()
        try:
            row = conn.execute("SELECT max(created_at) FROM portal_landsraad_claim WHERE account_id = ?",
                               (int(account_id),)).fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("landsraad_claim: last_claim_at(%s) failed: %s", account_id, exc)
        return None


def record_claim(account_id: int, discord_id: str, receipt: dict) -> None:
    """Store an applied claim's receipt for the DM. Best-effort by design: the claim
    already committed on the game host, and failing the response over a lost DM would
    tell the player it did not happen."""
    if receipt.get("empty"):
        return
    try:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO portal_landsraad_claim (discord_id, account_id, receipt) VALUES (?, ?, ?)",
                (str(discord_id), int(account_id), json.dumps(receipt)))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("landsraad claim receipt not stored acct=%s: %s", account_id, exc)


def pending_dms(limit: int = 1000, settle_s: int = 0) -> list[dict]:
    """Undelivered claim receipts at least settle_s old, oldest first, with the Discord
    recipient. ready_dms filters these to players who are online now."""
    age = f"-{max(0, int(settle_s))} seconds"
    conn = get_db()
    try:
        import portal_identity
        if portal_identity.credentials_installed(conn):
            rows = conn.execute(
                "SELECT c.id, v.subject AS discord_id, c.account_id, c.receipt, c.created_at "
                "FROM portal_landsraad_claim c "
                "JOIN portal_profiles p ON COALESCE(p.legacy_discord_id,'profile:'||p.id)=c.discord_id "
                "JOIN portal_profile_providers v ON v.profile_id=p.id AND v.provider='discord' "
                "WHERE c.dm_sent=0 AND p.disabled_at IS NULL AND v.revoked_at IS NULL "
                "AND c.created_at <= datetime('now', ?) "
                "ORDER BY c.id ASC LIMIT ?", (age, int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, discord_id, account_id, receipt, created_at FROM portal_landsraad_claim "
                "WHERE dm_sent=0 AND created_at <= datetime('now', ?) "
                "ORDER BY id ASC LIMIT ?", (age, int(limit))).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            receipt = json.loads(r["receipt"])
        except (TypeError, ValueError):
            continue
        out.append({"id": r["id"], "discord_id": r["discord_id"], "account_id": r["account_id"],
                    "receipt": receipt, "created_at": r["created_at"]})
    return out


async def ready_dms(online_now, limit: int, settle_s: int, scan: int = 1000) -> list[dict]:
    """Receipts to DM now: pending, older than settle_s, and the player is online.
    Scans past players who have not come back, so they never block the queue, and asks
    online_now(account_id) once per account. settle_s must cover the mirror's staleness
    so an `online` reading taken BEFORE the claim (the player had just logged out) cannot
    release it early."""
    ready, seen = [], {}
    for row in pending_dms(scan, settle_s):
        if len(ready) >= limit:
            break
        acct = row["account_id"]
        if acct not in seen:
            seen[acct] = await online_now(acct)
        if seen[acct]:
            ready.append({k: row[k] for k in ("id", "discord_id", "receipt", "created_at")})
    return ready


def ack_dms(ids: list, note: str | None = None) -> int:
    clean = [int(i) for i in (ids or []) if str(i).strip().isdigit()]
    if not clean:
        return 0
    conn = get_db()
    try:
        total = 0
        for i in range(0, len(clean), _IN_CHUNK):
            chunk = clean[i:i + _IN_CHUNK]
            qmarks = ",".join("?" * len(chunk))
            cur = conn.execute(
                f"UPDATE portal_landsraad_claim SET dm_sent=1, dm_note=? "
                f"WHERE id IN ({qmarks}) AND dm_sent=0", [note, *chunk])
            total += cur.rowcount
        conn.commit()
        return total
    finally:
        conn.close()
