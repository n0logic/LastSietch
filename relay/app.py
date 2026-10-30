import asyncio
import base64
import json
import os
import re
import subprocess
import time
from datetime import datetime

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Last Sietch Relay API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get("RELAY_ALLOWED_ORIGINS", "").split(",") if o],
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key"],
)

API_KEY = os.environ.get("LASTSIETCH_RELAY_API_KEY", "")


def verify_key(x_api_key: str = Header()):
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _karum_src(body):
    """Validate the optional take source. Absent/None = bank, which is what every leg did
    before 2026-08-25. Returned as a dict to splice into `fields`, because `fields` is a
    HARD ALLOWLIST: a key not present there is dropped silently before the writer sees it,
    and a dropped `src` would take from the BANK while the UI said backpack."""
    src = body.get("src")
    if src is None:
        return {}
    if src not in ("bank", "backpack"):
        raise HTTPException(400, "src must be bank or backpack")
    return {"src": src}


# --- Health ---

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# --- Dune self-host status (read-only; SSH proxy to <game-host>) ---
#
# The Dune game runs on a separate k3s host, not on a hypervisor VM.
# These endpoints SSH into <game-host> to read the BGD HTTP API, the game DB
# (via /root/dq.sh), and the lastsietch-telemetry SQLite store. All read-only — DB
# credentials stay on the game host; nothing mutating runs here.

# <game-host> SSH target. Fully explicit (host/user/key) so it works regardless
# of $HOME — the systemd unit runs with HOME=/opt/lastsietch-relay, not /root, so
# ~/.ssh/config is not consulted.
#
# The lastsietch-relay key on the game host is pinned to a forced command
# (/root/dune-relay-dispatch.sh) via authorized_keys, so the remote command we
# send is an action token, not a shell command — the dispatcher maps the token
# to one of a fixed set of read-only operations and rejects anything else.
DUNE_SSH_HOST = os.environ.get("DUNE_SSH_HOST", "")   # game host; required, no default
DUNE_SSH_USER = "root"
DUNE_SSH_KEY = "/opt/lastsietch-relay/.ssh/id_ed25519"
DUNE_SSH_KNOWN_HOSTS = "/opt/lastsietch-relay/.ssh/known_hosts"
DUNE_WINDOW_RE = re.compile(r'^[a-zA-Z0-9]+$')

# VC0 perf: SSH ControlMaster multiplexing. First call sets up the socket at
# /run/lastsietch-relay/ssh-<hash>; subsequent calls (within ControlPersist) reuse
# it for ~5ms vs ~200-500ms full handshake. The socket dir is provisioned
# by /etc/tmpfiles.d/lastsietch-relay.conf so it survives reboots.
SSH_CONTROLMASTER_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=/run/lastsietch-relay/ssh-%C",
    "-o", "ControlPersist=10m",
]


def _dune_ssh(remote_cmd: str, timeout: int = 45):
    """Run a command on <game-host> over SSH. Returns (stdout, stderr, returncode)."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=yes",
           "-o", f"UserKnownHostsFile={DUNE_SSH_KNOWN_HOSTS}",
           *SSH_CONTROLMASTER_OPTS,
           "-i", DUNE_SSH_KEY, "-l", DUNE_SSH_USER,
           DUNE_SSH_HOST, remote_cmd]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "ssh command to <game-host> timed out", -1


def _dune_ssh_stdin(remote_cmd: str, stdin_data: str, timeout: int = 45):
    """Same as _dune_ssh but pipes stdin_data into ssh's stdin instead of
    embedding it in the remote_cmd argv. Required for grant payloads above
    ~150KB that hit ARG_MAX when passed as a positional argv. The
    dispatcher on <game-host> must use an action that reads stdin
    (e.g. 'grant-stdin' for /root/dune-grant.sh --grant-b64-stdin)."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=yes",
           "-o", f"UserKnownHostsFile={DUNE_SSH_KNOWN_HOSTS}",
           *SSH_CONTROLMASTER_OPTS,
           "-i", DUNE_SSH_KEY, "-l", DUNE_SSH_USER,
           DUNE_SSH_HOST, remote_cmd]
    try:
        result = subprocess.run(cmd, input=stdin_data, capture_output=True,
                                text=True, timeout=timeout)
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "ssh command to <game-host> timed out", -1


def _dune_ssh_json(remote_cmd: str, timeout: int = 45):
    """Run a <game-host> command expected to emit JSON on stdout. Raises 502 on failure."""
    out, err, code = _dune_ssh(remote_cmd, timeout=timeout)
    if code != 0:
        detail = (err or out or "unknown error").strip()
        raise HTTPException(502, f"<game-host> command failed: {detail[:300]}")
    out = (out or "").strip()
    if not out:
        raise HTTPException(502, "<game-host> command produced no output")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise HTTPException(502, f"<game-host> returned non-JSON: {out[:300]}")


@app.get("/dune/battlegroup", dependencies=[Depends(verify_key)])
def dune_battlegroup():
    """Full BGD battlegroup state. Read-only passthrough of BGD /v0/battlegroup."""
    return _dune_ssh_json("battlegroup")


@app.get("/dune/players/online", dependencies=[Depends(verify_key)])
def dune_players_online():
    """Online FLS hex IDs from BGD. Read-only passthrough of BGD /v0/players/online."""
    return _dune_ssh_json("players-online")


@app.get("/dune/status", dependencies=[Depends(verify_key)])
def dune_status():
    """Per-map up/down + counts: farm_state + world_partition + BG JSON. Read-only."""
    return _dune_ssh_json("status", timeout=60)


@app.get("/dune/bases", dependencies=[Depends(verify_key)])
def dune_bases():
    """Land-claim ownership directory for the ADMIN portal. Every claim with its
    owner, co-holders, world position, piece count and condition. Read-only.

    Ownership is not on the actor: a totem carries no owner_account_id and the
    buildings around it are owned by the totem's OWN entity. The real link is
    permission_actor_rank (rank 1 = owner). Classifies each claim as owned /
    orphaned (owner record cascade-deleted with the character) / stored_backup
    (sitting inside the reconstruction tool, not a claim on the ground).

    PII: this says who owns what. Auth-gated here, and it must never be folded
    into the shared /portal/maps/{map}/data payload that the public site is
    designed to consume."""
    return _dune_ssh_json("bases", timeout=60)


@app.get("/dune/bases/near", dependencies=[Depends(verify_key)])
def dune_bases_near(map_name: str, dim: int, x: float, y: float, limit: int = 5):
    """Nearest claims to a world point — the admin map's click/tooltip lookup.
    Stored backups are excluded: they keep a world transform but are not on the
    ground. Read-only. Same PII rule as /dune/bases.

    `dim` is required and has no default. Hagga runs several sietches over one
    coordinate space, one dimension each (the map instance config), so a lookup
    that does not pin the dimension ranks the other world's bases against the
    clicked point and names the wrong owner."""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", map_name or ""):
        raise HTTPException(400, "map_name must be alphanumeric")
    if not 0 <= dim <= 99:
        raise HTTPException(400, "dim out of range")
    limit = max(1, min(limit, 25))
    # Fixed-point rather than repr(): a float that stringifies to exponent
    # notation would fail the dispatcher's arg pattern.
    return _dune_ssh_json(
        f"bases-near {map_name}:{dim}:{x:.1f}:{y:.1f}:{limit}", timeout=30)


def _claim_op(remote_cmd: str, timeout: int):
    """Run a claim write and pass the tool's own JSON back, refusal or not.

    NOT _dune_ssh_json: these tools exit non-zero for a REFUSAL, which is a
    normal outcome carrying the structured `blockers` an admin needs to read.
    _dune_ssh_json turns any non-zero exit into a 502 with the JSON flattened
    into a detail string, so "this base is orphaned, adopt it instead" reached
    the panel as a server error. A refusal is an answer; only an unparseable
    result is a fault."""
    out, err, code = _dune_ssh(remote_cmd, timeout=timeout)
    text = (out or "").strip()
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    detail = (err or text or "no output").strip()
    raise HTTPException(502, f"claim op failed: {detail[:300]}")


@app.post("/dune/claims/{totem_id}/backup", dependencies=[Depends(verify_key)])
def dune_claim_backup(totem_id: int):
    """WRITE. Back a base into its OWNER's reconstruction tool: the plot frees and
    the owner keeps everything. Every eligibility rule (orphan, owner online,
    owner already holds a backup) is enforced on the game host, not here.

    ⚠️ This persists the pickup; it does NOT despawn the structures, and no
    database path can. They stand until the map reloads, which for persistent
    Hagga means a restart. When the plot must be VISIBLY clear, adopt instead and
    remove it with the in-game tool, which is the only thing that despawns live."""
    if not 0 < totem_id < 10**19:
        raise HTTPException(400, "totem_id out of range")
    return _claim_op(f"claim-backup {totem_id}", timeout=180)


@app.post("/dune/claims/{totem_id}/adopt", dependencies=[Depends(verify_key)])
def dune_claim_adopt(totem_id: int, account_id: int):
    """WRITE. Adopt a claim onto an admin, keeping the previous owner as a rank-2
    co-holder so they are not locked out of their own base. Reversible: --revert
    restores the original owner from the audit row. The 3-claim cap is enforced
    on the game host."""
    if not 0 < totem_id < 10**19:
        raise HTTPException(400, "totem_id out of range")
    if not 0 < account_id < 10**19:
        raise HTTPException(400, "account_id out of range")
    return _claim_op(f"claim-adopt {totem_id}:{account_id}", timeout=120)


@app.get("/dune/claims/{totem_id}/options", dependencies=[Depends(verify_key)])
def dune_claim_options(totem_id: int):
    """READ-ONLY preflight for the admin Claims map: what can be done about this
    base, and what stands in the way. Merges the adopt checker and the
    back-up-to-owner checker so the panel makes one call and can never render
    half an answer. Neither underlying script writes without its own env flag,
    and the dispatcher sets neither."""
    if not 0 < totem_id < 10**19:
        raise HTTPException(400, "totem_id out of range")
    return _dune_ssh_json(f"claim-options {totem_id}", timeout=60)


@app.get("/dune/guilds", dependencies=[Depends(verify_key)])
def dune_guilds():
    """Public guild directory: guilds, members (resolved to character names),
    and Landsraad contributions (overall + current term). Read-only. Global
    (not per-account); the admin-backend caches it for the portal."""
    return _dune_ssh_json("guilds", timeout=45)


@app.get("/dune/guild-invites", dependencies=[Depends(verify_key)])
def dune_guild_invites(account_id: int):
    """A player's OWN pending guild invites (dune.get_player_guild_invites). The
    account_id is resolved to controller_id server-side (tombstone-safe) by
    dune-guilds.py. Read-only. The admin-backend passes the session-bound
    account_id — a client never picks whose invites to read."""
    if account_id <= 0:
        raise HTTPException(400, "account_id must be a positive integer")
    return _dune_ssh_json(f"guild-invites {account_id}", timeout=45)


@app.get("/dune/guild-census", dependencies=[Depends(verify_key)])
def dune_guild_census(guild_id: int):
    """Online-state census of one guild's roster
    (dune.get_all_player_in_guild_online_state). Read-only. Caller-must-be-a-member
    is enforced by the admin-backend before this is called."""
    if guild_id <= 0:
        raise HTTPException(400, "guild_id must be a positive integer")
    return _dune_ssh_json(f"guild-census {guild_id}", timeout=45)


# Guild op payload: op verbs + a UUID idempotency key check, mirroring the
# grant/market write validation. Every field is re-validated on <game-host> by
# dune-guild-op.sh; this is the first layer.
_GUILD_OP_VERBS = {"edit_description", "accept_invite", "reject_invite", "send_invite",
                   "promote", "demote", "remove"}
# Member-management ops: controller-scoped target + guild_id required. Dark by
# default (GUILD_WRITES_DARK on the writer); remove is extra-gated on the writer.
_GUILD_MEMBER_OPS = {"promote", "demote", "remove"}
_GUILD_OP_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@app.post("/dune/guild-op", dependencies=[Depends(verify_key)])
async def dune_guild_op(request: Request):
    """Guild-operations WRITE path. Body =
    {op, guild_id?, actor_account_id, target_account_id?, detail?, idempotency_key,
     mode?, requested_by_discord_id?}. actor_account_id is resolved SERVER-SIDE by
     the admin-backend from the authenticated session — the relay never derives it
     and a client can never act as another player. The compact JSON is
     base64-encoded and handed to the dispatcher's 'guild-op' action over stdin;
     dune-guild-op.sh re-validates every field, takes the guild lock, writes the
     dune.ls_guild_ops audit row (idempotency), and calls the proc. Invite ops
     are dark by default. The writer's JSON is surfaced verbatim."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    op = body.get("op")
    if op not in _GUILD_OP_VERBS:
        raise HTTPException(400, f"op must be one of {sorted(_GUILD_OP_VERBS)}")

    idem = body.get("idempotency_key")
    if not isinstance(idem, str) or not _GUILD_OP_UUID_RE.match(idem):
        raise HTTPException(400, "idempotency_key must be a UUID")

    def _pos_int(name, required=True):
        val = body.get(name)
        if val is None:
            if required:
                raise HTTPException(400, f"{name} is required")
            return None
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    actor_account_id = _pos_int("actor_account_id")
    guild_id = _pos_int(
        "guild_id",
        required=op in ("edit_description", "send_invite") or op in _GUILD_MEMBER_OPS)
    target_account_id = _pos_int("target_account_id", required=op == "send_invite")
    # Controller-scoped target for member ops (dune.guild_members.player_id). This
    # is distinct from target_account_id (account-scoped, send_invite only).
    target_player_controller_id = _pos_int(
        "target_player_controller_id", required=op in _GUILD_MEMBER_OPS)

    mode = body.get("mode", "apply")
    if mode not in ("apply", "dry-run"):
        raise HTTPException(400, "mode must be 'apply' or 'dry-run'")

    detail = body.get("detail") or {}
    if not isinstance(detail, dict):
        raise HTTPException(400, "detail must be an object")

    req_by = body.get("requested_by_discord_id")
    if req_by is not None and not isinstance(req_by, str):
        raise HTTPException(400, "requested_by_discord_id must be a string")

    fields = {"op": op, "actor_account_id": actor_account_id,
              "idempotency_key": idem, "mode": mode, "detail": detail}
    if guild_id is not None:
        fields["guild_id"] = guild_id
    if target_account_id is not None:
        fields["target_account_id"] = target_account_id
    if target_player_controller_id is not None:
        fields["target_player_controller_id"] = target_player_controller_id
    if req_by:
        fields["requested_by_discord_id"] = req_by

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode guild-op payload")

    out, err, code = _dune_ssh_stdin("guild-op", arg, timeout=45)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "success": False,
        "status": "failed",
        "exit_code": code,
        "message": (err or out or "guild-op produced no output")[:1000],
        "stderr": err[:2000],
    }


@app.post("/dune/gift-op", dependencies=[Depends(verify_key)])
async def dune_gift_op(request: Request):
    """Solari GIFT WRITE path. Body =
    {sender_account_id, recipient_account_id, amount, idempotency_key, mode?,
     requested_by_discord_id?}. sender_account_id is resolved SERVER-SIDE by the
     admin-backend from the authenticated session — a client can never gift AS
     another player. The compact JSON is base64-encoded and handed to the
     dispatcher's 'gift-op' action over stdin; dune-gift-op.sh re-validates every
     field, writes the dune.ls_guild_gifts audit row (idempotency), pre-checks
     the sender balance, and runs the two value-conserving adjusts. Gifts are OFF
     by default (LASTSIETCH_GIFTS_ENABLED). The writer's JSON is surfaced verbatim."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    idem = body.get("idempotency_key")
    if not isinstance(idem, str) or not _GUILD_OP_UUID_RE.match(idem):
        raise HTTPException(400, "idempotency_key must be a UUID")

    def _pos_int(name):
        val = body.get(name)
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    sender_account_id = _pos_int("sender_account_id")
    recipient_account_id = _pos_int("recipient_account_id")
    amount = _pos_int("amount")
    if sender_account_id == recipient_account_id:
        raise HTTPException(400, "cannot gift to yourself")

    mode = body.get("mode", "apply")
    if mode not in ("apply", "dry-run"):
        raise HTTPException(400, "mode must be 'apply' or 'dry-run'")

    req_by = body.get("requested_by_discord_id")
    if req_by is not None and not isinstance(req_by, str):
        raise HTTPException(400, "requested_by_discord_id must be a string")

    fields = {"sender_account_id": sender_account_id,
              "recipient_account_id": recipient_account_id, "amount": amount,
              "idempotency_key": idem, "mode": mode}
    if req_by:
        fields["requested_by_discord_id"] = req_by

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode gift-op payload")

    out, err, code = _dune_ssh_stdin("gift-op", arg, timeout=45)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "success": False,
        "status": "failed",
        "exit_code": code,
        "message": (err or out or "gift-op produced no output")[:1000],
        "stderr": err[:2000],
    }


