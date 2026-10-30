"""Planned outage switch for the games on the home Proxmox relay.

/etc/lastsietch/games-offline lists games whose host is dark (the 2026 relocation).
While a game is listed the panel answers for it locally and never calls the
games relay, which would only time out:

    # one game id per line, '#' starts a comment
    conan
    enshrouded
    note: Offline while we relocate

A missing or unreadable file means nothing is offline. Dune is never offline
through this file: it does not run on that host.

The file is re-read on every call, so adding or removing it needs no restart.
"""
import logging
import os
import re

from fastapi import HTTPException

logger = logging.getLogger(__name__)

DEFAULT_NOTE = "Offline while we relocate"

# The games relay roster, mirrored from relay/games.json so the panel can
# answer for it while the relay host is dark.
GAMES_RELAY = {"conan": "Conan Exiles", "enshrouded": "Enshrouded"}
NEVER_OFFLINE = frozenset({"dune"})

_ID_RE = re.compile(r"^[a-z0-9_-]+$")


def flag_path():
    return os.environ.get("LASTSIETCH_GAMES_OFFLINE_FILE", "/etc/lastsietch/games-offline")


def parse(text):
    games = set()
    note = DEFAULT_NOTE
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("note:"):
            note = line[len("note:"):].strip() or DEFAULT_NOTE
            continue
        gid = line.split("#", 1)[0].strip().lower()
        if gid in NEVER_OFFLINE or not _ID_RE.match(gid):
            continue
        games.add(gid)
    return frozenset(games), note


def read():
    try:
        with open(flag_path(), encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return frozenset(), DEFAULT_NOTE
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("games_offline: flag read failed, treating as absent: %s", exc)
        return frozenset(), DEFAULT_NOTE
    return parse(text)


def display_name(game_id):
    return GAMES_RELAY.get(game_id, game_id)


def refusal(game_id):
    return HTTPException(status_code=503, detail=f"{display_name(game_id)} is offline while we relocate")


def refuse_if_offline(game_id):
    offline, _ = read()
    if game_id in offline:
        raise refusal(game_id)


def whole_host_offline(offline):
    return set(GAMES_RELAY) <= offline


def status_entry(note):
    return {
        "provisioned": True,
        "relocating": True,
        "note": note,
        "vm_running": None,
        "server_running": None,
        "uptime_seconds": None,
        "players_online_estimate": None,
        "last_snapshot_ts": None,
    }


def game_info(game_id, note):
    return {
        "game_id": game_id,
        "display_name": display_name(game_id),
        "vmid": None,
        "groups": [],
        "setting_count": 0,
        "supports_rcon": False,
        "has_save_dir": False,
        "provisioned": True,
        "has_update": False,
        "relocating": True,
        "note": note,
    }


# What the relay itself would answer for the rest of its roster. Only used
# when every games-relay game is listed and the relay is not called at all.
def registry():
    games = {
        gid: {"display_name": name, "supports_rcon": False,
              "config_format": "ini", "config_restart_mode": "in_place"}
        for gid, name in GAMES_RELAY.items()
    }
    games["dune"] = {"display_name": "Dune Awakening", "supports_rcon": False,
                     "config_format": "ini", "config_restart_mode": "in_place"}
    return games


def batch(note):
    result = {gid: status_entry(note) for gid in GAMES_RELAY}
    result["dune"] = {
        "provisioned": False,
        "vm_running": None,
        "server_running": None,
        "uptime_seconds": None,
        "players_online_estimate": None,
        "last_snapshot_ts": None,
    }
    return result


def host_status(note):
    return {"offline": True, "relocating": True, "note": note}


def answer(game_id, rest, method, note):
    """Local answer for a listed game's /games/<id><rest> call: a stub for the
    read paths the dashboard polls, a 503 for everything else."""
    if method == "GET":
        if rest == "":
            return game_info(game_id, note)
        if rest == "/server/status":
            return {"running": False, "relocating": True, "note": note}
        if rest == "/vm/status":
            return {"status": "offline", "relocating": True, "note": note}
    raise refusal(game_id)
