"""Steam Update Orchestrator.
Runs the multi-phase update sequence (snapshot, backup, stop, steamcmd, verify, start, snapshot, announce).
Per-game asyncio.Lock prevents concurrent updates. Job state lives in-memory for status polling.
"""
import asyncio
import os
import time
import uuid
from datetime import datetime, timezone

import discord_post
from auth import audit_log
from config import DISCORD_CH_BOTLOGS
from relay import call_relay


# Per-game lock + active job tracker. Process-local; admin-backend is single-process.
_game_locks: dict[str, asyncio.Lock] = {}
_active_jobs: dict[str, dict] = {}


def _get_lock(game_id: str) -> asyncio.Lock:
    if game_id not in _game_locks:
        _game_locks[game_id] = asyncio.Lock()
    return _game_locks[game_id]


def get_active_job(game_id: str) -> dict | None:
    return _active_jobs.get(game_id)


def is_running(game_id: str) -> bool:
    job = _active_jobs.get(game_id)
    return bool(job and job.get("status") == "running")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _phase(job: dict, name: str, fn):
    """Run a phase coroutine; record start/end + result. Re-raises on failure after marking failed."""
    phase = {"name": name, "started_at": _utc_now_iso(), "status": "running"}
    job["phases"].append(phase)
    job["current_phase"] = name
    try:
        result = await fn()
        phase["status"] = "ok"
        phase["finished_at"] = _utc_now_iso()
        # Only store small results; SteamCMD result has tail strings, keep them.
        if isinstance(result, (dict, list, str, int, float, bool)) or result is None:
            phase["result"] = result
        return result
    except Exception as e:
        phase["status"] = "failed"
        phase["finished_at"] = _utc_now_iso()
        phase["error"] = str(e)[:500]
        raise


async def _post_botlogs(content: str):
    if DISCORD_CH_BOTLOGS:
        await discord_post.post_to_channel(DISCORD_CH_BOTLOGS, content)


async def _post_game_channel(channel_env: str, content: str):
    chan = os.environ.get(channel_env, "") if channel_env else ""
    if chan:
        await discord_post.post_to_channel(chan, content)


async def _wait_stopped(game_id: str, timeout_sec: int = 30):
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            status = await call_relay(f"/games/{game_id}/server/status")
            if not status.get("running"):
                return {"stopped_after_seconds": round(time.time() - start, 1)}
        except Exception:
            pass
        await asyncio.sleep(1.5)
    raise RuntimeError(f"Server did not stop within {timeout_sec}s")


async def _wait_ready(game_id: str, timeout_sec: int = 180):
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            status = await call_relay(f"/games/{game_id}/server/status")
            if status.get("running") and status.get("pid"):
                return {"ready_after_seconds": round(time.time() - start, 1)}
        except Exception:
            pass
        await asyncio.sleep(2.0)
    raise RuntimeError(f"Server did not become ready within {timeout_sec}s")


async def _verify_or_restore_config(game_id: str, config_files: list, expected_hashes: dict, buildid: str):
    """Compare current SHA256 to backup hashes. If drift, restore from .pre-<buildid>-bak and re-verify."""
    res = await call_relay(
        f"/games/{game_id}/server/file-hash",
        "POST",
        {"paths": config_files},
    )
    current = {f["path"]: f.get("sha256", "") for f in res.get("files", [])}
    drift = [p for p in config_files if current.get(p, "") != expected_hashes.get(p, "")]
    if not drift:
        return {"drift_detected": False, "restored": [], "final_hashes": current}
    # Drift detected — restore from backup
    restored = await call_relay(
        f"/games/{game_id}/server/restore-config",
        "POST",
        {"buildid": buildid},
    )
    after = {f["path"]: f.get("sha256", "") for f in restored.get("files", [])}
    still_drifting = [p for p in config_files if after.get(p, "") != expected_hashes.get(p, "")]
    if still_drifting:
        raise RuntimeError(f"Config restore failed for: {still_drifting}")
    return {"drift_detected": True, "restored": drift, "final_hashes": after}