@app.post("/dune/item-transfer-op", dependencies=[Depends(verify_key)])
async def dune_item_transfer_op(request: Request):
    """CHOAM bank item TRANSFER WRITE path (Tier 5, cross-player single-row re-home).
    Body = {sender_account_id, recipient_account_id, item_id, idempotency_key,
     expected_template?, mode?, requested_by_discord_id?}. sender_account_id AND
     recipient_account_id are resolved SERVER-SIDE by the admin-backend (sender from the
     authenticated session, recipient from a char_name lookup) — a client can never
     transfer AS another player. The compact JSON is base64-encoded and handed to the
     dispatcher's 'item-transfer-op' action over stdin; dune-item-transfer-op.sh
     re-validates every field, resolves both banks tombstone-safe (pawn-keyed inv30),
     locks the item row pinned to the sender's bank, and does the single-row re-home
     UPDATE (atomic, dupe/loss-proof) writing dune.ls_item_transfers (idempotency +
     audit + RMT rate caps) in the same txn. DARK by default (LASTSIETCH_ITEM_TRANSFER_ENABLED=0):
     while off it refuses cleanly with status:"deferred". The writer's JSON is surfaced
     verbatim. Mirrors dune_gift_op."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    idem = body.get("idempotency_key")
    if not isinstance(idem, str) or not _GUILD_OP_UUID_RE.match(idem):
        raise HTTPException(400, "idempotency_key must be a UUID")

    def _pos_int(name):
        val = body.get(name)
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    sender_account_id = _pos_int("sender_account_id")
    recipient_account_id = _pos_int("recipient_account_id")
    item_id = _pos_int("item_id")
    if sender_account_id == recipient_account_id:
        raise HTTPException(400, "cannot transfer to yourself")

    mode = body.get("mode", "apply")
    if mode not in ("apply", "dry-run"):
        raise HTTPException(400, "mode must be 'apply' or 'dry-run'")

    # Optional swapped-item guard: the writer RAISEs if the locked row's template_id !=
    # this value. template_id-safe charset only. The writer reads it as `template_id`.
    expected_template = body.get("expected_template")
    if expected_template is not None:
        if not isinstance(expected_template, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{2,64}", expected_template):
            raise HTTPException(400, "expected_template must be 2-64 chars of letters, digits, _ or -")

    req_by = body.get("requested_by_discord_id")
    if req_by is not None and not isinstance(req_by, str):
        raise HTTPException(400, "requested_by_discord_id must be a string")

    fields = {"sender_account_id": sender_account_id,
              "recipient_account_id": recipient_account_id, "item_id": item_id,
              "idempotency_key": idem, "mode": mode}
    if expected_template is not None:
        fields["template_id"] = expected_template
    if req_by:
        fields["requested_by_discord_id"] = req_by

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode item-transfer-op payload")

    out, err, code = _dune_ssh_stdin("item-transfer-op", arg, timeout=45)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "success": False,
        "status": "failed",
        "exit_code": code,
        "message": (err or out or "item-transfer-op produced no output")[:1000],
        "stderr": err[:2000],
    }


# ---------------------------------------------------------------------------
# The Karum: player-to-player trade venue (SB-006 + SB-007). L3 of the four-layer
# contract. Four routes, ONE dispatcher token (`karum-op`); the action rides inside the
# base64 payload and dune-karum-op.sh re-validates it along with every other field.
#
# Every identity is resolved SERVER-SIDE by the admin-backend from the authenticated
# session before it reaches here, and re-resolved in the writer's transaction. A client can
# never trade AS another player. Wanted-order fills also carry the selected buyer and
# filler controllers that the backend resolved; the writer re-proves both against those
# accounts before using them.
#
# The writer's JSON is surfaced VERBATIM, including its `paid` and `delivered` booleans,
# because L4's branch table depends on them separately. Do not collapse them into a
# success/failure boolean here, and do not synthesise them when the writer did not answer:
# "no usable response" is a THIRD outcome that sends the listing to `reconciling`, and
# inventing paid:false on a timeout is how a second buyer ends up purchasing goods the
# first buyer already paid for.

_KARUM_ACTIONS = {
    "list":   "karum-list",
    "buy":    "karum-buy",
    "fill":   "karum-fill",
    "cancel": "karum-cancel",
    "admin":  "karum-admin",
}

_KARUM_ADMIN_ACTIONS = ("force-deliver", "force-return", "refund")


def _karum_dispatch(action: str, fields: dict):
    """Encode a compact sorted-key job and hand it to the karum-op forced command. Mirrors
    dune_gift_op / dune_item_transfer_op: the writer owns idempotency, we only transport."""
    fields = dict(fields)
    fields["action"] = action
    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, f"failed to encode {action} payload")

    out, err, code = _dune_ssh_stdin("karum-op", arg, timeout=60)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    # No usable response. Report it as exactly that, with NO paid/delivered claim, so L4
    # cannot mistake it for an affirmative refusal.
    return {
        "success": False,
        "status": "unknown",
        "exit_code": code,
        "message": (err or out or f"{action} produced no output")[:1000],
        "stderr": err[:2000],
    }


async def _karum_body(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    corr = body.get("correlation_id")
    if not isinstance(corr, str) or not _GUILD_OP_UUID_RE.match(corr):
        raise HTTPException(400, "correlation_id must be a UUID")
    return body, corr


def _karum_pos_int(body: dict, name: str):
    val = body.get(name)
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        raise HTTPException(400, f"{name} must be a positive integer")
    return val


def _karum_common(body: dict, corr: str, fields: dict):
    """operator + requested_by_discord_id are audit-only strings; mode is apply|dry-run."""
    fields["correlation_id"] = corr
    mode = body.get("mode", "apply")
    if mode not in ("apply", "dry-run"):
        raise HTTPException(400, "mode must be 'apply' or 'dry-run'")
    fields["mode"] = mode
    for key in ("operator", "requested_by_discord_id"):
        val = body.get(key)
        if val is not None:
            if not isinstance(val, str) or len(val) > 128:
                raise HTTPException(400, f"{key} must be a string of at most 128 chars")
            fields[key] = val
    return fields


@app.post("/dune/karum/list", dependencies=[Depends(verify_key)])
async def dune_karum_list(request: Request):
    """LIST: take the seller's item into escrow. THE ONLY GATED LEG in the whole feature.
    Body = {listing_id, seller_account_id, item_id, template_id, correlation_id,
     operator?, requested_by_discord_id?, mode?}.

    The writer's take is offline-gated on the SELLER, in-transaction, under a row lock,
    fail-closed on an undetermined status, because taking an item from a loaded session is
    resurrected under its original item id (live-tested 2026-07-26) and that is a
    duplication path. `player_online` here is an expected, routine refusal, not an error."""
    body, corr = await _karum_body(request)
    template_id = body.get("template_id")
    # Charset matches the shared take library's own guard (letters, digits, _ and -).
    if not isinstance(template_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{2,64}", template_id):
        raise HTTPException(400, "template_id must be 2-64 chars of letters, digits, _ or -")
    fields = {
        "listing_id": _karum_pos_int(body, "listing_id"),
        "seller_account_id": _karum_pos_int(body, "seller_account_id"),
        "item_id": _karum_pos_int(body, "item_id"),
        "template_id": template_id,
        **_karum_src(body),
    }
    return _karum_dispatch(_KARUM_ACTIONS["list"],
                           _karum_common(body, corr, fields))


@app.post("/dune/karum/buy", dependencies=[Depends(verify_key)])
async def dune_karum_buy(request: Request):
    """BUY: pay, then deliver, as two separate writer transactions in that order. Body =
    {listing_id, buyer_account_id, seller_account_id, amount, correlation_id, ...}.

    Payment goes first because it is the reversible leg; delivery is irreversible once the
    buyer can walk up and claim it. The reply always carries `paid` and `delivered` as
    SEPARATE booleans and L4 must branch on them, never on `status` alone:
      paid && delivered            -> sold
      !paid with a clean refusal   -> revert the listing to active, buyer not charged
      paid && !delivered           -> paid_undelivered, RETRY the same correlation_id
      status == 'unknown'          -> reconciling, NEVER revert to active

    A retry is free: the payment is gated by dune.ls_karum_payments and the delivery by
    dune.ls_item_delivery_log, both UNIQUE on correlation_id."""
    body, corr = await _karum_body(request)
    buyer = _karum_pos_int(body, "buyer_account_id")
    seller = _karum_pos_int(body, "seller_account_id")
    if buyer == seller:
        # Second line only. The FIRST line is L4 comparing account_id AND discord_id off
        # the listing row, because linked alts share a discord_id and the account check
        # alone is trivially defeated by someone who owns both sides of the trade.
        raise HTTPException(400, "cannot buy your own listing")
    fields = {
        "listing_id": _karum_pos_int(body, "listing_id"),
        "buyer_account_id": buyer,
        "seller_account_id": seller,
        "amount": _karum_pos_int(body, "amount"),
    }
    return _karum_dispatch(_KARUM_ACTIONS["buy"],
                           _karum_common(body, corr, fields))


@app.post("/dune/karum/fill", dependencies=[Depends(verify_key)])
async def dune_karum_fill(request: Request):
    """FILL: atomically take one exact filler stack and move the requester's total
    payment, then deliver through the existing claim lane. Body = {request_id,
    listing_id, buyer_account_id, buyer_ctrl, seller_account_id, seller_ctrl, item_id,
    template_id, stack_size, amount, take_correlation_id, correlation_id, ...}.

    The separate take correlation and payment correlation each land in their existing
    game-DB unique gate. The writer requires both gates to be new or both to be replays.
    """
    body, corr = await _karum_body(request)
    buyer = _karum_pos_int(body, "buyer_account_id")
    seller = _karum_pos_int(body, "seller_account_id")
    buyer_ctrl = _karum_pos_int(body, "buyer_ctrl")
    seller_ctrl = _karum_pos_int(body, "seller_ctrl")
    if buyer == seller or buyer_ctrl == seller_ctrl:
        raise HTTPException(400, "cannot fill your own wanted order")
    template_id = body.get("template_id")
    if not isinstance(template_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{2,64}", template_id):
        raise HTTPException(400, "template_id must be 2-64 chars of letters, digits, _ or -")
    take_corr = body.get("take_correlation_id")
    if not isinstance(take_corr, str) or not _GUILD_OP_UUID_RE.match(take_corr):
        raise HTTPException(400, "take_correlation_id must be a UUID")
    if take_corr == corr:
        raise HTTPException(400, "take_correlation_id must differ from correlation_id")
    # `fields` is a HARD ALLOWLIST: anything not named here is silently dropped before
    # the writer ever sees it. That has produced three live player-facing bugs already
    # (dry_run, period_key, and this shape of omission generally), so a new field on the
    # portal side is not wired until it is ALSO named here. Grade is optional: an order
    # posted before 2026-08-25 carries none and must keep meaning any-grade.
    fields = {
        "request_id": _karum_pos_int(body, "request_id"),
        "listing_id": _karum_pos_int(body, "listing_id"),
        "buyer_account_id": buyer,
        "buyer_ctrl": buyer_ctrl,
        "seller_account_id": seller,
        "seller_ctrl": seller_ctrl,
        "item_id": _karum_pos_int(body, "item_id"),
        "template_id": template_id,
        "stack_size": _karum_pos_int(body, "stack_size"),
        "amount": _karum_pos_int(body, "amount"),
        "take_correlation_id": take_corr,
        **_karum_src(body),
    }
    expected_quality = body.get("expected_quality")
    if expected_quality is not None:
        if (isinstance(expected_quality, bool)
                or not isinstance(expected_quality, int)
                or expected_quality < 0 or expected_quality > 5):
            raise HTTPException(400, "expected_quality must be an integer 0-5")
        quality_mode = body.get("quality_mode", "exact")
        if quality_mode not in ("exact", "min"):
            raise HTTPException(400, "quality_mode must be exact or min")
        fields["expected_quality"] = expected_quality
        fields["quality_mode"] = quality_mode
    return _karum_dispatch(_KARUM_ACTIONS["fill"],
                           _karum_common(body, corr, fields))


@app.post("/dune/karum/cancel", dependencies=[Depends(verify_key)])
async def dune_karum_cancel(request: Request):
    """CANCEL: return an unsold listing to the seller through the SAME claim lane a buyer
    collects from. Body = {listing_id, seller_account_id, price?, correlation_id, ...}.

    Give-only, and no money moves: payment only happens at buy time and a cancel is only
    reachable from `active`. Deliberately NOT a re-home back into the seller's bank, which
    would be a second take-shaped write for no benefit."""
    body, corr = await _karum_body(request)
    fields = {
        "listing_id": _karum_pos_int(body, "listing_id"),
        "seller_account_id": _karum_pos_int(body, "seller_account_id"),
    }
    price = body.get("price")
    if price is not None:
        if isinstance(price, bool) or not isinstance(price, int) or price < 0:
            raise HTTPException(400, "price must be a non-negative integer")
        fields["price"] = price
    return _karum_dispatch(_KARUM_ACTIONS["cancel"],
                           _karum_common(body, corr, fields))


@app.post("/dune/karum/admin", dependencies=[Depends(verify_key)])
async def dune_karum_admin(request: Request):
    """ADMIN: operator recovery for `paid_undelivered`, the one state that can need a human.
    Body = {admin_action, listing_id, correlation_id, ...} where admin_action is
    force-deliver | force-return | refund.

    It exists so nobody resolves a stuck trade with ad-hoc SQL, which is exactly what it is
    here to prevent. A refund is a NEW dune.ls_karum_payments row with its own
    correlation_id, never an UPDATE of the original, so a retried refund cannot
    double-credit."""
    body, corr = await _karum_body(request)
    admin_action = body.get("admin_action")
    if admin_action not in _KARUM_ADMIN_ACTIONS:
        raise HTTPException(400, "admin_action must be one of %s"
                                 % ", ".join(_KARUM_ADMIN_ACTIONS))
    fields = {
        "admin_action": admin_action,
        "listing_id": _karum_pos_int(body, "listing_id"),
    }
    if admin_action == "refund":
        original = body.get("original_correlation_id")
        if not isinstance(original, str) or not _GUILD_OP_UUID_RE.match(original):
            raise HTTPException(400, "original_correlation_id must be a UUID")
        if original == corr:
            raise HTTPException(400, "a refund must carry its OWN correlation_id, "
                                     "distinct from the payment it reverses")
        fields["original_correlation_id"] = original
        fields["buyer_account_id"] = _karum_pos_int(body, "buyer_account_id")
        fields["seller_account_id"] = _karum_pos_int(body, "seller_account_id")
        fields["amount"] = _karum_pos_int(body, "amount")
    else:
        fields["target_account_id"] = _karum_pos_int(body, "target_account_id")
        price = body.get("price")
        if price is not None:
            if isinstance(price, bool) or not isinstance(price, int) or price < 0:
                raise HTTPException(400, "price must be a non-negative integer")
            fields["price"] = price
    return _karum_dispatch(_KARUM_ACTIONS["admin"],
                           _karum_common(body, corr, fields))


@app.get("/dune/karum/audit", dependencies=[Depends(verify_key)])
async def dune_karum_audit():
    """The Karum escrow audit. READ-ONLY, so this is a GET and it is safe under a change
    freeze. Nightly from <web-host>, on demand from the admin panel.

    🔴 The exit code carries meaning that the body alone does not, and it MUST NOT be
    flattened: 0 clean, 1 at least one paging finding, 3 the audit could not run. Three is
    the one that matters, because "we could not look" is not "we looked and it was fine",
    and this audit exists precisely because the failure it watches for is silent. So a
    non-zero exit with unparseable output is reported as a PAGE, never as a pass."""
    out, err, code = _dune_ssh("karum-audit", timeout=200)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                # The script sets `page` itself; only ever raise it, never lower it.
                if code not in (0, 1):
                    parsed["page"] = True
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": False,
        "error": "audit_unavailable",
        "page": True,
        "exit_code": code,
        "message": (err or out or "the Karum audit produced no output")[:1000],
        "detail": "this is NOT a clean result; the audit could not be read",
    }


@app.post("/dune/reward-op", dependencies=[Depends(verify_key)])
async def dune_reward_op(request: Request):
    """Login-reward GRANT WRITE path. Body =
    {account_id, reward_kind, idempotency_key, mode?, requested_by_discord_id?,
     amount (daily_solari), template_id + quality_level (weekly_item)}.
     account_id is resolved SERVER-SIDE by the admin-backend from the authenticated
     session — a client can never claim AS another player. The compact JSON is
     base64-encoded and handed to the dispatcher's 'reward-op' action over stdin;
     dune-reward-op.sh re-validates every field, writes the dune.ls_reward_claims
     audit row (idempotency), then credits Solari (gift proc, +credit only) or mints
     the weekly item into the CHOAM bank (G29). Rewards are DARK by default
     (LASTSIETCH_REWARD_ENABLED=0): while off the writer refuses cleanly with status:"deferred"
     and NO txn. The writer's JSON is surfaced verbatim. Mirrors dune_gift_op."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    idem = body.get("idempotency_key")
    if not isinstance(idem, str) or not _GUILD_OP_UUID_RE.match(idem):
        raise HTTPException(400, "idempotency_key must be a UUID")

    def _pos_int(name):
        val = body.get(name)
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    account_id = _pos_int("account_id")

    reward_kind = body.get("reward_kind")
    if reward_kind not in ("daily_solari", "weekly_item", "monthly_augment",
                           "sn_shop_bank", "sn_shop_solari", "sn_shop_spec", "sn_shop_charxp"):
        raise HTTPException(
            400, "reward_kind must be 'daily_solari', 'weekly_item', 'monthly_augment', "
                 "'sn_shop_bank', 'sn_shop_solari', 'sn_shop_spec' or 'sn_shop_charxp'")

    fields = {"account_id": account_id, "reward_kind": reward_kind,
              "idempotency_key": idem}

    if reward_kind in ("sn_shop_bank", "sn_shop_solari", "sn_shop_spec", "sn_shop_charxp"):
        # Seven Nights shop. The SKU is the whole request: the writer looks up what
        # to mint (or how much Solari) in its own allowlist, so no template,
        # quantity, grade or amount is accepted here at all.
        sku = body.get("sku")
        if not isinstance(sku, str) or not re.fullmatch(r"sn-[a-z0-9-]{1,48}", sku):
            raise HTTPException(400, "sku must be sn- followed by 1-48 of a-z, 0-9 or -")
        fields["sku"] = sku
        fields["controller_id"] = _pos_int("controller_id")
        profile_id = body.get("profile_id")
        if not isinstance(profile_id, str) or not re.fullmatch(r"[0-9a-f]{32}", profile_id):
            raise HTTPException(400, "profile_id must be 32 lowercase hex chars")
        fields["profile_id"] = profile_id
        discord_id = body.get("discord_id")
        if discord_id is not None:
            if not isinstance(discord_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", discord_id):
                raise HTTPException(400, "discord_id must be a numeric string")
            fields["discord_id"] = discord_id
    elif reward_kind == "daily_solari":
        amount = _pos_int("amount")
        # Sanity ceiling on the Solari faucet (admin ramp tops out at 25k/day).
        if amount > 10_000_000:
            raise HTTPException(400, "amount is out of range")
        fields["amount"] = amount
    else:  # weekly_item / monthly_augment both deliver an item to the CHOAM bank
        template_id = body.get("template_id")
        # Charset matches dune-reward-op.sh validate_template_id (alnum + underscore).
        if not isinstance(template_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_]{1,64}", template_id):
            raise HTTPException(400, "template_id must be 1-64 chars of letters, digits or _")
        quality_level = body.get("quality_level")
        if isinstance(quality_level, bool) or not isinstance(quality_level, int) \
                or quality_level < 0 or quality_level > 5:
            raise HTTPException(400, "quality_level must be an integer 0..5")
        fields["template_id"] = template_id
        fields["quality_level"] = quality_level

    if reward_kind == "monthly_augment":
        # The monthly prize is PRE-AUGMENTED gear, so the item carries an augment
        # list that becomes its FAugmentedItemStats blob at INSERT. `rolls` is a
        # COUNT, not the values: roll count varies per augment (live: Lmg1 6,
        # Ch5_Scattergun1 5, Damage1 1) and a wrong count renders an empty augment
        # slot in game. The writer re-validates all of this; this is the edge gate.
        augments = body.get("augments")
        if not isinstance(augments, list) or not (1 <= len(augments) <= 3):
            raise HTTPException(400, "augments must be a 1-3 element array")
        clean = []
        for a in augments:
            if not isinstance(a, dict):
                raise HTTPException(400, "each augment must be an object")
            name = a.get("name")
            if not isinstance(name, str) or not re.fullmatch(
                    r"T\d+_Augment_[A-Za-z0-9_]{1,64}", name):
                raise HTTPException(400, "augment name must match T<digit>_Augment_*")
            grade = a.get("grade")
            if isinstance(grade, bool) or not isinstance(grade, int) or not (1 <= grade <= 5):
                raise HTTPException(400, "augment grade must be an integer 1..5")
            rolls = a.get("rolls")
            if isinstance(rolls, bool) or not isinstance(rolls, int) or not (1 <= rolls <= 10):
                raise HTTPException(400, "augment rolls must be an integer 1..10")
            clean.append({"name": name, "grade": grade, "rolls": rolls})
        fields["augments"] = clean
        # 🔴 REQUIRED, and its absence is silent. The writer refuses
        # monthly_augment outright without it ("monthly_augment requires
        # period_key"), and the reward period is a rolling 28-day window, so it
        # cannot be re-derived here from a calendar month. Omitted from this
        # allowlist between the feature landing and 2026-08-24, during which
        # EVERY monthly claim failed -- the same silent-drop shape as the
        # `dry_run` field above. A field missing from `fields` is dropped
        # without an error anywhere in the chain.
        period_key = body.get("period_key")
        if not isinstance(period_key, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", period_key):
            raise HTTPException(400, "period_key must be a YYYY-MM-DD date string")
        fields["period_key"] = period_key

    mode = body.get("mode", "apply")
    if mode not in ("apply", "dry-run"):
        raise HTTPException(400, "mode must be 'apply' or 'dry-run'")
    fields["mode"] = mode

    # Optional operator label (the writer records it on the ledger row for audit
    # attribution; defaults to 'lastsietch-reward' when absent). The backend passes
    # 'portal:<discord_id>'. Charset-limited so it is a safe -v psql binding.
    operator = body.get("operator")
    if operator is not None:
        if not isinstance(operator, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", operator):
            raise HTTPException(400, "operator must be 1-64 chars of letters, digits, :, _ or -")
        fields["operator"] = operator

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode reward-op payload")

    out, err, code = _dune_ssh_stdin("reward-op", arg, timeout=45)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "success": False,
        "status": "failed",
        "exit_code": code,
        "message": (err or out or "reward-op produced no output")[:1000],
        "stderr": err[:2000],
    }


@app.get("/dune/rewards/login-days", dependencies=[Depends(verify_key)])
def dune_rewards_login_days(account_id: int = None):
    """Daily-login history from the lastsietch-telemetry SQLite portal_login_days table
    (UTC dates the account was seen online, newest first, default 60-day window).
    Read-only (telemetry.db; the game DB is not touched). Mirrors the
    /dune/stats/presence read precedent. Dispatches the 'login-days' action (the
    telemetry read API decides the window). The backend derives streak + calendar
    claim-state from this. Without account_id it returns every account, the
    dispatcher's no-arg form, so the Seven Nights worker can pull once per tick."""
    if account_id is None:
        return _dune_ssh_json("login-days", timeout=30)
    if account_id <= 0:
        raise HTTPException(400, "account_id must be a positive integer")
    return _dune_ssh_json(f"login-days {account_id}", timeout=30)


@app.get("/dune/read-models", dependencies=[Depends(verify_key)])
def dune_read_models(account_id: int = None):
    """Per-account portal/admin read models from the telemetry mirror. Read-only
    (served from telemetry.db; the game DB is not touched). Pulled on a timer by
    the <web-host> local-mirror sync loop. Optional account_id narrows the pull."""
    cmd = f"read-models {account_id}" if account_id is not None else "read-models"
    return _dune_ssh_json(cmd, timeout=30)


@app.get("/dune/storage-models", dependencies=[Depends(verify_key)])
def dune_storage_models(account_id: int = None):
    """Per-account storage snapshots from the telemetry mirror (Phase 2). Read-only
    (served from telemetry.db; the game DB is not touched). Pulled on a timer by
    the <web-host> local-mirror sync loop. Optional account_id narrows the pull."""
    cmd = f"storage-models {account_id}" if account_id is not None else "storage-models"
    return _dune_ssh_json(cmd, timeout=45)


@app.get("/dune/market-listings-all", dependencies=[Depends(verify_key)])
def dune_market_listings_all():
    """Full active CHOAM exchange listing set from the telemetry mirror (Phase 2).
    Read-only (served from telemetry.db; the game DB is not touched). Pulled on a
    timer by the <web-host> local-mirror sync loop, which searches it locally."""
    return _dune_ssh_json("market-all", timeout=45)


@app.get("/dune/market/bot-prices", dependencies=[Depends(verify_key)])
def dune_market_bot_prices():
    """NPC market-maker buy-price export: per-item per-grade caps the bot still
    pays for player listings (written by lastsietch-market-bot each tick). Read-only
    (plain file read on the game box; the game DB is not touched). Pulled on a
    timer by the <web-host> local-mirror sync loop for the portal market page."""
    return _dune_ssh_json("market-bot-prices", timeout=30)


@app.get("/dune/market/bot-limits", dependencies=[Depends(verify_key)])
def dune_market_bot_limits():
    """Per-category weekly buy/sell budget usage + reset timestamps for the portal
    Exchange tracker (written by lastsietch-market-bot each tick). Read-only (plain file
    read on the game box; the game DB is not touched). Pulled on a timer by the
    <web-host> local-mirror sync loop."""
    return _dune_ssh_json("market-bot-limits", timeout=30)


@app.get("/dune/market/status", dependencies=[Depends(verify_key)])
def dune_market_status():
    """Market-bot service state + bot balance + ls_market_log activity. Read-only."""
    return _dune_ssh_json("market-status", timeout=45)


@app.get("/dune/market/listings", dependencies=[Depends(verify_key)])
def dune_market_listings(q: str):
    """Search active CHOAM exchange listings by template_id fragment. q must be
    a template_id-safe token (letters/digits/_/-); validated here before it
    reaches the dispatcher (which re-validates). Read-only, LIMIT 200."""
    q = (q or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", q):
        raise HTTPException(400, "q must be 2-64 chars of letters, digits, _ or -")
    return _dune_ssh_json(f"market-listings {q}", timeout=45)


@app.get("/dune/market/rare-recent", dependencies=[Depends(verify_key)])
def dune_market_rare_recent(after: str = None, limit: int = 50):
    """Recently-listed rare-rotation items for the Cielago market announcer
    (dune.ls_rare_rotation, RareRotation.recent_listings). Optional `after`
    cursor (ISO8601 timestamptz) returns only rows with listed_at newer than it;
    `limit` caps the page (default 50). The cold-fill bootstrap is auto-excluded
    by the producer's announce_after marker. Validated here before it reaches the
    dispatcher (which re-validates). Read-only. Shape: {"rows":[...]}."""
    after = (after or "").strip()
    if after and not re.fullmatch(r"[0-9TZ:+.\-]{1,40}", after):
        raise HTTPException(400, "after must be an ISO8601 UTC timestamp")
    if not (1 <= limit <= 500):
        raise HTTPException(400, "limit must be 1-500")
    cursor = after if after else "-"
    return _dune_ssh_json(f"market-rare-recent {cursor} {limit}", timeout=45)


@app.get("/dune/market/policy", dependencies=[Depends(verify_key)])
def dune_market_policy_get():
    """Current market-policy.json: price floors, weekly sell budgets, blocked
    sellers. Read-only."""
    return _dune_ssh_json("market-policy-get", timeout=30)


@app.post("/dune/market/policy", dependencies=[Depends(verify_key)])
async def dune_market_policy_set(request: Request):
    """Replace market-policy.json. Body = the full policy JSON object. base64 +
    stdin to the dispatcher's market-policy-set action, which validates, backs
    up, and atomically writes. Applies on the bot's next restart."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode policy payload")
    out, err, code = _dune_ssh_stdin("market-policy-set", arg, timeout=45)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    raise HTTPException(502, (err or out or "market-policy-set produced no output")[:300])


@app.post("/dune/market/service", dependencies=[Depends(verify_key)])
async def dune_market_service(request: Request):
    """start | stop | restart the lastsietch-market-bot service. Body = {"verb": ...}."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    verb = (body.get("verb") or "").strip() if isinstance(body, dict) else ""
    if verb not in ("start", "stop", "restart"):
        raise HTTPException(400, "verb must be start|stop|restart")
    # verb is allowlisted above, so the f-string carries no injection risk.
    out, err, code = _dune_ssh(f"market-service {verb}", timeout=45)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    if code != 0:
        raise HTTPException(502, (err or out or "market-service failed")[:300])
    return {"status": "ok", "verb": verb, "exit_code": code}


@app.post("/dune/market/buy", dependencies=[Depends(verify_key)])
async def dune_market_buy(request: Request):
    """Buy one CHOAM-exchange listing for a portal player. Delivery lands in the
    buyer's in-game Completed tab (exchange storage), funded from their CHOAM
    bank Solari. Online+offline safe (never touches live inventory).

    Body = {order_id:int, revision:int, count:int, buyer_ctrl:int, max_orders?:int}.
    buyer_ctrl is the player's controller_id and is resolved SERVER-SIDE by the
    admin-backend from the authenticated portal session — the relay never derives
    it and a client can never spend another player's bank. The compact JSON is
    base64-encoded and handed to the dispatcher's 'market-buy' action over stdin
    (same path as /dune/grant); the writer re-validates every field and runs the
    single funding+fulfill transaction. The writer's JSON (incl. ok:false errors)
    is surfaced verbatim."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    def _pos_int(name, required=True):
        val = body.get(name)
        if val is None:
            if required:
                raise HTTPException(400, f"{name} is required")
            return None
        if isinstance(val, bool) or not isinstance(val, int):
            raise HTTPException(400, f"{name} must be an integer")
        if val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    order_id = _pos_int("order_id")
    revision = _pos_int("revision")
    count = _pos_int("count")
    buyer_ctrl = _pos_int("buyer_ctrl")
    max_orders = _pos_int("max_orders", required=False)

    fields = {"order_id": order_id, "revision": revision,
              "count": count, "buyer_ctrl": buyer_ctrl}
    if max_orders is not None:
        fields["max_orders"] = max_orders

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode market-buy payload")

    out, err, code = _dune_ssh_stdin("market-buy", arg, timeout=30)
    out = (out or "").strip()
    err = (err or "").strip()

    # The writer owns the ok/error contract; surface its JSON verbatim (including
    # ok:false error tokens) so the admin-backend can map them to friendly text.
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass

    return {
        "ok": False,
        "error": "writer_no_output",
        "exit_code": code,
        "message": (err or out or "market-buy produced no output")[:1000],
        "stderr": err[:2000],
    }


@app.post("/dune/market/sell", dependencies=[Depends(verify_key)])
async def dune_market_sell(request: Request):
    """List one item a portal player owns on the CHOAM exchange. The engine moves
    the item to escrow and creates the order; the listing fee is funded from the
    seller's CHOAM bank Solari. OFFLINE-only (the source item lives in a persisted
    base container / bank; listing it writes the player's inventory state, which is
    RAM-backed while online — the writer hard-gates on online_status + grace).

    Body = {seller_ctrl:int, item_id:int, count:int, price:int, duration_days:int,
            max_orders?:int, expected_template?:str, expected_src?:bank/backpack}.
    duration_days must be one of {1,3,7,14}. expected_src names which of the seller's
    two pawn inventories the admin-backend resolved the item to, and the writer
    re-derives it from the locked row and refuses a mismatch. seller_ctrl
    is the player's controller_id and is resolved SERVER-SIDE by the admin-backend
    from the authenticated portal session — the relay never derives it and a client
    can never list another player's item. The admin-backend also re-verifies (via
    the container-browser ownership path) that item_id is in an inventory the seller
    owns before this call; the writer re-verifies a third time in-transaction. The
    compact JSON is base64-encoded and handed to the dispatcher's 'market-sell'
    action over stdin (same path as /dune/grant). The writer's JSON (incl. ok:false
    error tokens) is surfaced verbatim."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    def _pos_int(name, required=True):
        val = body.get(name)
        if val is None:
            if required:
                raise HTTPException(400, f"{name} is required")
            return None
        if isinstance(val, bool) or not isinstance(val, int):
            raise HTTPException(400, f"{name} must be an integer")
        if val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    seller_ctrl = _pos_int("seller_ctrl")
    item_id = _pos_int("item_id")
    count = _pos_int("count")
    price = _pos_int("price")
    duration_days = _pos_int("duration_days")
    max_orders = _pos_int("max_orders", required=False)

    if duration_days not in (1, 3, 7, 14):
        raise HTTPException(400, "duration_days must be one of 1, 3, 7, 14")

    # Optional swapped-item guard: the writer RAISEs item_not_found if the locked
    # items row's template_id != expected_template. template_id-safe charset only.
    expected_template = body.get("expected_template")
    if expected_template is not None:
        if not isinstance(expected_template, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{2,64}", expected_template):
            raise HTTPException(400, "expected_template must be 2-64 chars of letters, digits, _ or -")

    # Optional source pin. The admin-backend has already matched the client's
    # container_id against the selected character's strict bank/backpack pair, so this
    # only has to carry the answer. Closed set: an unknown value is a 400, never a
    # dropped key, because `fields` below is a HARD ALLOWLIST and a silently dropped
    # expected_src would let the writer list from whichever bag the item is in now.
    expected_src = body.get("expected_src")
    if expected_src is not None and expected_src not in ("bank", "backpack"):
        raise HTTPException(400, "expected_src must be bank or backpack")

    fields = {"seller_ctrl": seller_ctrl, "item_id": item_id,
              "count": count, "price": price, "duration_days": duration_days}
    if max_orders is not None:
        fields["max_orders"] = max_orders
    if expected_template is not None:
        fields["expected_template"] = expected_template
    if expected_src is not None:
        fields["expected_src"] = expected_src

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode market-sell payload")

    out, err, code = _dune_ssh_stdin("market-sell", arg, timeout=30)
    out = (out or "").strip()
    err = (err or "").strip()

    # The writer owns the ok/error contract; surface its JSON verbatim (including
    # ok:false error tokens) so the admin-backend can map them to friendly text.
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass

    return {
        "ok": False,
        "error": "writer_no_output",
        "exit_code": code,
        "message": (err or out or "market-sell produced no output")[:1000],
        "stderr": err[:2000],
    }


def _dune_market_order_action(action: str, request_body: dict):
    """Shared CANCEL/RELIST relay path. Validates the body, base64-encodes a compact
    JSON job, and hands it to the dispatcher's market-cancel/market-relist action over
    stdin (same path as /dune/market/sell). The orders writer owns the ok/error
    contract; its JSON is surfaced verbatim. owner_ctrl is resolved SERVER-SIDE by the
    admin-backend; the writer re-verifies ownership + revision in-transaction."""
    body = request_body
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    def _pos_int(name, required=True):
        val = body.get(name)
        if val is None:
            if required:
                raise HTTPException(400, f"{name} is required")
            return None
        if isinstance(val, bool) or not isinstance(val, int):
            raise HTTPException(400, f"{name} must be an integer")
        if val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    owner_ctrl = _pos_int("owner_ctrl")
    order_id = _pos_int("order_id")
    revision = _pos_int("revision")
    fields = {"action": action, "owner_ctrl": owner_ctrl,
              "order_id": order_id, "revision": revision}
    if action == "relist":
        price = _pos_int("price")
        duration_days = _pos_int("duration_days")
        if duration_days not in (1, 3, 7, 14):
            raise HTTPException(400, "duration_days must be one of 1, 3, 7, 14")
        fields["price"] = price
        fields["duration_days"] = duration_days

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, f"failed to encode market-{action} payload")

    token = "market-cancel" if action == "cancel" else "market-relist"
    out, err, code = _dune_ssh_stdin(token, arg, timeout=30)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": False,
        "error": "writer_no_output",
        "exit_code": code,
        "message": (err or out or f"market-{action} produced no output")[:1000],
        "stderr": err[:2000],
    }


