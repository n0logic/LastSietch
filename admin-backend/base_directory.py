"""Base directory (admin plan A0): one row per totem, joined across the three
places a base can live.

  game DB     relay /dune/bases (claims, packed totems, held leftovers) and
              /dune/bb/available-sources (every base_backups header)
  Base Vault  the catalog on this host (admin-helper `catalog`)

Read-only end to end. Every source is optional: a field whose source is down or
not yet deployed comes back None, and the page says "unknown" rather than
guessing. The status precedence is packed > held > orphaned > placed, then
"archived only" for a totem the vault remembers and the game DB does not.

PII: rows name owners against positions. `build` is admin-only. The portal gets
`for_player`, which keeps only the caller's own bases and drops everything that
names or locates anyone else.
"""
import datetime

import base_vault
from cache import ttl_cache
from relay import call_relay

# The engine's cap on parked BRT backups per player (verified across every holder).
SLOT_CAP = 3

STATUS_LABELS = {
    "placed": "Placed",
    "packed": "Packed",
    "held": "Held",
    "orphaned": "Orphaned",
    "archived": "Archived only",
}

# Shared with routers/v2_bases.py `/bases` so both read one cached directory:
# it is the heaviest read in the dune-* family (see the TTL note there).
@ttl_cache("dune.bases", ttl=300, max_stale=1800)
async def cached_bases() -> dict:
    # 70s: the relay itself allows the game-host command 60s.
    return await call_relay("/dune/bases", timeout=70)


@ttl_cache("dune.bb.sources", ttl=300, max_stale=1800)
async def cached_backups() -> dict:
    return await call_relay("/dune/bb/available-sources", timeout=30)


@ttl_cache("vault.catalog", ttl=60, max_stale=600)
async def cached_catalog() -> dict:
    return await base_vault.call({"action": "catalog"})


async def _safe(fetch):
    try:
        data = await fetch()
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("available") is False:
        return None
    return data


async def load(now=None) -> dict:
    return build(await _safe(cached_bases), await _safe(cached_backups),
                 await _safe(cached_catalog), now=now)