async def _run_update(game_id: str, game: dict, user: dict, ip: str, job: dict):
    """Inner orchestration. Caller holds the lock."""
    update_cfg = game["update"]
    app_id = update_cfg["steam_app_id"]
    config_files = update_cfg.get("config_files_full") or []
    snapshot_prefix = update_cfg.get("snapshot_prefix", "auto-update")
    channel_env = update_cfg.get("discord_announce_channel_env")
    ready_timeout = int(update_cfg.get("ready_check_timeout_seconds", 180))
    display_name = job["display_name"]

    # Use UTC timestamp as the buildid suffix for snapshots/.bak files (we don't know the
    # current buildid pre-update without an extra call; ts is unique + monotonic).
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    pre_snap_suffix = f"{snapshot_prefix}-pre-{app_id}-{ts}"

    server_was_stopped = False

    try:
        # 1. Discord pre-announce (best-effort, never blocks the update if it fails)
        async def pre_announce():
            await _post_botlogs(
                f"`[Update]` **{display_name}** update starting (initiated by `{user['username']}`)."
            )
            if channel_env:
                await _post_game_channel(
                    channel_env,
                    f"**{display_name}** server is going down for an update. We'll announce when it's back.",
                )
        await _phase(job, "discord-pre-announce", pre_announce)

        # 2. Pre snapshot (ZFS, named auto-update-pre-<appid>-<ts>)
        await _phase(job, "snapshot-pre", lambda: call_relay(
            f"/games/{game_id}/zfs/snapshot",
            "POST",
            {"name_suffix": pre_snap_suffix},
        ))

        # 3. Backup config files (creates .pre-<ts>-bak on the VM, returns SHA256 of originals)
        backup_result = await _phase(job, "backup-config", lambda: call_relay(
            f"/games/{game_id}/server/backup-config",
            "POST",
            {"buildid": ts},
        ))
        expected_hashes = {
            f["path"]: f.get("sha256", "")
            for f in backup_result.get("files", [])
            if f.get("ok")
        }

        # 4. Stop server
        await _phase(job, "stop", lambda: call_relay(f"/games/{game_id}/server/stop", "POST"))
        server_was_stopped = True
        await _phase(job, "wait-stopped", lambda: _wait_stopped(game_id, 30))

        # 5. SteamCMD update (long-running, can take 30 min for big payloads)
        steamcmd_result = await _phase(job, "steamcmd", lambda: call_relay(
            f"/games/{game_id}/server/update-steamcmd",
            "POST",
            timeout=2000.0,  # admin->relay HTTP timeout; must exceed relay's 1860s
        ))
        new_buildid = steamcmd_result.get("buildid") or ""
        steamcmd_exit = steamcmd_result.get("exit_code")
        # Trust the appmanifest buildid as the "did it work" canary, not the steamcmd exit code
        # (steamcmd has known weird exit codes 6/7 that still represent success).
        if not new_buildid:
            tail = (steamcmd_result.get("stdout_tail") or "")[-300:]
            raise RuntimeError(f"SteamCMD did not produce a buildid (exit={steamcmd_exit}). Tail: {tail}")

        # 5b. Refresh Steam Workshop mods (separate appid/install dir from the build).
        # Skipped automatically for games with no workshop_mod_ids configured. A failed
        # mod download leaves the prior copy in place but would mismatch updated clients,
        # so a hard failure aborts the update (snapshot-pre covers rollback).
        mods_result = await _phase(job, "workshop-mods", lambda: call_relay(
            f"/games/{game_id}/server/update-workshop-mods",
            "POST",
            timeout=2000.0,  # admin->relay HTTP timeout; must exceed relay's 1860s
        ))
        if mods_result.get("failed"):
            raise RuntimeError(
                f"Workshop mod update failed for: {', '.join(mods_result['failed'])} "
                f"(succeeded: {', '.join(mods_result.get('succeeded') or []) or 'none'})"
            )

        # 6. Verify config integrity, restore from backup if SteamCMD touched anything
        if config_files and expected_hashes:
            await _phase(job, "verify-config", lambda: _verify_or_restore_config(
                game_id, config_files, expected_hashes, ts,
            ))

        # 7. Start server + wait until gamedig-ready
        await _phase(job, "start", lambda: call_relay(f"/games/{game_id}/server/start", "POST"))
        await _phase(job, "wait-ready", lambda: _wait_ready(game_id, ready_timeout))
        server_was_stopped = False

        # 8. Post snapshot (ZFS, named auto-update-post-<appid>-<buildid>-success)
        post_snap_suffix = f"{snapshot_prefix}-post-{app_id}-{new_buildid}-success"
        await _phase(job, "snapshot-post", lambda: call_relay(
            f"/games/{game_id}/zfs/snapshot",
            "POST",
            {"name_suffix": post_snap_suffix},
        ))

        # 9. Discord post-announce
        duration_seconds = sum(
            (datetime.fromisoformat(p["finished_at"]) - datetime.fromisoformat(p["started_at"])).total_seconds()
            for p in job["phases"] if p.get("finished_at")
        )
        m, s = divmod(int(duration_seconds), 60)

        async def post_announce():
            await _post_botlogs(
                f"`[Update]` **{display_name}** update complete. Build `{new_buildid}`. Duration {m}m{s:02d}s."
            )
            if channel_env:
                await _post_game_channel(
                    channel_env,
                    f"**{display_name}** server is back online. New build: `{new_buildid}`. You can reconnect now.",
                )
        await _phase(job, "discord-post-announce", post_announce)

        job["status"] = "completed"
        job["finished_at"] = _utc_now_iso()
        job["new_buildid"] = new_buildid
        audit_log(
            user["id"], user["username"], "update_completed", game_id, ip,
            details=f"job_id={job['job_id']} buildid={new_buildid} duration_s={int(duration_seconds)}",
        )

    except Exception as e:
        # Best-effort recovery: if we stopped the server but never started it, try to start it
        # so it's not left dead. We do this BEFORE marking failed/posting Discord so the recovery
        # attempt itself has a chance to succeed.
        if server_was_stopped:
            try:
                await call_relay(f"/games/{game_id}/server/start", "POST")
            except Exception:
                pass

        job["status"] = "failed"
        job["finished_at"] = _utc_now_iso()
        job["error"] = str(e)[:500]
        # Best-effort failure announcement (bot-logs only; players don't see operational noise).
        # Do NOT include the raw exception text — upstream errors (relay HTTPException, SteamCMD
        # output, qm guest exec stderr) can contain sudo prompts, API keys, or other secrets.
        # The full error lives in the admin panel's update status modal + audit log; admins
        # already have access there, no need to re-publish to Discord.
        try:
            await _post_botlogs(
                f"`[Update]` **{display_name}** update FAILED at phase `{job.get('current_phase')}`. "
                f"See admin panel update status for details (initiated by `{user['username']}`)."
            )
        except Exception:
            pass
        audit_log(
            user["id"], user["username"], "update_failed", game_id, ip,
            details=f"job_id={job['job_id']} phase={job.get('current_phase')} error={str(e)[:200]}",
            success=False,
        )
        # Don't re-raise; the orchestrator runs as a background task and the API
        # client polls status to learn the outcome.


async def start_update(game_id: str, game: dict, user: dict, ip: str) -> dict:
    """Start an update in the background. Returns the job dict immediately. Raises if one is already running."""
    if "update" not in game:
        raise ValueError(f"Game {game_id} has no update config")
    lock = _get_lock(game_id)
    if lock.locked():
        existing = _active_jobs.get(game_id, {})
        raise RuntimeError(
            f"Update already running for {game_id} (job_id={existing.get('job_id', 'unknown')})"
        )

    job = {
        "job_id": str(uuid.uuid4()),
        "game_id": game_id,
        "display_name": game.get("display_name", game_id),
        "started_at": _utc_now_iso(),
        "started_by": user["username"],
        "started_by_id": user["id"],
        "status": "running",
        "phases": [],
        "current_phase": "starting",
    }
    _active_jobs[game_id] = job
    audit_log(user["id"], user["username"], "update_started", game_id, ip,
              details=f"job_id={job['job_id']}")

    async def _runner():
        async with lock:
            await _run_update(game_id, game, user, ip, job)

    asyncio.create_task(_runner())
    return job