@app.post("/dune/market/cancel", dependencies=[Depends(verify_key)])
async def dune_market_cancel(request: Request):
    """Cancel one of a portal player's ACTIVE CHOAM sell listings. The engine forfeits
    the listing fee (already paid) and moves the item to the player's Completed tab
    (exchange escrow) — it never touches live inventory, so this is ONLINE-SAFE.
    Body = {owner_ctrl:int, order_id:int, revision:int}. owner_ctrl is resolved
    SERVER-SIDE; the writer re-verifies the order belongs to owner_ctrl at the given
    revision and is still active."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    return _dune_market_order_action("cancel", body)


@app.post("/dune/market/relist", dependencies=[Depends(verify_key)])
async def dune_market_relist(request: Request):
    """Relist one of a portal player's CANCELED orders from the Completed tab at a new
    price/duration. The item is already in exchange escrow, so this is ONLINE-SAFE; the
    relist fee is funded from the player's CHOAM bank Solari. Body = {owner_ctrl:int,
    order_id:int, revision:int, price:int, duration_days:int (1|3|7|14)}. owner_ctrl is
    resolved SERVER-SIDE; the writer re-verifies ownership + revision + that the order
    is a canceled completed order in-transaction."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    return _dune_market_order_action("relist", body)


def _dune_storage_action(action: str, request_body: dict):
    """Shared WITHDRAW/DEPOSIT relay path for the portal Storage Manager. Validates the
    body, base64-encodes a compact JSON job, and hands it to the dispatcher's
    storage-withdraw/storage-deposit action over stdin (same path as /dune/market/sell).
    The storage writer owns the ok/error contract; its JSON is surfaced verbatim.
    owner_ctrl is resolved SERVER-SIDE by the admin-backend; the writer re-verifies the
    offline gate + ownership in-transaction. Mirrors _dune_market_order_action."""
    body = request_body
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    def _pos_int(name, required=True):
        val = body.get(name)
        if val is None:
            if required:
                raise HTTPException(400, f"{name} is required")
            return None
        if isinstance(val, bool) or not isinstance(val, int):
            raise HTTPException(400, f"{name} must be an integer")
        if val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    owner_ctrl = _pos_int("owner_ctrl")
    fields = {"action": action, "owner_ctrl": owner_ctrl}

    # `fields` is a WHITELIST -- anything not copied into it never reaches the writer.
    # That is why the selected-container passthrough below has to be explicit: without it
    # the portal's selection is silently dropped, every withdraw falls back to the
    # writer's default priority order, and the feature looks simply broken.
    if action == "withdraw":
        amount = _pos_int("amount")
        if amount > 100000:
            raise HTTPException(400, "amount must not exceed 100000")
        fields["amount"] = amount
        # Preferred destination = the container the player has open. Optional; the writer
        # intersects it against that player's own inventories, so it can only re-order
        # the choice, never reach somebody else's storage.
        dst_inv = _pos_int("dst_inventory_id", required=False)
        if dst_inv is not None:
            fields["dst_inventory_id"] = dst_inv
    else:  # deposit
        mode = body.get("mode")
        if mode not in ("sweep", "amount"):
            raise HTTPException(400, "mode must be one of sweep, amount")
        fields["mode"] = mode
        if mode == "amount":
            fields["amount"] = _pos_int("amount")
        # Source scope, same ownership guarantee. Omitted => the historic whole-owned-set
        # behaviour, so a sweep never starts emptying players' pockets by default.
        src_inv = _pos_int("src_inventory_id", required=False)
        if src_inv is not None:
            fields["src_inventory_id"] = src_inv

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, f"failed to encode storage-{action} payload")

    token = "storage-withdraw" if action == "withdraw" else "storage-deposit"
    out, err, code = _dune_ssh_stdin(token, arg, timeout=30)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": False,
        "error": "writer_no_output",
        "exit_code": code,
        "message": (err or out or f"storage-{action} produced no output")[:1000],
        "stderr": err[:2000],
    }