def _age_days(stamp, now):
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0, int((now - when).total_seconds() // 86400))


def _vault(entry, now):
    if entry is None:
        return {"snapshots": 0, "last_captured_at": None, "age_days": None, "verified": None,
                "last_kind": None, "last_live_captured_at": None}
    status = entry.get("last_verification")
    return {
        "snapshots": entry.get("snapshots") or 0,
        "last_captured_at": entry.get("last_captured_at"),
        "age_days": _age_days(entry.get("last_captured_at"), now),
        "verified": True if status == "verified" else False if status == "failed" else None,
        "last_kind": entry.get("last_kind"),
        "last_live_captured_at": entry.get("last_live_captured_at"),
    }


def _int(value):
    return value if type(value) is int else None


def _status(base):
    if base.get("ownership") == "stored_backup" or base.get("actor_state") == "BaseBackup":
        return "packed"
    if base.get("partition_id") is None:
        return "held"
    if base.get("ownership") == "orphaned":
        return "orphaned"
    return "placed"


def _marker(base, status):
    """Where a row goes on the map. A packed base is drawn hollow at its stored claim
    origin (where the BRT would rebuild it), falling back to the parked position on a
    relay that does not ship the origin yet. Its dimension_index is not meaningful."""
    if status == "packed":
        if _int(base.get("origin_x")) is not None and _int(base.get("origin_y")) is not None:
            return {"x": base["origin_x"], "y": base["origin_y"], "kind": "origin"}
        if _int(base.get("x")) is not None and _int(base.get("y")) is not None:
            return {"x": base["x"], "y": base["y"], "kind": "parked"}
        return None
    if _int(base.get("x")) is None or _int(base.get("y")) is None:
        return None
    return {"x": base["x"], "y": base["y"], "kind": "claim"}


def build(bases, backups, catalog, now=None) -> dict:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    sources = (backups or {}).get("sources") or []
    vault_rows = {e["totem_id"]: e for e in (catalog or {}).get("totems") or []
                  if isinstance(e, dict) and _int(e.get("totem_id"))}

    by_backup = {}
    slots_by_player = {}
    slots_by_account = {}
    for s in sources:
        if not isinstance(s, dict) or _int(s.get("id")) is None:
            continue
        by_backup[s["id"]] = s
        if _int(s.get("player_id")) is not None:
            slots_by_player[s["player_id"]] = slots_by_player.get(s["player_id"], 0) + 1
        if _int(s.get("owner_account_id")) is not None:
            slots_by_account[s["owner_account_id"]] = slots_by_account.get(s["owner_account_id"], 0) + 1
    backup_by_totem = {s["totem_actor_id"]: s for s in by_backup.values() if _int(s.get("totem_actor_id"))}

    def backup_block(source):
        if source is None:
            return None
        player = _int(source.get("player_id"))
        return {"id": source["id"], "name": source.get("name"),
                "linked_actors": source.get("linked_actor_count"),
                "slots_used": slots_by_player.get(player) if player is not None else None,
                "slots_cap": SLOT_CAP}

    rows = []
    seen_totems = set()
    seen_backups = set()
    for base in (bases or {}).get("bases") or []:
        if not isinstance(base, dict) or _int(base.get("totem_id")) is None:
            continue
        totem = base["totem_id"]
        seen_totems.add(totem)
        status = _status(base)
        owner = base.get("owner") if isinstance(base.get("owner"), dict) else None
        source = None
        if status == "packed":
            source = by_backup.get(_int(base.get("backup_id"))) or backup_by_totem.get(totem)
            if source is not None:
                seen_backups.add(source["id"])
                owner = {"name": source.get("owner_name"), "account_id": source.get("owner_account_id"),
                         "days_away": None, "online": None}
        account = _int((owner or {}).get("account_id"))
        slots = base.get("owner_backup_slots")
        if status != "packed" and _int(slots) is None and account is not None:
            slots = slots_by_account.get(account, 0) if backups is not None else None
        rows.append({
            "key": "totem:%d" % totem,
            "status": status,
            "totem_id": totem,
            "backup_id": source["id"] if source else _int(base.get("backup_id")),
            "label": base.get("label") or (source or {}).get("name") or "",
            "map": base.get("map"),
            "dimension_index": None if status == "packed" else base.get("dimension_index"),
            "partition_id": base.get("partition_id"),
            "resident": False if status == "held" else base.get("partition_alive"),
            "marker": _marker(base, status),
            "owner": owner,
            "owner_activity": base.get("owner_activity"),
            "holders": len([h for h in base.get("holders") or [] if isinstance(h, dict) and h.get("rank") != 1]),
            "pieces": base.get("pieces"),
            "placeables": base.get("placeables"),
            "health_pct": base.get("health_pct"),
            "backup": backup_block(source),
            "owner_backup_slots": _int(slots) if status != "packed" else None,
            "owner_backup_tools": _int(base.get("owner_backup_tools")),
            "vault": _vault(vault_rows.get(totem), now),
        })

    # A backup header whose totem the directory did not return (no totem actor, or
    # the bases read is down): still a packed base, listed without a map position.
    for backup_id, source in sorted(by_backup.items()):
        if backup_id in seen_backups:
            continue
        totem = _int(source.get("totem_actor_id"))
        if totem is not None:
            seen_totems.add(totem)
        rows.append({
            "key": "backup:%d" % backup_id, "status": "packed", "totem_id": totem, "backup_id": backup_id,
            "label": source.get("name") or "", "map": None, "dimension_index": None, "partition_id": None,
            "resident": None, "marker": None,
            "owner": {"name": source.get("owner_name"), "account_id": source.get("owner_account_id"),
                      "days_away": None, "online": None},
            "owner_activity": None, "holders": 0, "pieces": None, "placeables": None, "health_pct": None,
            "backup": backup_block(source), "owner_backup_slots": None, "owner_backup_tools": None,
            "vault": _vault(vault_rows.get(totem), now),
        })

    # Only safe to call a totem "archived only" when the game-DB directory answered.
    if bases is not None:
        for totem, entry in sorted(vault_rows.items()):
            if totem in seen_totems:
                continue
            rows.append({
                "key": "totem:%d" % totem, "status": "archived", "totem_id": totem, "backup_id": None,
                "label": "", "map": entry.get("live_source_map"), "dimension_index": entry.get("live_source_dimension"),
                "partition_id": None, "resident": None, "marker": None, "owner": None, "owner_activity": None,
                "holders": 0, "pieces": None, "placeables": None, "health_pct": None, "backup": None,
                "owner_backup_slots": None, "owner_backup_tools": None, "vault": _vault(entry, now),
            })

    for row in rows:
        row["status_label"] = STATUS_LABELS[row["status"]]
    counts = {key: 0 for key in STATUS_LABELS}
    for row in rows:
        counts[row["status"]] += 1
    return {
        "available": bases is not None or backups is not None,
        "sources": {"bases": bases is not None, "backups": backups is not None, "vault": catalog is not None},
        "stale": bool((bases or {}).get("stale") or (backups or {}).get("stale")),
        "vault_truncated": bool((catalog or {}).get("truncated")),
        "slot_cap": SLOT_CAP,
        "counts": counts,
        "legend": (bases or {}).get("legend"),
        "rows": rows,
    }


def for_player(directory, account_id) -> dict:
    """The player-scoped read for the portal (plan D1): the caller's own placed, held and
    packed bases with their last vault capture, plus their packed-backup slots. Nothing
    that names or locates anyone else: no co-holders, no positions, no other rows."""
    if type(account_id) is not int or account_id <= 0:
        return {"bases": [], "packed_backups": None}
    mine = []
    used = None
    for row in (directory or {}).get("rows") or []:
        owner = row.get("owner") or {}
        if row.get("status") == "archived" or owner.get("account_id") != account_id:
            continue
        if row["status"] == "packed" and row.get("backup"):
            used = row["backup"].get("slots_used")
        elif row.get("owner_backup_slots") is not None:
            used = row["owner_backup_slots"]
        vault = row.get("vault") or {}
        mine.append({"totem_id": row.get("totem_id"), "backup_id": row.get("backup_id"),
                     "label": row.get("label") or "", "status": row["status"],
                     "map": row.get("map"), "dimension_index": row.get("dimension_index"),
                     "last_vault_capture_at": vault.get("last_captured_at")})
    return {"bases": mine,
            "packed_backups": {"used": used, "cap": SLOT_CAP} if used is not None else None}


async def player_bases(account_id) -> dict:
    return for_player(await load(), account_id)
