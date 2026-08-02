"""Market price-alert DMs for the Last Sietch portal.

A player sets a price watch on lastsietch.com/portal (the CHOAM Exchange market
page). The admin backend's watcher fires an alert row when the cheapest listing
crosses the player's target. This cog polls the admin backend's internal endpoint
on localhost, DMs the player, and acks delivered alerts so they are not re-sent.

Read-only on the exchange: this only delivers notifications. Buying is in-game.

Config (cielago.config.settings):
  CIELAGO_ADMIN_BASE_URL     admin backend base (default http://127.0.0.1:8078)
  CIELAGO_ALERT_POLL_KEY     shared key for /_internal/market-alerts (X-Alert-Key)
  CIELAGO_MARKET_ALERT_INTERVAL  poll cadence in seconds (default 120)

Disabled (loop never starts) when the poll key is unset.
"""

from __future__ import annotations

import discord
import httpx
import structlog
from discord.ext import commands, tasks

from cielago.config import settings

log = structlog.get_logger()

DUNE_COLOR = 0xD4A574  # desert tan, matches the other Dune embeds
SITE = "https://lastsietch.com"
MARKET_URL = f"{SITE}/portal/market"
_PENDING_LIMIT = 50


def _build_dm(alert: dict) -> discord.Embed:
    """A single price-alert DM embed."""
    name = alert.get("name") or alert.get("template_id") or "an item"
    match = alert.get("match_price")
    threshold = alert.get("threshold_price")
    embed = discord.Embed(
        title="🔔 Price Alert",
        description=(
            f"**{name}** is on the CHOAM Exchange for "
            f"**{match:,}** Solari or less."
        ),
        color=DUNE_COLOR,
        url=MARKET_URL,
    )
    if threshold is not None:
        embed.add_field(name="Your target", value=f"≤ {threshold:,} S", inline=True)
    if match is not None:
        embed.add_field(name="Cheapest now", value=f"{match:,} S", inline=True)
    embed.add_field(
        name="Grab it",
        value=f"Browse on the [portal]({MARKET_URL}), then buy at an in-game terminal.",
        inline=False,
    )
    embed.set_author(name="Last Sietch", url=f"{SITE}/dune/")
    embed.set_footer(text="Manage your watches on the portal · Price Alerts")
    return embed


class MarketAlerts(commands.Cog):
    """Polls the admin backend for fired price alerts and DMs the watcher."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if not settings.cielago_alert_poll_key:
            log.warning("market_alerts.disabled_no_poll_key")
            return
        self._poll.change_interval(seconds=max(30, settings.cielago_market_alert_interval))
        self._poll.start()

    async def cog_unload(self) -> None:
        self._poll.cancel()

    # --- backend calls ---

    def _base(self) -> str:
        return settings.cielago_admin_base_url.rstrip("/")

    async def _fetch_pending(self, client: httpx.AsyncClient) -> list[dict]:
        resp = await client.get(
            f"{self._base()}/_internal/market-alerts/pending",
            params={"limit": _PENDING_LIMIT},
            headers={"X-Alert-Key": settings.cielago_alert_poll_key},
        )
        resp.raise_for_status()
        return resp.json().get("alerts", [])

    async def _ack(self, client: httpx.AsyncClient, ids: list[int], note: str) -> None:
        if not ids:
            return
        try:
            resp = await client.post(
                f"{self._base()}/_internal/market-alerts/ack",
                json={"ids": ids, "note": note},
                headers={"X-Alert-Key": settings.cielago_alert_poll_key},
            )
            resp.raise_for_status()
        except Exception:
            # Leave un-acked on failure; next tick retries. A duplicate DM is far
            # worse to suppress than to risk, but ack failures are rare + transient.
            log.warning("market_alerts.ack_failed", count=len(ids), note=note, exc_info=True)

    async def _deliver(self, client: httpx.AsyncClient, alert: dict) -> tuple[int, str] | None:
        """DM one alert. Returns (id, note) when it should be acked (delivered or
        permanently undeliverable), or None to retry next tick (transient)."""
        aid = alert.get("id")
        did = alert.get("discord_id")
        if aid is None:
            return None
        try:
            user = await self.bot.fetch_user(int(did))
        except (discord.NotFound, ValueError):
            return (aid, "user_not_found")
        except discord.HTTPException:
            return None  # transient; retry
        try:
            await user.send(embed=_build_dm(alert))
            return (aid, "delivered")
        except discord.Forbidden:
            # DMs closed / bot blocked — the portal bell still covers them.
            return (aid, "dms_closed")
        except discord.HTTPException:
            return None  # transient; retry next tick

    # --- scheduled loop ---

    @tasks.loop(seconds=120)
    async def _poll(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    pending = await self._fetch_pending(client)
                except Exception:
                    log.warning("market_alerts.fetch_failed", exc_info=True)
                    return
                if not pending:
                    return
                delivered, closed, missing = [], [], []
                for alert in pending:
                    result = await self._deliver(client, alert)
                    if result is None:
                        continue
                    aid, note = result
                    if note == "delivered":
                        delivered.append(aid)
                    elif note == "dms_closed":
                        closed.append(aid)
                    else:
                        missing.append(aid)
                await self._ack(client, delivered, "delivered")
                await self._ack(client, closed, "dms_closed")
                await self._ack(client, missing, "user_not_found")
                log.info("market_alerts.tick", delivered=len(delivered),
                         dms_closed=len(closed), user_not_found=len(missing))
        except Exception:
            log.warning("market_alerts.tick_failed", exc_info=True)

    @_poll.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MarketAlerts(bot))