@app.post("/dune/storage/withdraw", dependencies=[Depends(verify_key)])
async def dune_storage_withdraw(request: Request):
    """Withdraw N Solari from a portal player's CHOAM bank balance (Credit) into a
    SolarisCoin item stack (Coin) in their bank item storage. OFFLINE-only (the bank
    item storage is RAM-backed while online; the writer hard-gates on online_status +
    grace). Body = {owner_ctrl:int, amount:int (1..100000)}. owner_ctrl is resolved
    SERVER-SIDE by the admin-backend; the writer re-verifies offline + bank ownership and
    locks the bank balance before debiting."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    return _dune_storage_action("withdraw", body)


@app.post("/dune/storage/deposit", dependencies=[Depends(verify_key)])
async def dune_storage_deposit(request: Request):
    """Deposit a portal player's SolarisCoin item stacks back into their CHOAM bank
    balance (Coin -> Credit). OFFLINE-only (coin stacks live in RAM-backed inventories;
    the writer hard-gates). Body = {owner_ctrl:int, mode:"sweep"|"amount", amount?:int}.
    mode=sweep sums + deletes every owned SolarisCoin stack; mode=amount consumes exactly
    `amount` across stacks (amount required). owner_ctrl is resolved SERVER-SIDE; the
    writer re-verifies offline + gathers coins only from inventories the player owns."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    return _dune_storage_action("deposit", body)


@app.post("/dune/landsraad/claim", dependencies=[Depends(verify_key)])
async def dune_landsraad_claim(request: Request):
    """Claim every unclaimed Landsraad house reward for a portal player (backpack first,
    CHOAM Exchange Completed orders for the overflow, swatches as unlocks). OFFLINE-only
    for mode=apply; mode=preview runs the same transaction and rolls back. Body =
    {owner_ctrl:int, mode:"preview"|"apply", operator?:str}. owner_ctrl is resolved
    SERVER-SIDE by the admin-backend; the writer re-verifies offline in-transaction.

    `mode` has NO default. This transport rebuilds the job from a whitelist, and a
    whitelist rebuild that drops a field must fail closed, not fall through to the write
    (AGENTS.md §3, the 2026-08-14 dry_run incident)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    owner_ctrl = body.get("owner_ctrl")
    if isinstance(owner_ctrl, bool) or not isinstance(owner_ctrl, int) or owner_ctrl <= 0:
        raise HTTPException(400, "owner_ctrl must be a positive integer")
    mode = body.get("mode")
    if mode not in ("preview", "apply"):
        raise HTTPException(400, "mode must be one of preview, apply")
    operator = re.sub(r"[^A-Za-z0-9:_#.-]", "", str(body.get("operator") or "portal"))[:64]
    fields = {"owner_ctrl": owner_ctrl, "mode": mode, "operator": operator or "portal"}

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode landsraad-claim payload")
    out, err, code = _dune_ssh_stdin("landsraad-claim", arg, timeout=45)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out.splitlines()[-1])
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": False,
        "error": "writer_no_output",
        "exit_code": code,
        "message": (err or out or "landsraad-claim produced no output")[:1000],
    }


AUGMENT_ID_RE = re.compile(r"^T\d_Augment_[A-Za-z0-9_]{1,48}$")
AUGMENT_IDEM_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@app.post("/dune/augment/apply", dependencies=[Depends(verify_key)])
async def dune_augment_apply(request: Request):
    """Per-item augment REROLL / SWAP write path. Replaces the item's whole
    FAugmentedItemStats block from `augments` (the FULL resulting list, in slot
    order) and redraws rolls only for the slots named in `reroll_only`; every
    other slot keeps its exact current rolls AND grade. OFFLINE-only (item stats
    are RAM-backed while online; the writer hard-gates).

    Body = {owner_ctrl:int, item_id:int, augments:[str], grade?:int,
    roll_mode?:"random"|"perfect"|"transplant", consume?:bool,
    preserve_grades?:bool, reroll_only?:[str], swap_slot?:int,
    idempotency_key?:str}.

    `transplant` is the player-facing swap mode: instead of drawing fresh rolls
    for the incoming augment it splices the CONSUMED copy's real rolls into the
    destination slot, so a hand-picked augment no longer arrives as an average
    draw. It carries NO new field -- it is a third accepted value of `roll_mode`
    and nothing else.

    🔴 owner_ctrl is resolved SERVER-SIDE by the admin-backend from the session.
    It is never accepted from a browser: the writer trusts it completely to
    resolve the pawn, ownership and the offline gate, so it IS the auth boundary.
    The writer re-verifies offline + item ownership + augment ownership, and on a
    swap consumes the incoming augment in the SAME transaction as the write.

    `idempotency_key` is honoured in-transaction: a retry of an already-applied
    intent replays the prior outcome and consumes NOTHING. Send one for anything
    player-initiated -- a dropped response plus a retry would otherwise destroy a
    second augment, of which only ~297 exist server-wide.

    Kill-switch: the writer refuses with augment_disabled unless
    LASTSIETCH_AUGMENT_ENABLED=1. The writer's JSON (incl. ok:false error tokens) is
    surfaced verbatim."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    def _pos_int(name):
        val = body.get(name)
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    def _aug_list(name, required):
        raw = body.get(name)
        if raw is None:
            if required:
                raise HTTPException(400, f"{name} is required")
            return None
        if not isinstance(raw, list) or not raw or len(raw) > 3:
            raise HTTPException(400, f"{name} must be a 1-3 element array")
        out = []
        for a in raw:
            if not isinstance(a, str) or not AUGMENT_ID_RE.match(a):
                raise HTTPException(400, f"{name} entries must look like T6_Augment_Name")
            out.append(a)
        return out

    fields = {
        "owner_ctrl": _pos_int("owner_ctrl"),
        "item_id": _pos_int("item_id"),
        "augments": _aug_list("augments", True),
    }

    grade = body.get("grade")
    if grade is not None:
        if isinstance(grade, bool) or not isinstance(grade, int) or not 1 <= grade <= 5:
            raise HTTPException(400, "grade must be an integer 1..5")
        fields["grade"] = grade

    roll_mode = body.get("roll_mode")
    if roll_mode is not None:
        if roll_mode not in ("random", "perfect", "transplant"):
            raise HTTPException(
                400, "roll_mode must be 'random', 'perfect' or 'transplant'")
        fields["roll_mode"] = roll_mode

    # 🔴 `dry_run` MUST be in this list. It was omitted until 2026-08-14, and because
    # this handler rebuilds the request from a whitelist rather than forwarding the
    # body, the flag was silently DROPPED -- so the admin-backend's "authoritative
    # dry-run probe" was a LIVE WRITE on every reroll from 08-11 13:56 onward. The
    # writer honours the flag correctly; it simply never arrived. Symptom was
    # "That item is no longer there." (the live response carries no
    # `already_installed`, which only do_dry_run emits) while the reroll really
    # landed -- uncapped and redrawing EVERY slot, because the probe body also
    # carries no reroll_only or idempotency_key.
    # Anything added to this endpoint's contract must be added here too; a missing
    # entry fails OPEN toward the destructive default, which is exactly backwards.
    # 🔴 `isinstance(val, bool)` FIRST, always: bool subclasses int in Python, so an
    # int check alone accepts True/False for a numeric field and a bool check placed
    # after one never runs. The transplant swap mode deliberately added NO field
    # here -- it is a `roll_mode` value -- precisely so this list did not have to
    # grow again.
    for flag in ("consume", "preserve_grades", "dry_run"):
        val = body.get(flag)
        if val is not None:
            if not isinstance(val, bool):
                raise HTTPException(400, f"{flag} must be a boolean")
            fields[flag] = val

    reroll_only = _aug_list("reroll_only", False)
    if reroll_only is not None:
        fields["reroll_only"] = reroll_only

    # 0-based index into `augments` of the slot a swap REPLACES. Carries the one
    # fact the name list cannot: WHICH slot changed, when the incoming augment has
    # the same name as the outgoing one and only its grade differs. Without it the
    # writer sees no new name, consumes nothing, keeps the old grade and hands out
    # a free upgrade. Subject to the same rule as `dry_run` above -- this handler
    # rebuilds the request from a whitelist, so an omission here is silent.
    # Bounds are re-checked by the writer against its own post-validation list;
    # the ceiling here is just the 3-slot cap `_aug_list` already enforces.
    swap_slot = body.get("swap_slot")
    if swap_slot is not None:
        if isinstance(swap_slot, bool) or not isinstance(swap_slot, int) \
                or not 0 <= swap_slot <= 2:
            raise HTTPException(400, "swap_slot must be an integer 0..2")
        fields["swap_slot"] = swap_slot

    idem = body.get("idempotency_key")
    if idem is not None:
        if not isinstance(idem, str) or not AUGMENT_IDEM_RE.match(idem):
            raise HTTPException(400, "idempotency_key must be 8-64 chars of letters, digits, _ or -")
        fields["idempotency_key"] = idem

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode augment-op payload")

    out, err, code = _dune_ssh_stdin("augment-op", arg, timeout=45)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": False,
        "error": "writer_no_output",
        "exit_code": code,
        "message": (err or out or "augment-op produced no output")[:1000],
        "stderr": err[:2000],
    }


