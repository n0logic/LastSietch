"""Dune Awakening Steam build watcher for the Last Sietch Discord.

Polls the public Steam buildid of the two Dune appids the community cares about
and posts to the staff #server-logs channel when a buildid actually changes:

  4754530  Dune: Awakening Self-Hosted Server (GA)  -- the appid Last Sietch runs;
                                                        a change means a server patch
                                                        is out (review before pulling).
  1172710  Dune: Awakening Live Client              -- a client patch (TLDR-worthy).

This replaces the Dune half of the personal-Last Sietch "server monitor" bot's build
watcher (the Conan + Enshrouded watchers stay on the Last Sietch box). Cielago owns the
schedule, branding, and posting; the <game-host> pod-watcher reposts pod alerts to
the same channel as Cielago separately.

State (last-seen buildid per appid) is a small JSON file; a first sighting of an
appid seeds silently (no alert), matching the old shell watcher.

Config (cielago.config.settings):
  CIELAGO_SERVER_LOGS_CHANNEL_ID  where alerts post (default ⚙️｜server-logs)
  CIELAGO_BUILDWATCH_ENABLED      master toggle (default true)
  CIELAGO_BUILDWATCH_INTERVAL     poll cadence in seconds (default 900 = 15 min)
  CIELAGO_BUILDWATCH_STATE_PATH   state file (default data/dune-buildwatch.json)

Disabled (loop never starts) when the toggle is off or the channel is unset.
"""

from __future__ import annotations

import json
import os

import httpx
import structlog
from discord.ext import commands, tasks

from cielago.config import settings

log = structlog.get_logger()

STEAM_INFO = "https://api.steamcmd.net/v1/info/{appid}"

# appid -> (friendly label, message kind). Order is the poll order.
APPS: dict[str, tuple[str, str]] = {
    "4754530": ("Dune Awakening Self-Hosted Server (GA)", "server"),
    "1172710": ("Dune Awakening Live Client", "client"),
}


def _extract_buildid(payload: dict, appid: str) -> str | None:
    """Pull the public-branch buildid out of a steamcmd.net info response."""
    try:
        d = payload["data"][appid]
        bid = d["depots"]["branches"]["public"]["buildid"]
        return str(bid) if bid not in (None, "", "none") else None
    except (KeyError, TypeError):
        return None


def _build_message(appid: str, label: str, kind: str, old: str, new: str) -> str:
    steamdb = f"https://steamdb.info/app/{appid}/"
    if kind == "server":
        return (
            f":dna: :rotating_light: **Dune self-host server build changed** "
            f"(`{label}`, appid `{appid}`)\n"
            f"`buildid`: `{old}` -> `{new}`\n"
            "This is the appid the Last Sietch server runs. Review the update "
            "procedure before pulling.\n"
            f"Cross-check: <https://duneawakening.com/news/> and <{steamdb}>"
        )
    return (
        f":mag: **Dune live client patched** (`{label}`, appid `{appid}`)\n"
        f"`buildid`: `{old}` -> `{new}`\n"
        "Funcom usually posts patch notes shortly after deploy; a TLDR in "
        "#dune-general is welcome when they land.\n"
        f"Patch notes: <https://duneawakening.com/news/> · SteamDB: <{steamdb}patchnotes/>"
    )


class DuneBuildWatch(commands.Cog):
    """Polls Steam buildids for the Dune appids and posts changes to #server-logs."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._state: dict[str, str] = {}

    async def cog_load(self) -> None:
        if not settings.cielago_buildwatch_enabled:
            log.warning("dune_buildwatch.disabled_toggle")
            return
        if not settings.cielago_server_logs_channel_id:
            log.warning("dune_buildwatch.disabled_no_channel")
            return
        self._load_state()
        self._poll.change_interval(seconds=max(300, settings.cielago_buildwatch_interval))
        self._poll.start()

    async def cog_unload(self) -> None:
        self._poll.cancel()

    # --- state persistence ---

    def _state_path(self) -> str:
        return settings.cielago_buildwatch_state_path

    def _load_state(self) -> None:
        try:
            with open(self._state_path(), encoding="utf-8") as fh:
                self._state = {str(k): str(v) for k, v in json.load(fh).items()}
        except (FileNotFoundError, ValueError, OSError):
            self._state = {}

    def _save_state(self) -> None:
        path = self._state_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._state, fh)
        os.replace(tmp, path)

    # --- posting ---

    async def _post(self, content: str) -> None:
        cid = settings.cielago_server_logs_channel_id
        channel = self.bot.get_channel(cid)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(cid)
            except Exception:
                log.warning("dune_buildwatch.channel_unavailable", channel_id=cid)
                return
        await channel.send(content)

    # --- scheduled loop ---

    @tasks.loop(seconds=900)
    async def _poll(self) -> None:
        changed = False
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for appid, (label, kind) in APPS.items():
                    try:
                        resp = await client.get(STEAM_INFO.format(appid=appid))
                        resp.raise_for_status()
                        new = _extract_buildid(resp.json(), appid)
                    except Exception:
                        log.warning("dune_buildwatch.query_failed", appid=appid, exc_info=True)
                        continue
                    if new is None:
                        log.warning("dune_buildwatch.no_buildid", appid=appid)
                        continue
                    old = self._state.get(appid)
                    if old is None:
                        # First sighting: seed silently, do not alert.
                        self._state[appid] = new
                        changed = True
                        log.info("dune_buildwatch.seeded", appid=appid, buildid=new)
                        continue
                    if old == new:
                        continue
                    self._state[appid] = new
                    changed = True
                    log.info("dune_buildwatch.change", appid=appid, old=old, new=new)
                    try:
                        await self._post(_build_message(appid, label, kind, old, new))
                    except Exception:
                        # Keep the new buildid saved so we don't re-alert forever on a
                        # transient post failure; the change is already logged.
                        log.warning("dune_buildwatch.post_failed", appid=appid, exc_info=True)
        finally:
            if changed:
                try:
                    self._save_state()
                except Exception:
                    log.warning("dune_buildwatch.state_save_failed", exc_info=True)

    @_poll.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DuneBuildWatch(bot))