@app.post("/dune/storage/move", dependencies=[Depends(verify_key)])
async def dune_storage_move(request: Request):
    """Drag-drop MOVE WRITE path (Tier 3): relocate a WHOLE item stack from one of a
    portal player's OWNED inventories to another OWNED inventory (first-empty slot, slot +
    volume gate). OFFLINE-only (the inventories are RAM-backed while online; the writer
    hard-gates). Body = {owner_ctrl:int, item_id:int, dst_inventory_id:int,
    expected_template?:str}. owner_ctrl is resolved SERVER-SIDE by the admin-backend; the
    writer re-verifies offline + ownership of BOTH source and destination (owned_inv_sql)
    + the DeepDesert exclusion + the slot/volume gate in-transaction, then does the single
    atomic re-home UPDATE pinned to the source inventory. Kill-switch: the writer refuses
    with move_disabled unless LASTSIETCH_STORAGE_MOVE_ENABLED=1. The writer's JSON (incl. ok:false
    error tokens) is surfaced verbatim."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    def _pos_int(name):
        val = body.get(name)
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    owner_ctrl = _pos_int("owner_ctrl")
    item_id = _pos_int("item_id")
    dst_inventory_id = _pos_int("dst_inventory_id")

    expected_template = body.get("expected_template")
    if expected_template is not None:
        if not isinstance(expected_template, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{2,64}", expected_template):
            raise HTTPException(400, "expected_template must be 2-64 chars of letters, digits, _ or -")

    fields = {"action": "move", "owner_ctrl": owner_ctrl, "item_id": item_id,
              "dst_inventory_id": dst_inventory_id}
    if expected_template is not None:
        fields["expected_template"] = expected_template

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode storage-move payload")

    out, err, code = _dune_ssh_stdin("storage-move", arg, timeout=30)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": False,
        "error": "writer_no_output",
        "exit_code": code,
        "message": (err or out or "storage-move produced no output")[:1000],
        "stderr": err[:2000],
    }


@app.post("/dune/storage/pawn-move", dependencies=[Depends(verify_key)])
async def dune_storage_pawn_move(request: Request):
    """Move one stack only between the selected pawn's Bank and Backpack."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    def _pos_int(name):
        val = body.get(name)
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    owner_ctrl = _pos_int("owner_ctrl")
    item_id = _pos_int("item_id")
    src_storage = body.get("src_storage")
    dst_storage = body.get("dst_storage")
    if src_storage not in ("bank", "backpack") or dst_storage not in ("bank", "backpack"):
        raise HTTPException(400, "storage kinds must be bank or backpack")
    if src_storage == dst_storage:
        raise HTTPException(400, "source and destination must differ")

    idem = body.get("idempotency_key")
    if not isinstance(idem, str) or not _GUILD_OP_UUID_RE.match(idem):
        raise HTTPException(400, "idempotency_key must be a UUID")

    expected_template = body.get("expected_template")
    if expected_template is not None:
        if not isinstance(expected_template, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{2,64}", expected_template):
            raise HTTPException(400, "expected_template must be 2-64 chars of letters, digits, _ or -")

    fields = {
        "action": "pawn_move",
        "owner_ctrl": owner_ctrl,
        "item_id": item_id,
        "src_storage": src_storage,
        "dst_storage": dst_storage,
        "idempotency_key": idem,
    }
    if expected_template is not None:
        fields["expected_template"] = expected_template

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode storage-pawn-move payload")

    out, err, code = _dune_ssh_stdin("storage-pawn-move", arg, timeout=30)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": False,
        "error": "writer_no_output",
        "exit_code": code,
        "message": (err or out or "storage-pawn-move produced no output")[:1000],
        "stderr": err[:2000],
    }


# ---------------------------------------------------------------------------
# The Ingot Refinery (wave 10): offline ingots + Spice Melange -> spiced dust.
# Two routes, ONE dispatcher token (`refinery-op`); the op rides inside the
# base64 payload and dune-refinery-write.py re-validates it along with every
# other field, the way the Karum's four routes share `karum-op`.
#
# owner_ctrl and account_id are resolved SERVER-SIDE by the admin-backend from
# the authenticated session before they reach here, and re-resolved inside the
# writer's transaction. A client can never refine AS another player.
#
# 🔴 `fields` IS A WHITELIST. This handler REBUILDS the request rather than
# forwarding the body, so a field missing from the list below is silently
# DROPPED, and every omission fails toward the destructive default. `dry_run` is
# the one that has already cost us: it was left off the augment route until
# 2026-08-14, and for three days the admin-backend's "authoritative dry-run
# probe" was a LIVE WRITE on every reroll. The five `expected_*` fields are the
# same class of hazard: they carry the rate the player was SHOWN, and a dropped
# one turns the writer's rate_changed guard into a trade filled at whatever
# number the host happens to be quoting now.
_REFINERY_OPS = ("catalog", "exchange")
_REFINERY_TEMPLATE_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
# SAFETY bound, NOT the product ceiling. The rate table on the game host owns
# the real per-request limit (max_batches_per_request, 50 today) and the portal
# route refuses above whatever the catalog published. This number exists only so
# a hand-built body cannot ask for a transaction of unbounded size, and it is
# one full output stack: 500 dust is the most a single mint could ever land in
# one slot, so nothing legitimate is above it and the owner can retune the real
# ceiling anywhere below it without touching the relay.
_REFINERY_SAFETY_MAX_BATCHES = 500


def _dune_refinery_action(op: str, request_body: dict):
    """Shared CATALOG/EXCHANGE relay path for the portal Ingot Refinery. Validates
    the body, base64-encodes a compact JSON job, and hands it to the dispatcher's
    refinery-op forced command over stdin. The refinery writer owns the ok/error
    contract; its JSON is surfaced verbatim. Mirrors _dune_storage_action."""
    body = request_body
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    if op not in _REFINERY_OPS:
        raise HTTPException(400, "unknown refinery op")

    def _pos_int(name, maximum=None):
        val = body.get(name)
        if val is None:
            raise HTTPException(400, f"{name} is required")
        # isinstance(val, bool) FIRST, always: bool subclasses int in Python, so
        # an int check alone accepts True/False for a numeric field.
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        if maximum is not None and val > maximum:
            raise HTTPException(400, f"{name} must not exceed {maximum}")
        return val

    def _template(name):
        val = body.get(name)
        # fullmatch, not match: `$` also matches before a trailing newline, so
        # .match would let "CopperBar\n" through into the base64 payload.
        if not isinstance(val, str) or not _REFINERY_TEMPLATE_RE.fullmatch(val):
            raise HTTPException(
                400, f"{name} must be 2-64 chars of letters, digits, _ or -")
        return val

    fields = {
        "op": op,
        "owner_ctrl": _pos_int("owner_ctrl"),
        "account_id": _pos_int("account_id"),
    }

    if op == "exchange":
        fields["output_template"] = _template("output_template")
        fields["batches"] = _pos_int("batches", _REFINERY_SAFETY_MAX_BATCHES)
        fields["expected_input_template"] = _template("expected_input_template")
        fields["expected_spice_template"] = _template("expected_spice_template")
        fields["expected_input_per_batch"] = _pos_int("expected_input_per_batch")
        fields["expected_spice_per_batch"] = _pos_int("expected_spice_per_batch")
        fields["expected_output_per_batch"] = _pos_int("expected_output_per_batch")
        # 🔴 Whole-table rate version. Subject to the same rule as dry_run above:
        # this handler REBUILDS the request from this list, so leaving it out
        # does not fail the guard loudly, it deletes the guard. A retune that
        # moved only max_stack or the weekly cap would then apply to a page that
        # had been open since before it.
        # Same bound and charset as the writer's own validation, and as the
        # route's. fullmatch, not match: `$` also matches before a trailing
        # newline, and nothing here strips one.
        rate_version = body.get("expected_rate_version")
        if not isinstance(rate_version, str) or not re.fullmatch(
                r"[A-Za-z0-9._-]{1,32}", rate_version):
            raise HTTPException(
                400, "expected_rate_version must be 1-32 chars of letters, "
                     "digits, dot, underscore or hyphen")
        fields["expected_rate_version"] = rate_version
        idem = body.get("uuid")
        # fullmatch for the same reason as _template above.
        if not isinstance(idem, str) or not _GUILD_OP_UUID_RE.fullmatch(idem):
            raise HTTPException(400, "uuid must be a UUID")
        fields["uuid"] = idem

    # 🔴 Read the comment above this function before touching this. Optional on
    # purpose (a catalog read has nothing to rehearse), but when it is sent it
    # MUST arrive: a dropped dry_run is a live write wearing a probe's name.
    dry_run = body.get("dry_run")
    if dry_run is not None:
        if not isinstance(dry_run, bool):
            raise HTTPException(400, "dry_run must be a boolean")
        fields["dry_run"] = dry_run

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, f"failed to encode refinery-{op} payload")

    out, err, code = _dune_ssh_stdin("refinery-op", arg, timeout=45)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": False,
        "error": "writer_no_output",
        "exit_code": code,
        "message": (err or out or f"refinery-{op} produced no output")[:1000],
        "stderr": err[:2000],
    }


@app.post("/dune/refinery/catalog", dependencies=[Depends(verify_key)])
async def dune_refinery_catalog(request: Request):
    """READ: the Ingot Refinery rate table (scripts/refinery-rates.json on the game
    host, the single rate authority) plus this character's ingot, melange and dust
    holdings summed across bank, backpack and toolbar. Opens no write session.
    Body = {op:"catalog", owner_ctrl:int, account_id:int}."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    return _dune_refinery_action("catalog", body)


@app.post("/dune/refinery/exchange", dependencies=[Depends(verify_key)])
async def dune_refinery_exchange(request: Request):
    """WRITE: consume `batches` lots of ingots plus Spice Melange from the selected
    character's bank, backpack and toolbar (in that order) and mint the matching
    spiced dust into the bank. OFFLINE-only: taking from an inventory the engine
    holds in RAM is a duplication path, so the writer hard-gates on online_status
    plus the reconnect grace under a row lock.

    Body = {op:"exchange", owner_ctrl:int, account_id:int, output_template:str,
    batches:int (1..50), expected_input_template:str, expected_spice_template:str,
    expected_input_per_batch:int, expected_spice_per_batch:int,
    expected_output_per_batch:int, uuid:str, dry_run?:bool}. The expected_* fields
    carry the rate the player was shown; the writer refuses `rate_changed` rather
    than filling at a rate nobody agreed to.

    `uuid` is honoured in-transaction: a retry of an already-applied intent
    replays the prior outcome and consumes NOTHING.

    Kill switch: the writer refuses with refinery_disabled, without opening a DB
    session, unless /etc/lastsietch/refinery-enabled exists. An explicit dry_run still
    runs and rolls back while dark, which is what makes the deploy lane's dark
    round trip a proof rather than a restatement of the refusal. The writer's
    JSON is surfaced verbatim."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    return _dune_refinery_action("exchange", body)


def _dune_repair_action(action: str, request_body: dict):
    """Shared WRITE relay path for the portal Item Repair feature (box/gear/everything).
    Validates the body, base64-encodes a compact JSON job, and hands it to the
    dispatcher's repair-box/repair-gear/repair-all action over stdin (same path as
    /dune/storage/*). The repair writer owns the ok/error contract; its JSON is surfaced
    verbatim. owner_ctrl is resolved SERVER-SIDE by the admin-backend; the writer
    re-verifies the offline gate + ownership in-transaction. Mirrors _dune_storage_action."""
    body = request_body
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    if action not in ("box", "gear", "everything", "vehicle"):
        raise HTTPException(400, "unknown repair action")

    def _pos_int(name):
        val = body.get(name)
        if val is None:
            raise HTTPException(400, f"{name} is required")
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    owner_ctrl = _pos_int("owner_ctrl")
    fields = {"action": action, "owner_ctrl": owner_ctrl}
    if action in ("box", "vehicle"):
        fields["inv_id"] = _pos_int("inv_id")

    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, f"failed to encode repair-{action} payload")

    token = {"box": "repair-box", "gear": "repair-gear",
             "everything": "repair-all", "vehicle": "repair-vehicle"}[action]
    out, err, code = _dune_ssh_stdin(token, arg, timeout=30)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": False,
        "error": "writer_no_output",
        "exit_code": code,
        "message": (err or out or f"repair-{action} produced no output")[:1000],
        "stderr": err[:2000],
    }


@app.post("/dune/repair/box", dependencies=[Depends(verify_key)])
async def dune_repair_box(request: Request):
    """Vanilla repair of every durable item in ONE owned container (tops CurrentDurability
    up to each row's DecayedMaxDurability). OFFLINE-only (the container is RAM-backed while
    online; the writer hard-gates on online_status + grace). Body = {owner_ctrl:int,
    inv_id:int}. owner_ctrl is resolved SERVER-SIDE by the admin-backend; the writer
    re-verifies inv_id is a member of owned_inv_sql(owner_ctrl) (-> not_owned) and the
    offline gate in-transaction."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    return _dune_repair_action("box", body)


@app.post("/dune/repair/gear", dependencies=[Depends(verify_key)])
async def dune_repair_gear(request: Request):
    """Vanilla repair of a player's CARRIED inventories (backpack inv 0, worn armor inv 1,
    hotbar/weapons inv 15) on their pawn. OFFLINE-only (carried inventories are RAM-backed
    while online; the writer hard-gates). Body = {owner_ctrl:int}. owner_ctrl is resolved
    SERVER-SIDE; the writer resolves the pawn from owner_ctrl and only ever touches that
    pawn's own inventories."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    return _dune_repair_action("gear", body)


@app.post("/dune/repair/everything", dependencies=[Depends(verify_key)])
async def dune_repair_everything(request: Request):
    """Factory REFURBISH of all owned storage + bank + the carried pawn inventories
    (writes BOTH CurrentDurability AND DecayedMaxDurability up to MaxDurability, wiping
    accumulated decay). OFFLINE-only (the writer hard-gates). Body = {owner_ctrl:int}.
    owner_ctrl is resolved SERVER-SIDE; the writer targets only owned_inv_sql(owner_ctrl)
    UNION the owner's pawn inventories (DD excluded) and re-verifies offline + ownership
    in-transaction."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    return _dune_repair_action("everything", body)


@app.post("/dune/repair/vehicle", dependencies=[Depends(verify_key)])
async def dune_repair_vehicle(request: Request):
    """Factory REFURBISH of ONE owned vehicle's parts IN PLACE (writes BOTH
    CurrentDurability AND DecayedMaxDurability up to MaxDurability on every durable row in
    the vehicle's inventories, wiping accumulated decay -- no dismounting). OFFLINE-only
    (the writer hard-gates). Body = {owner_ctrl:int, inv_id:int} where inv_id is one of the
    selected vehicle's inventory ids. owner_ctrl is resolved SERVER-SIDE; the writer
    resolves the vehicle actor from inv_id and re-verifies the caller holds a
    permission_actor_rank on that vehicle (-> not_owned) plus the offline gate."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    return _dune_repair_action("vehicle", body)


@app.get("/dune/stats/reports", dependencies=[Depends(verify_key)])
def dune_stats_report(period: str, window_start: int, window_end: int):
    import base64
    import time
    span = window_end - window_start
    if (period not in ("daily", "weekly") or window_start < 1 or window_end > time.time() + 1
            or time.time() - window_end > 31 * 86400
            or not (23 * 3600 <= span <= 25 * 3600 if period == "daily" else 167 * 3600 <= span <= 169 * 3600)):
        raise HTTPException(400, "invalid report window")
    job = {"period": period, "window_start": window_start, "window_end": window_end}
    encoded = base64.b64encode(json.dumps(job, separators=(",", ":")).encode()).decode()
    result = _dune_ssh_json("report-collect " + encoded, timeout=90)
    if (not isinstance(result, dict) or result.get("schema") != 2 or result.get("period") != period
            or result.get("window_start") != window_start or result.get("window_end") != window_end):
        raise HTTPException(502, "report collector contract mismatch")
    return result


@app.get("/dune/stats/digest", dependencies=[Depends(verify_key)])
def dune_stats_digest(period: str = "daily"):
    """Structured server-stats blob for the Cielago digest (Last Sietch).
    period = 'daily' | 'weekly'. Read-only collector on <game-host>; Cielago
    renders + posts the embed. Returns pure data, no Discord/branding."""
    if period not in ("daily", "weekly"):
        raise HTTPException(400, "period must be daily or weekly")
    return _dune_ssh_json(f"stats-collect {period}", timeout=90)


@app.get("/dune/players/positions", dependencies=[Depends(verify_key)])
def dune_players_positions():
    """Online player coordinates on Hagga Basin from dune.actors. Read-only.
    Raw pass-through: since 2026-09-04 each point also carries `a` (account id)
    and `n` (character name) for the admin Live Map; the PUBLIC projection in
    admin-backend routers/dune.py strips them and drops untracked partitions."""
    try:
        return _dune_ssh_json("positions", timeout=60)
    except HTTPException as e:
        return {"map": "HaggaBasin", "available": False, "error": e.detail}


@app.get("/dune/players/vehicles", dependencies=[Depends(verify_key)])
def dune_players_vehicles():
    """World-vehicle coordinates + type on Hagga Basin from dune.actors.
    Read-only. PII-safe: coords + type only, no owners/names — safe for the
    public map."""
    try:
        return _dune_ssh_json("vehicles", timeout=60)
    except HTTPException as e:
        return {"map": "HaggaBasin", "available": False, "error": e.detail}


@app.get("/dune/spice/active", dependencies=[Depends(verify_key)])
def dune_spice_active():
    """Active Deep Desert Large spice field per dimension: authoritative liveness
    (spicefield_types) + the RAM bloom-discriminator sector (the surfaced field,
    m_BloomVariationIndex != -1), with the harvester cluster as a fallback hint.
    Read-only, global data, PII-safe (sector + aggregate actor counts only)."""
    try:
        return _dune_ssh_json("spice-active", timeout=45)
    except HTTPException as e:
        return {"dimensions": {}, "available": False, "error": e.detail}


@app.get("/dune/worms", dependencies=[Depends(verify_key)])
def dune_worms():
    """Live Deep Desert sandworm tracker per dimension: each active worm's
    last-known position (sector + world x/y) and threat state (surfaced /
    enraged / wants-to-breach / attacking), parsed read-only from the DD pod
    logs. Global data, PII-safe (worm positions only, no players)."""
    try:
        return _dune_ssh_json("worms", timeout=45)
    except HTTPException as e:
        return {"dimensions": {}, "available": False, "error": e.detail}


@app.get("/dune/sandstorm", dependencies=[Depends(verify_key)])
def dune_sandstorm():
    """Time-only sandstorm ETA forecast per Deep Desert dimension: last spawn,
    next-storm ETA, mean spawn interval, and a confidence from the cadence
    spread, parsed read-only from the DD pod logs (the logs carry only
    spawn-event timestamps -- no coordinates/path/intensity). Global data,
    PII-safe (no players)."""
    try:
        return _dune_ssh_json("sandstorm", timeout=60)
    except HTTPException as e:
        return {"dimensions": {}, "available": False, "error": e.detail}


@app.get("/dune/positions/stream", dependencies=[Depends(verify_key)])
async def dune_positions_stream(request: Request):
    """SSE pass-through of the <game-host> telemetry live-positions stream.

    The dispatcher's `positions-stream` token streams the telemetry read API's
    text/event-stream (GET /positions/stream) over the SSH stdout pipe; this
    route relays those frames verbatim to the admin-backend. Read-only and
    PII-safe (coordinates only). The dashboard falls back to the 5s
    /dune/players/positions poll if the stream drops."""
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
               "-o", "StrictHostKeyChecking=yes",
               "-o", f"UserKnownHostsFile={DUNE_SSH_KNOWN_HOSTS}",
               *SSH_CONTROLMASTER_OPTS,
               "-i", DUNE_SSH_KEY, "-l", DUNE_SSH_USER,
               DUNE_SSH_HOST, "positions-stream"]

    async def gen():
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            while True:
                if await request.is_disconnected():
                    break
                line = await proc.stdout.readline()
                if not line:
                    break
                yield line
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/dune/players/roster", dependencies=[Depends(verify_key)])
def dune_players_roster():
    """Per-map online player roster from the game DB. Read-only.
    Contains character names (PII) — the admin-backend proxy is auth-gated."""
    try:
        return _dune_ssh_json("roster", timeout=60)
    except HTTPException as e:
        return {"available": False, "error": e.detail}


@app.get("/dune/fields", dependencies=[Depends(verify_key)])
def dune_fields():
    """Resource + spice field aggregates per partition from the game DB. Read-only."""
    try:
        return _dune_ssh_json("fields", timeout=60)
    except HTTPException as e:
        return {"available": False, "error": e.detail}


@app.get("/dune/stats/presence", dependencies=[Depends(verify_key)])
def dune_stats_presence(window: str = "24h"):
    """Historical player counts from the lastsietch-telemetry SQLite presence table. Read-only."""
    if not DUNE_WINDOW_RE.match(window):
        raise HTTPException(400, "Invalid window (alphanumeric only, e.g. 1h, 24h, 7d)")
    return _dune_ssh_json(f"presence {window}", timeout=30)


@app.get("/dune/player/{account_id}/tags", dependencies=[Depends(verify_key)])
def dune_player_tags(account_id: str):
    """Read-only player-tag list from dune.admin_read_player_tags(account_id).
    Numeric account_id is enforced both here and in the dispatcher allowlist;
    the on-disk script binds it as a psql variable, never string-concatenates.
    """
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return _dune_ssh_json(f"tags-read {account_id}", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "tags": []}


@app.get("/dune/player/{account_id}/progress", dependencies=[Depends(verify_key)])
def dune_player_progress(account_id: str, ctrl: str = "", list: str = ""):
    """Read-only character (XP / skill points) + economy (Solari / Scrip) for
    the public portal dashboard. Numeric account_id enforced here and in the
    dispatcher allowlist; the on-disk script binds it as a psql variable.

    Multi-character (portal switcher): ?list=1 returns every non-Deleted
    character on the account; ?ctrl=<controller_id> scopes the read to one
    character. Both are numeric-validated here and re-validated by the
    dispatcher allowlist before reaching the script."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    if list in ("1", "true"):
        cmd = f"player-progress {account_id} --list"
    elif ctrl:
        if not ctrl.isdigit():
            raise HTTPException(400, "ctrl must be a positive integer")
        cmd = f"player-progress {account_id} --controller {ctrl}"
    else:
        cmd = f"player-progress {account_id}"
    try:
        return _dune_ssh_json(cmd, timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail}


@app.get("/dune/player/{account_id}/equipped", dependencies=[Depends(verify_key)])
def dune_player_equipped(account_id: str):
    """Read-only equipped-gear loadout (inventory_type=1) for the public portal
    character stage: per-slot template_id + mesh VariantId + dye SwatchId +
    durability. Numeric account_id enforced here and in the dispatcher allowlist."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return _dune_ssh_json(f"player-equipped {account_id}", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail}


@app.get("/dune/player/{account_id}/map", dependencies=[Depends(verify_key)])
def dune_player_map(account_id: str):
    """Read-only per-player map overlay: the player's OWN position + base totems
    + owned vehicles (coords + map + dimension). Numeric account_id enforced here
    and in the dispatcher allowlist; the on-disk script binds it as a psql
    variable. Carries the player's own position, so the admin-backend only serves
    it for the caller's own session-bound account (never public, never arbitrary)."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return _dune_ssh_json(f"player-map {account_id}", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail}


@app.get("/dune/player/{account_id}/containers", dependencies=[Depends(verify_key)])
def dune_player_containers(account_id: str):
    """Read-only per-player container list. 4-class storage whitelist, world
    POIs + holograms filtered. Numeric account_id is enforced here and in the
    dispatcher allowlist; the on-disk script re-validates and binds it before
    interpolation into the SQL template."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return _dune_ssh_json(f"containers-list {account_id}", timeout=45)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "containers": []}


@app.get("/dune/player/{account_id}/container-search", dependencies=[Depends(verify_key)])
def dune_player_container_search(account_id: str):
    """Read-only cross-container item index for the portal search: every item
    across the account's storage containers, aggregated by (container,
    template_id) with summed quantity. Friendly-name matching happens in
    admin-backend; this is one DB round-trip for the whole search."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return _dune_ssh_json(f"container-search {account_id}", timeout=45)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "rows": []}


@app.get("/dune/player/{account_id}/my-orders", dependencies=[Depends(verify_key)])
def dune_player_my_orders(account_id: str):
    """Read-only "My Orders" panel for the public portal: the player's active
    sell listings, their Completed tab (purchased/sold/canceled), and recent
    realised-trade history. Numeric account_id is enforced here and in the
    dispatcher allowlist; the on-disk helper resolves controller_id server-side
    so a player can only ever see their own orders."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return _dune_ssh_json(f"my-orders {account_id}", timeout=45)
    except HTTPException as e:
        return {"available": False, "error": e.detail,
                "active": [], "completed": [], "history": []}


@app.get("/dune/player/{account_id}/_container/{container_id}/_items",
         dependencies=[Depends(verify_key)])
def dune_player_container_items(account_id: str, container_id: str, page: int = 1):
    """Read-only per-container items list for the v2 admin Player Tools tab
    drill-down (LIFT-10). Both IDs are numeric-only (re-validated by the
    dispatcher allowlist + by the on-disk helper script). Ownership is
    verified server-side via a JOIN chain; a `not_owned` envelope here is
    mapped to a 404 by the admin-backend route."""
    if not account_id.isdigit() or not container_id.isdigit():
        raise HTTPException(400, "account_id and container_id must be positive integers")
    if not isinstance(page, int) or page < 1:
        page = 1
    try:
        return _dune_ssh_json(f"container-items {account_id} {container_id} {page}", timeout=45)
    except HTTPException as e:
        return {
            "available": False, "error": e.detail,
            "account_id": account_id, "container_id": container_id,
            "items": [], "count": 0, "total_count": 0,
            "page": page, "page_size": 100,
        }


@app.get("/dune/player/{account_id}/_vehicle/{container_id}/_parts",
         dependencies=[Depends(verify_key)])
def dune_player_vehicle_parts(account_id: str, container_id: str):
    """Read-only INSTALLED-parts durability list for one owned vehicle (the selected
    storage-browser vehicle, keyed by its cargo container_id). Reads dune.vehicle_modules
    (NOT items -- installed parts are not items). Ownership verified server-side; a
    `not_owned` envelope maps to a 404 in the admin-backend route."""
    if not account_id.isdigit() or not container_id.isdigit():
        raise HTTPException(400, "account_id and container_id must be positive integers")
    try:
        return _dune_ssh_json(f"vehicle-parts {account_id} {container_id}", timeout=45)
    except HTTPException as e:
        return {
            "available": False, "error": e.detail,
            "account_id": account_id, "container_id": container_id,
            "parts": [], "count": 0,
        }


@app.get("/dune/player/{account_id}/blueprints", dependencies=[Depends(verify_key)])
def dune_player_blueprints(account_id: str):
    """Order 0 / G30 v1 — list a player's BuildingBlueprint_CopyDevice items
    plus their linked building_blueprints rows + piece counts. Drives the
    v2 admin Export subtab's blueprint picker. Read-only."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return _dune_ssh_json(f"blueprints-list {account_id}", timeout=45)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "blueprints": [], "count": 0}


@app.get("/dune/player/{account_id}/blueprint/{bp_id}/export",
         dependencies=[Depends(verify_key)])
def dune_player_blueprint_export(account_id: str, bp_id: str):
    """Order 0 / G30 v1 — Solido-market JSON dump of a single blueprint.
    Verbatim icehunter cmdExportBlueprint schema. Ownership is verified
    server-side; a `not_owned` envelope from the helper is mapped to a
    404 by the admin-backend route."""
    if not account_id.isdigit() or not bp_id.isdigit():
        raise HTTPException(400, "account_id and bp_id must be positive integers")
    try:
        return _dune_ssh_json(f"blueprint-export {account_id} {bp_id}", timeout=60)
    except HTTPException as e:
        return {"available": False, "error": e.detail,
                "account_id": account_id, "bp_id": bp_id, "blueprint": None}


@app.post("/dune/blueprint/rename", dependencies=[Depends(verify_key)])
async def dune_blueprint_rename(request: Request):
    """Rename one of a portal player's OWN base blueprints (the My Bases page). Sets the
    BuildingBlueprintName field on the BuildingBlueprint_CopyDevice item's stats JSONB.
    OFFLINE-only (the copy device is a RAM-backed inventory item; the writer hard-gates on
    online_status + grace). Body = {account_id:int, bp_id:int, name:str}. account_id is
    resolved SERVER-SIDE by the admin-backend from the caller's linked characters; the
    writer re-verifies ownership + the offline gate in-transaction. Mirrors the
    _dune_storage_action stdin path. The rename writer owns the ok/error contract."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    def _pos_int(name):
        val = body.get(name)
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise HTTPException(400, f"{name} must be a positive integer")
        return val

    account_id = _pos_int("account_id")
    bp_id = _pos_int("bp_id")
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(400, "name is required")
    if len(name) > 200:  # generous outer bound; the writer applies the real 40-char cap
        raise HTTPException(400, "name too long")

    fields = {"account_id": account_id, "bp_id": bp_id, "name": name}
    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode blueprint-rename payload")

    out, err, code = _dune_ssh_stdin("blueprint-rename", arg, timeout=30)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": False,
        "error": "writer_no_output",
        "exit_code": code,
        "message": (err or out or "blueprint-rename produced no output")[:1000],
        "stderr": err[:2000],
    }


@app.get("/dune/player/{account_id}/character/export",
         dependencies=[Depends(verify_key)])
def dune_player_character_export(account_id: str, ctrl: str = ""):
    """Last Sietch character snapshot dump. FLevelComponent + FactionPlayerComponent +
    tags + spec_tracks + full actor properties. Read-only; Last Sietch-internal schema.

    ?ctrl=<controller_id> scopes the snapshot to ONE character (portal
    "Download my data" for multichar accounts); omit for the account-level
    admin export. Numeric-validated here and re-validated by the dispatcher
    allowlist before reaching the helper."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    if ctrl:
        if not ctrl.isdigit():
            raise HTTPException(400, "ctrl must be a positive integer")
        cmd = f"character-export {account_id} --controller {ctrl}"
    else:
        cmd = f"character-export {account_id}"
    try:
        return _dune_ssh_json(cmd, timeout=60)
    except HTTPException as e:
        return {"available": False, "error": e.detail,
                "account_id": account_id, "snapshot": None}


@app.get("/dune/player/{account_id}/progression_state",
         dependencies=[Depends(verify_key)])
def dune_player_progression_state(account_id: str):
    """Phase 1/2: combined progression read for the Specializations + Skills
    pickers: owned_keystone_ids + spec_tracks (per-track level/xp) +
    learned_blocks (Skills.Key.* with SkillPointsSpent>=1). Read-only."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return _dune_ssh_json(f"progression-state {account_id}", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail,
                "account_id": account_id, "owned_keystone_ids": [],
                "spec_tracks": {}, "learned_blocks": []}


@app.get("/dune/player/{account_id}/vitals", dependencies=[Depends(verify_key)])
def dune_player_vitals(account_id: str):
    """V2 Identity tab: per-player economy (online-safe Solari/Scrip balances)
    + activity (telemetry presence: hours, last-seen, active days). Read-only."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return _dune_ssh_json(f"player-vitals {account_id}", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "account_id": account_id,
                "economy": {"available": False}, "activity": {"available": False}}


@app.get("/dune/player/{account_id}/landsraad-rewards", dependencies=[Depends(verify_key)])
def dune_player_landsraad_rewards(account_id: str):
    """Public portal: a player's own unclaimed Landsraad rewards (per-house
    items awaiting pickup + Solari totals). Read-only. The portal scopes the
    request to the OAuth-bound account_id, so a player only ever sees their own."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return _dune_ssh_json(f"landsraad-rewards {account_id}", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "account_id": account_id,
                "houses": [], "summary": {"total_lines": 0, "total_solari": 0,
                                          "houses_with_rewards": 0, "oldest_epoch": None}}


@app.get("/dune/landsraad/board", dependencies=[Depends(verify_key)])
def dune_landsraad_board():
    """Public portal: the live Landsraad term board (term-global). 25 minor-house
    tiles + per-faction progress + great-house score + top-guild contributors +
    reward ladders for the current term. Read-only. Global (not per-account); the
    admin-backend caches it for the portal (30s ttl), same as /dune/guilds."""
    return _dune_ssh_json("landsraad-board", timeout=30)


@app.get("/dune/progression/snapshot", dependencies=[Depends(verify_key)])
def dune_progression_snapshot():
    """Latest per-account progression sample for the dashboard widgets.
    Read-only passthrough of the telemetry-api /progression/snapshot. Carries
    character names — the admin-backend proxy is the public surface and
    enforces rate limiting; names are visible per leaderboard convention."""
    try:
        return _dune_ssh_json("progression-snapshot", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "count": 0, "players": []}


@app.get("/dune/progression/levelups", dependencies=[Depends(verify_key)])
def dune_progression_levelups(limit: int = 100):
    """Recent level-up events for the dashboard ticker. Read-only passthrough
    of the telemetry-api /progression/levelups. Clamped to 1..1000."""
    if not isinstance(limit, int) or limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000
    try:
        return _dune_ssh_json(f"progression-levelups {limit}", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "count": 0, "levelups": []}


# --- Progression-grant admin tool (write path) ---
#
# These two routes back the Dune progression-grant tool. They reach <game-host>
# through the same forced-command dispatcher; the dispatcher maps "grant-players"
# and "grant <b64>" to /root/dune-grant.sh, which does its own kubectl exec.
# The SSH key on <game-host> is widened from read-only to write for this — the
# dispatcher's base64-only arg allowlist is the airtight boundary.

# Grant payload base64 alphabet — the ONLY characters the relay forwards. The
# dispatcher re-checks this; the script re-validates every decoded field. Three
# independent layers, all required (plan section 8 / risk R1).
DUNE_GRANT_B64_RE = re.compile(r'^[A-Za-z0-9+/=]+$')


@app.get("/dune/grant/players", dependencies=[Depends(verify_key)])
def dune_grant_players():
    """Every character row (incl. OFFLINE) for the grant-tool player picker.

    Unlike /dune/players/roster (online only) this returns all accounts with
    online_status + grace flag. Contains character names (PII) — the
    admin-backend proxy is auth-gated (require_admin)."""
    try:
        return _dune_ssh_json("grant-players", timeout=45)
    except HTTPException as e:
        return {"available": False, "error": e.detail}


@app.get("/dune/grant/recent", dependencies=[Depends(verify_key)])
def dune_grant_recent(limit: int = 20):
    """Last N rows of dune.ls_progression_grants for the admin panel's
    "Recent grants" widget. The dispatcher's grant-recent token allowlists
    the limit to numeric-only and clamps to 1..200 server-side."""
    # Clamp here too so a bogus admin-backend query doesn't blow up the SSH cmd
    # — the dispatcher's own regex re-validates, but layered guards.
    if not isinstance(limit, int) or limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    try:
        return _dune_ssh_json(f"grant-recent {limit}", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "grants": []}


@app.get("/dune/preset/list", dependencies=[Depends(verify_key)])
def dune_preset_list():
    """VC3 — Read holadmin.grant_presets. Backs the Workbench preset
    chooser. ttl-cached at admin-backend so the relay call rate is low."""
    try:
        return _dune_ssh_json("preset-list", timeout=15)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "presets": []}


@app.get("/dune/player/{account_id}/recent_grants", dependencies=[Depends(verify_key)])
def dune_player_recent_grants(account_id: int, limit: int = 10):
    """VC3 — Recent grants for a single account. Clamp limit 1..50 here
    (dispatcher also clamps)."""
    if not isinstance(account_id, int) or account_id < 1:
        raise HTTPException(400, "account_id must be a positive integer")
    if not isinstance(limit, int) or limit < 1:
        limit = 1
    if limit > 50:
        limit = 50
    try:
        return _dune_ssh_json(f"grant-recent-by-player {account_id} {limit}", timeout=20)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "account_id": account_id, "grants": []}


@app.get("/dune/player/{account_id}/deliveries", dependencies=[Depends(verify_key)])
def dune_player_deliveries(account_id: int):
    """Fremkit wave 12: the Welcome and Return packages the server sent one
    account, with where each part landed and what is still owed. Read-only.

    A failure degrades to available:false with the same empty shape rather than
    a 502, because the portal card seals on available:false and an exception
    here would seal the whole Home read instead of one panel."""
    if not isinstance(account_id, int) or account_id < 1:
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return _dune_ssh_json(f"deliveries-by-player {account_id}", timeout=20)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "account_id": account_id,
                "welcome": None, "welcome_skip": None, "returns": []}


class GrantPostprocessRequest(BaseModel):
    grant_id: int
    batch_id: str | None = None
    preset_name: str | None = None


@app.post("/dune/grant/postprocess", dependencies=[Depends(verify_key)])
def dune_grant_postprocess(body: GrantPostprocessRequest):
    """VC3 — UPDATE just-applied grant row with batch_id + preset_name.
    Called by admin-backend after a successful grant fire; the relay
    re-validates field formats and forwards to the dispatcher."""
    import re as _re
    if body.grant_id < 1:
        raise HTTPException(400, "grant_id must be a positive integer")
    bid = body.batch_id if body.batch_id else "-"
    pname = body.preset_name if body.preset_name else "-"
    if bid != "-" and len(bid) != 36:
        raise HTTPException(400, "batch_id must be a UUID")
    if pname != "-" and not _re.fullmatch(r"[a-z][a-z0-9_]{0,63}", pname):
        raise HTTPException(400, "preset_name must be snake_case")
    try:
        return _dune_ssh_json(f"grant-postprocess {body.grant_id} {bid} {pname}", timeout=15)
    except HTTPException as e:
        return {"success": False, "error": e.detail}


@app.get("/dune/bb/available-sources", dependencies=[Depends(verify_key)])
async def dune_bb_available_sources():
    """Read-only list of dune.base_backups joined to each backup's totem actor
    + linked-actor count. Backs the BB handoff/clone source picker in the admin
    panel. The dispatcher's bb-available-sources token takes no args."""
    try:
        return _dune_ssh_json("bb-available-sources", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "sources": []}


@app.get("/dune/bb/slot-count", dependencies=[Depends(verify_key)])
async def dune_bb_slot_count(account_id: int):
    """Resolve a target account's player_controller_id and count their existing
    dune.base_backups rows. The dispatcher's bb-slot-count token requires a
    numeric arg; we enforce it here too so a malformed admin-backend query
    never reaches the SSH cmd."""
    if not isinstance(account_id, int) or account_id < 1:
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return _dune_ssh_json(f"bb-slot-count {account_id}", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail}


@app.get("/dune/bb/{backup_id}/detail", dependencies=[Depends(verify_key)])
async def dune_bb_detail(backup_id: int):
    """Read-only detail for a single dune.base_backups row: identity + owner +
    totem + linked-actor count + per-class composition. Backs the admin Bases
    detail drawer. The dispatcher's bb-detail token requires a numeric arg; we
    enforce it here too so a malformed admin-backend query never reaches the
    SSH cmd."""
    if not isinstance(backup_id, int) or backup_id < 1:
        raise HTTPException(400, "backup_id must be a positive integer")
    try:
        return _dune_ssh_json(f"bb-detail {backup_id}", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail}


@app.post("/dune/grant", dependencies=[Depends(verify_key)])
async def dune_grant(request: Request):
    """Execute one progression grant via /root/dune-grant.sh on <game-host>.

    The admin-backend posts a JSON grant body. The relay base64-encodes the
    compact JSON into a single token (no whitespace, no shell metacharacters)
    and hands it to the dispatcher as `grant <b64>`. The script decodes,
    re-validates every field, and runs a parameterized psql transaction.

    The script's stdout and exit code are surfaced faithfully so the
    admin-backend sees exactly what the grant script reported. A non-zero exit
    is NOT remapped to a 502 — a slow but committed grant must not look like a
    failure (idempotency key makes a genuine retry safe; risk R4)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    # Compact, separator-stable JSON so the base64 token is deterministic.
    payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        # base64encode can only ever produce the allowed alphabet; this is a
        # belt-and-braces guard so a malformed token can never reach the SSH cmd.
        raise HTTPException(500, "failed to encode grant payload")

    # 90s: a slow kubectl exec into the DB pod must not time out and report a
    # false failure on a grant that actually committed (risk R4).
    # Use the stdin path so large G20/G22 Solido blueprints (>~150KB b64,
    # roughly 2900+ pieces) don't hit ARG_MAX on the ssh argv. See the
    # 'grant-stdin' case in /root/dune-relay-dispatch.sh.
    out, err, code = _dune_ssh_stdin("grant-stdin", arg, timeout=90)

    out = (out or "").strip()
    err = (err or "").strip()

    # Surface the script's own JSON verbatim when it produced it — the script
    # owns the success/status/grant_id contract.
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass

    # No parseable JSON: hand back stdout + stderr + exit code faithfully so the
    # admin-backend can audit-log the raw result rather than guess.
    return {
        "success": False,
        "status": "failed",
        "exit_code": code,
        "message": (err or out or "grant script produced no output")[:1000],
        "stdout": out[:2000],
        "stderr": err[:2000],
    }


@app.post("/dune/chat/send", dependencies=[Depends(verify_key)])
async def dune_chat_send(request: Request):
    """Send one in-game chat message as the Cielago herald via
    /opt/lastsietch-rmq-bridge/dune-chat-send.py on <game-host>.

    The admin-backend posts {scope, message, mode, recipient?, map?, dim?}. Same
    base64-token + stdin path as /dune/grant: the compact JSON is base64-encoded
    (no whitespace / shell metacharacters) and handed to the dispatcher's
    'chat-send' action, which decodes it and runs the herald with a safe argv.
    mode=dry-run previews; mode=apply publishes live to every targeted client."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode chat payload")

    out, err, code = _dune_ssh_stdin("chat-send", arg, timeout=60)
    out = (out or "").strip()
    err = (err or "").strip()

    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass

    return {
        "success": False,
        "exit_code": code,
        "detail": (err or out or "chat-send produced no output")[:1000],
    }


@app.post('/dune/auth/whisper', dependencies=[Depends(verify_key)])
async def dune_auth_whisper(request: Request):
    try:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise ValueError()
        body = json.loads(raw)
        op = body.get('op')
        if op == 'lookup':
            name = body.get('character')
            if not isinstance(name, str) or not 1 <= len(name) <= 80:
                raise ValueError()
            job = {'op': op, 'character': name}
        elif op in ('send', 'check'):
            aid = body.get('account_id')
            if type(aid) is not int or not 0 < aid < 2**63:
                raise ValueError()
            job = {'op': op, 'account_id': aid}
            if op == 'send':
                code = body.get('code')
                if not isinstance(code, str) or re.fullmatch('[0-9]{6}', code) is None:
                    raise ValueError()
                job['code'] = code
        else:
            raise ValueError()
        from starlette.concurrency import run_in_threadpool
        payload = base64.b64encode(json.dumps(job, separators=(',', ':')).encode()).decode()
        out, _err, status = await run_in_threadpool(_dune_ssh_stdin, 'auth-whisper', payload, timeout=65)
        parsed = json.loads(out) if status == 0 and len(out) <= 4096 else None
        if not isinstance(parsed, dict) or parsed.get('ok') is not True:
            raise ValueError()
        if op == 'lookup':
            return {'ok': True, 'account_id': parsed['account_id'], 'character_name': parsed['character_name']}
        if op == 'send' and parsed.get('sent') is True:
            return {'ok': True, 'sent': True}
        if op == 'check' and parsed.get('private_queue_verified') is True and parsed.get('sent') is False:
            return {'ok': True, 'private_queue_verified': True, 'sent': False}
        raise ValueError()
    except Exception:
        return {'ok': False, 'error': 'game_verification_unavailable'}


@app.post("/dune/broadcast/send", dependencies=[Depends(verify_key)])
async def dune_broadcast_send(request: Request):
    """Publish one in-game ServiceBroadcast (Generic system banner) to every
    connected player via /opt/lastsietch-rmq-bridge/dune-broadcast-send.py on <game-host>.

    The admin-backend posts {title, message, duration, mode}. Same base64-token +
    stdin path as /dune/chat/send: the compact JSON is base64-encoded (no
    whitespace / shell metacharacters) and handed to the dispatcher's
    'broadcast-send' action, which decodes it and runs the wrapper with a safe
    argv. mode=dry-run previews the envelope; mode=apply publishes live to every
    connected client."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode broadcast payload")

    out, err, code = _dune_ssh_stdin("broadcast-send", arg, timeout=60)
    out = (out or "").strip()
    err = (err or "").strip()

    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass

    return {
        "success": False,
        "exit_code": code,
        "detail": (err or out or "broadcast-send produced no output")[:1000],
    }


@app.post("/dune/server-command", dependencies=[Depends(verify_key)])
async def dune_server_command(request: Request):
    """Drive one native server command (give-item / award-xp / teleport /
    refill-water) at an ONLINE player via /opt/lastsietch-rmq-bridge/dune-server-command-send.py
    on the game box.

    The admin-backend posts {verb, resolve|player_id, mode, operator, reason, args}.
    Same base64-token + stdin path as /dune/broadcast/send: the compact JSON is
    base64-encoded (no whitespace / shell metacharacters) and handed to the
    dispatcher's 'server-command' action, which decodes it and runs the wrapper
    with a safe argv. The wrapper enforces a NON-destructive verb allow-list;
    mode=dry-run previews; mode=apply publishes live (and additionally requires the
    box-side master switch /etc/lastsietch/servercmd-enabled)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode server-command payload")

    out, err, code = _dune_ssh_stdin("server-command", arg, timeout=60)
    out = (out or "").strip()
    err = (err or "").strip()

    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass

    return {
        "success": False,
        "exit_code": code,
        "detail": (err or out or "server-command produced no output")[:1000],
    }


@app.get("/dune/chat/players", dependencies=[Depends(verify_key)])
def dune_chat_players():
    """Online players with funcom_id for the admin chat whisper picker (read-only)."""
    return _dune_ssh_json("chat-players", timeout=45)


# --- Moderation trio (Phase C): kick, ban, unban, bans list, bans history. ---
# Dispatcher cases on <game-host>:
#   kick <account_id>          : positional, JSON stdout
#   ban <base64_json>          : base64 token via stdin (large reason fields)
#   unban <account_id>         : positional, JSON payload via stdin
#   bans-list                  : JSON stdout
#   bans-history               : JSON stdout
# Numeric account_id is enforced both here and in the dispatcher allowlist;
# the on-disk scripts re-validate before any psql write or kubectl publish.


@app.post("/dune/player/{account_id}/kick", dependencies=[Depends(verify_key)])
def dune_player_kick(account_id: str):
    """Kick one online player by account_id. The dispatcher resolves
    account_id to fls_id, then publishes one KickPlayer envelope via the
    fls_backend command bus (same path as service-broadcast + chat herald).
    Online-safe; no pod restart. Returns the dispatcher JSON verbatim."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    out, err, code = _dune_ssh(f"kick {account_id}", timeout=45)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "success": False,
        "exit_code": code,
        "detail": (err or out or "kick produced no output")[:1000],
    }


@app.post("/dune/player/{account_id}/ban", dependencies=[Depends(verify_key)])
async def dune_player_ban(account_id: str, request: Request):
    """Insert/activate a holadmin.bans row keyed by fls_id, then publish one
    KickPlayer to disconnect now. A 30s systemd timer re-kicks any banned
    account that reconnects until `expires_at` or `active=false`.

    Request body: {account_id, fls_id, reason, note?, duration_minutes?,
    banned_by, idempotency_key}. Compact JSON is base64-encoded and handed
    to the dispatcher's `ban <b64>` action via stdin, same shape as /dune/grant."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    body_aid = body.get("account_id")
    if body_aid is not None and str(body_aid) != account_id:
        raise HTTPException(400, "body.account_id does not match path account_id")

    payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode ban payload")

    # Dispatcher's `ban) <b64>` case reads the payload from $arg (argv), not
    # stdin. b64 payloads are well under ARG_MAX for moderation actions, so the
    # simpler argv path matches the dispatcher contract.
    out, err, code = _dune_ssh(f"ban {arg}", timeout=60)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "success": False,
        "exit_code": code,
        "detail": (err or out or "ban produced no output")[:1000],
    }


@app.post("/dune/player/{account_id}/unban", dependencies=[Depends(verify_key)])
async def dune_player_unban(account_id: str, request: Request):
    """Set `active=false` on the matching holadmin.bans row; the watcher stops
    re-kicking next tick. Body: {account_id, fls_id, unban_reason,
    unbanned_by, idempotency_key}. Dispatcher case `unban <account_id>` with
    the base64-encoded JSON payload on stdin (same wire shape as `ban` for
    dispatcher symmetry; cheap to base64-decode in the on-disk script)."""
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    body_aid = body.get("account_id")
    if body_aid is not None and str(body_aid) != account_id:
        raise HTTPException(400, "body.account_id does not match path account_id")

    payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode unban payload")
    # Dispatcher's `unban) <b64>` case reads the payload from $arg (argv),
    # matching the ban contract above.
    out, err, code = _dune_ssh(f"unban {arg}", timeout=45)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "success": False,
        "exit_code": code,
        "detail": (err or out or "unban produced no output")[:1000],
    }


@app.get("/dune/bans", dependencies=[Depends(verify_key)])
def dune_bans_list():
    """Active bans from holadmin.bans (active=true, not expired). Admin-gated."""
    try:
        return _dune_ssh_json("bans-list", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "bans": []}


@app.get("/dune/bans/history", dependencies=[Depends(verify_key)])
def dune_bans_history():
    """Full ban history (active + expired + unbanned). Admin-gated."""
    try:
        return _dune_ssh_json("bans-history", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "bans": []}


# --- P8 Server CVars (5-layer INI read, single-target UserOverrides write, ---
# --- settingsUpdate capture diff). Read paths are admin-gated by the      ---
# --- admin-backend; the write path is additionally Operator-gated.        ---


@app.get("/server/cvars/read", dependencies=[Depends(verify_key)])
def server_cvars_read():
    """5-layer INI merge read from the pinned game pod (sg-survival-1-pod-1).
    Read-only kubectl exec inside the pod; returns the merged-walk envelope per
    P8-EXECUTION-BRIEF Appendix B.9. NEVER restarts game pods / BGD / k3s."""
    try:
        return _dune_ssh_json("cvars-read", timeout=60)
    except HTTPException as e:
        return {"status": "error", "error": e.detail,
                "settings": [], "raw_sections": []}


@app.post("/server/cvars/write", dependencies=[Depends(verify_key)])
async def server_cvars_write(request: Request):
    """Single-target write to UserOverrides.ini on the pinned game pod.

    Request body: {section, key, value, operator, reason} — see brief Appendix
    B.9. The relay base64-encodes the compact JSON into a single token; the
    dispatcher's `cvars-write` allowlist + the on-disk helper re-validate every
    field. The helper backs up UserOverrides.ini to `.bak-<UTC>` (when prior
    file existed) before the atomic kubectl-cp+mv. Lazy-create on first write."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    for field in ("section", "key", "operator"):
        if not isinstance(body.get(field), str) or not body[field].strip():
            raise HTTPException(400, f"missing required field: {field}")
    if "value" in body and not isinstance(body["value"], str):
        raise HTTPException(400, "value must be a string")
    if "reason" in body and not isinstance(body["reason"], str):
        raise HTTPException(400, "reason must be a string")

    payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode cvars-write payload")

    out, err, code = _dune_ssh(f"cvars-write {arg}", timeout=60)
    out = (out or "").strip()
    err = (err or "").strip()

    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass

    return {
        "status": "error",
        "exit_code": code,
        "error": (err or out or "cvars-write produced no output")[:1000],
        "stdout": out[:2000],
        "stderr": err[:2000],
    }


@app.get("/server/cvars/history", dependencies=[Depends(verify_key)])
def server_cvars_history(limit: int = 50, offset: int = 0):
    """Paginated holadmin.cvar_changes read. Clamped to limit∈[1..200],
    offset∈[0..100000]. Returns `{rows: [...], total: int, limit, offset}`.

    Uses _dune_ssh (not _dune_ssh_json) so a SQL-level failure (e.g. the
    holadmin schema not yet migrated) surfaces the helper's JSON error
    envelope to the admin-backend instead of a generic 502."""
    if not isinstance(limit, int) or limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    if not isinstance(offset, int) or offset < 0:
        offset = 0
    if offset > 100000:
        offset = 100000
    out, err, code = _dune_ssh(f"cvars-history {limit} {offset}", timeout=30)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "status": "error",
        "exit_code": code,
        "error": (err or out or "cvars-history produced no output")[:1000],
        "rows": [], "total": 0,
        "limit": limit, "offset": offset,
    }


@app.get("/server/cvars/diff", dependencies=[Depends(verify_key)])
def server_cvars_diff(since: str | None = None):
    """settingsUpdate capture diff. `since` is an optional compact UTC stamp
    (e.g. `20260526T091342Z`) — only captures older than this anchor become
    the `older` side. The dispatcher rejects anything outside [0-9TZ]+."""
    if since is not None and not re.match(r"^[0-9TZ]+$", since):
        raise HTTPException(400, "since must be a compact UTC stamp like 20260526T091342Z")
    cmd = "cvars-diff"
    if since:
        cmd = f"cvars-diff {since}"
    out, err, code = _dune_ssh(cmd, timeout=30)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    # Non-JSON output (only on dispatcher-level rejection or SSH transport error).
    return {
        "status": "error",
        "exit_code": code,
        "error": (err or out or "cvars-diff produced no output")[:1000],
    }


# --- W6 Spice-spawn toggle (VC2 P2). Read lists the 8 dune.spicefield_types  ---
# --- rows; write flips is_spawning_active per type. The admin-backend builds  ---
# --- the full {type_id,new_value,who,change_id} envelope (who + change_id are ---
# --- minted there); the relay only base64-encodes the body it is given and    ---
# --- dispatches it. NEVER restarts game pods/BGD/k3s; toggling off does not    ---
# --- despawn active fields, only suppresses the next spawn-tick.               ---


@app.get("/server/spice/types", dependencies=[Depends(verify_key)])
def server_spice_types():
    """List dune.spicefield_types (8 rows) for the V2 admin Spice sub-card.
    Read-only kubectl exec inside the DB pod via dune-spice-toggle.py --list."""
    try:
        return _dune_ssh_json("spice-types", timeout=30)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "types": []}


@app.post("/server/spice/types/{type_id}/spawning", dependencies=[Depends(verify_key)])
async def server_spice_set_spawning(type_id: str, request: Request):
    """Flip is_spawning_active for one spicefield type. The admin-backend POSTs
    {new_value, change_id} and we wrap to {type_id, new_value, who, change_id};
    `who` comes from the body (the admin-backend router builds it from the
    authenticated admin, NOT the relay). The relay base64-encodes the compact
    JSON; the dispatcher's `spice-toggle` allowlist + the python helper
    re-validate every field."""
    if not type_id.isdigit():
        raise HTTPException(400, "type_id must be a positive integer")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    if not isinstance(body.get("new_value"), bool):
        raise HTTPException(400, "new_value must be a boolean")
    if not isinstance(body.get("change_id"), str) or not body["change_id"].strip():
        raise HTTPException(400, "change_id must be a string")
    if not isinstance(body.get("who"), str) or not body["who"].strip():
        raise HTTPException(400, "who must be a string")

    job = {
        "type_id": int(type_id),
        "new_value": body["new_value"],
        "who": body["who"],
        "change_id": body["change_id"],
    }
    payload = json.dumps(job, separators=(",", ":"), sort_keys=True)
    arg = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    if not DUNE_GRANT_B64_RE.match(arg):
        raise HTTPException(500, "failed to encode spice-toggle payload")

    out, err, code = _dune_ssh(f"spice-toggle {arg}", timeout=45)
    out = (out or "").strip()
    err = (err or "").strip()
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                parsed.setdefault("exit_code", code)
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "ok": False,
        "exit_code": code,
        "error": (err or out or "spice-toggle produced no output")[:1000],
    }


# --- VC2 P1 Server Monitor — 5 RMQ helper proxies + 3 telemetry proxies ---
# All read-only, all wrap dispatcher tokens via _dune_ssh_json. Each route
# returns the helper's JSON envelope on success and a graceful fallback
# envelope on failure (matching the helper's shape). The aggregator on
# admin-backend treats both as a successful gather() result; per-source
# isolation lives there.


@app.get("/dune/rmq/last-funcom-push", dependencies=[Depends(verify_key)])
def rmq_last_funcom_push():
    """Newest settingsUpdate capture: ts, sha256, today's push count.
    Wraps dispatcher token `rmq-last-funcom-push`."""
    try:
        return _dune_ssh_json("rmq-last-funcom-push", timeout=5)
    except HTTPException as e:
        return {"available": False, "error": e.detail}


@app.get("/dune/rmq/bgd-rpc-recent", dependencies=[Depends(verify_key)])
def rmq_bgd_rpc_recent():
    """Recent BGD RPC calls in the captures dir: last_ts, count_1h, count_today.
    Wraps dispatcher token `rmq-bgd-rpc-recent`."""
    try:
        return _dune_ssh_json("rmq-bgd-rpc-recent", timeout=5)
    except HTTPException as e:
        return {"available": False, "error": e.detail,
                "count_1h": 0, "count_today": 0, "recent": []}


@app.get("/dune/rmq/partition-counts", dependencies=[Depends(verify_key)])
def rmq_partition_counts():
    """Per-partition player-presence counts from the response/ capture dir.
    Wraps dispatcher token `rmq-partition-counts`."""
    try:
        return _dune_ssh_json("rmq-partition-counts", timeout=5)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "partitions": {}}


@app.get("/dune/rmq/completions-recent", dependencies=[Depends(verify_key)])
def rmq_completions_recent(limit: int = 50):
    """Recent map-lifecycle events (validation.* / completion.* / server_state.*).
    Wraps dispatcher token `rmq-completions-recent`. Limit clamped 1..200."""
    if not isinstance(limit, int) or limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    try:
        return _dune_ssh_json(f"rmq-completions-recent {limit}", timeout=8)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "recent": []}


@app.get("/dune/rmq/travel-queue", dependencies=[Depends(verify_key)])
def rmq_travel_queue():
    """Current travel-queue depth + per-destination breakdown from travelQueueStatus/.
    Wraps dispatcher token `rmq-travel-queue`."""
    try:
        return _dune_ssh_json("rmq-travel-queue", timeout=5)
    except HTTPException as e:
        return {"available": False, "error": e.detail,
                "depth": 0, "by_destination": {}}


# Telemetry proxies — direct passthrough to http://127.0.0.1:8078 on <game-host>
# via the curl-based dispatcher tokens. Same shape as RMQ helpers but the
# helper is `curl ...` not a python script.


# Window regex re-used so each /dune/telemetry/world request can't smuggle
# arbitrary text into the dispatcher arg slot. Mirrors the dispatcher arm.
_TELEM_WINDOW_RE = re.compile(r"^[0-9]{1,3}[hdwHDW]$")


@app.get("/dune/telemetry/events", dependencies=[Depends(verify_key)])
def telemetry_events(limit: int = 50, type: int | None = None,
                     partition: int | None = None):
    """Recent telemetry events (combat / etc.). Wraps dispatcher token
    `telemetry-events`. Limit clamped 1..9999 to match dispatcher regex.
    Optional type/partition (0..999) narrow the read (Amtal ledger evidence)."""
    if not isinstance(limit, int) or limit < 1:
        limit = 1
    if limit > 9999:
        limit = 9999
    for name, val in (("type", type), ("partition", partition)):
        if val is not None and not 0 <= val <= 999:
            raise HTTPException(400, f"{name} must be 0..999")
    filters = ""
    if type is not None:
        filters += f" --type {type}"
    if partition is not None:
        filters += f" --partition {partition}"
    try:
        return _dune_ssh_json(f"telemetry-events {limit}{filters}", timeout=5)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "events": []}


@app.get("/dune/telemetry/transfers", dependencies=[Depends(verify_key)])
def telemetry_transfers(limit: int = 50):
    """Recent character transfers. Wraps dispatcher token `telemetry-transfers`."""
    if not isinstance(limit, int) or limit < 1:
        limit = 1
    if limit > 9999:
        limit = 9999
    try:
        return _dune_ssh_json(f"telemetry-transfers {limit}", timeout=5)
    except HTTPException as e:
        return {"available": False, "error": e.detail, "transfers": []}


@app.get("/dune/telemetry/world", dependencies=[Depends(verify_key)])
def telemetry_world(window: str = "24h"):
    """World counters snapshot. Wraps dispatcher token `telemetry-world`.
    Window must match `^[0-9]{1,3}[hdwHDW]$` (e.g. `24h`, `7d`, `30d`)."""
    if not _TELEM_WINDOW_RE.match(window):
        raise HTTPException(400, "window must be like 24h, 7d, 30d")
    try:
        return _dune_ssh_json(f"telemetry-world {window}", timeout=5)
    except HTTPException as e:
        return {"available": False, "error": e.detail,
                "subfiefs": 0, "structures": 0, "vehicles": 0,
                "snapshots": []}


# VC4 — weekly leaderboards. board is baked into the dispatcher token name;
# week mirrors the telemetry-api `week` query (current | YYYY-Www).
_TELEM_BOARDS = ("pvp", "deaths", "pilots")
_TELEM_WEEK_RE = re.compile(r"^(current|[0-9]{4}-W[0-9]{2})$")


@app.get("/dune/telemetry/leaderboard/{board}", dependencies=[Depends(verify_key)])
def telemetry_leaderboard(board: str, week: str = "current"):
    """Weekly leaderboard for the Monitor card. Wraps dispatcher token
    `telemetry-leaderboard-{board}`. board in pvp|deaths|pilots; week is
    `current` or an ISO week like `2026-W23`. Returns {week, leaderboard:[...]}."""
    if board not in _TELEM_BOARDS:
        raise HTTPException(400, "board must be pvp, deaths or pilots")
    if not _TELEM_WEEK_RE.match(week):
        raise HTTPException(400, "week must be `current` or like 2026-W23")
    try:
        return _dune_ssh_json(f"telemetry-leaderboard-{board} {week}", timeout=5)
    except HTTPException as e:
        return {"available": False, "error": e.detail,
                "week": week, "leaderboard": []}


@app.get("/dune/telemetry/amtal-pvp", dependencies=[Depends(verify_key)])
def telemetry_amtal_pvp():
    """Amtal PvP board + kill streaks. Wraps dispatcher token `telemetry-amtal-pvp`.
    Returns the telemetry payload as-is, account ids included; the public portal
    route strips them."""
    try:
        return _dune_ssh_json("telemetry-amtal-pvp", timeout=5)
    except HTTPException as e:
        return {"available": False, "error": e.detail}


# --- Game-host feature flags (admin wave 2) -----------------------------------
#
# The files under /etc/lastsietch are what the game-side WRITERS read, so these two
# routes are the switch that actually darks or lights a live player feature. The
# name and the state are validated HERE, before any SSH argument is built: the
# dispatcher and /root/dune-flag-set.sh each re-validate, but a bad value must
# never become part of a remote command line in the first place.
#
# servercmd-enabled is in the allowlist on purpose. Turning it off darks the
# admin panel's own live actions rather than a player feature; ssh remains the
# way back in.

DUNE_FLAG_NAMES = (
    "karum-enabled",
    "karum-wtb-enabled",
    "augment-enabled",
    "reward-enabled",
    "servercmd-enabled",
    "market-sell-container-enabled",
    "storage-pawn-move-enabled",
    "market-sell-backpack-enabled",
    "item-transfer-enabled",
    "landsraad-claim-enabled",
    "refinery-enabled",
)


class DuneFlagSetRequest(BaseModel):
    state: str


@app.get("/dune/flags", dependencies=[Depends(verify_key)])
def dune_flags():
    """READ-ONLY: the on/off state of every allowlisted game-host kill switch,
    from a fresh `test -f` on the box. Safe under a change freeze."""
    return _dune_ssh_json("flag-list", timeout=30)


@app.post("/dune/flags/{name}", dependencies=[Depends(verify_key)])
def dune_flag_set(name: str, req: DuneFlagSetRequest):
    """WRITE: create or remove ONE kill-switch file under /etc/lastsietch.

    _claim_op, not _dune_ssh_json: the helper exits non-zero for a refusal and
    for a write that did not verify, and both carry structured JSON the caller
    needs to read. Flattening those into a 502 detail string is how "this flag is
    not in the allowlist" reached the panel as a server error.

    Nothing here restarts a game pod, the BattleGroup Director or k3s."""
    if name not in DUNE_FLAG_NAMES:
        raise HTTPException(400, "unknown flag")
    state = (req.state or "").strip().lower()
    if state not in ("on", "off"):
        raise HTTPException(400, "state must be on or off")
    return _claim_op(f"flag-set {name}:{state}", timeout=30)
