"""Landsraad claim receipt DMs for the Last Sietch portal.

A player claims their Landsraad house rewards on the portal while offline. The admin
backend stores the receipt and exposes it on /_internal/landsraad-claims/pending once
that player is online again. This cog DMs the receipt and acks it, so it is sent once.

Same transport and key as market_alerts (CIELAGO_ADMIN_BASE_URL, CIELAGO_ALERT_POLL_KEY,
CIELAGO_MARKET_ALERT_INTERVAL). Disabled (loop never starts) when the key is unset.
House style for player-facing copy: no em dashes.
"""

from __future__ import annotations

import discord
import httpx
import structlog
from discord.ext import commands, tasks

from cielago.config import settings

log = structlog.get_logger()

DUNE_COLOR = 0xD4A574
SITE = "https://lastsietch.com"
LANDSRAAD_URL = f"{SITE}/portal/v2/landsraad"
_PENDING_LIMIT = 50
_FIELD_MAX = 1000          # Discord caps a field value at 1024
_SECTION_FIELDS = 4        # a long section continues over this many fields (embed total stays under 6000)


def _chunks(rows: list[dict], fields: int = _SECTION_FIELDS) -> list[str]:
    """Item lines packed into field values; a big claim (a whole term of rewards is
    40+ lines) spills into continuation fields, and only past `fields` of them
    does the last one end in '...and N more'."""
    lines = [f"{r.get('name', 'Item')} x{int(r.get('qty') or 0):,}" for r in rows]
    out, cur, used = [], [], 0
    for i, line in enumerate(lines):
        last = len(out) == fields - 1
        if cur and used + len(line) + 1 > _FIELD_MAX - (20 if last else 0):
            if last:
                cur.append(f"...and {len(lines) - i} more")
                break
            out.append("\n".join(cur))
            cur, used = [], 0
        cur.append(line)
        used += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out or ["None"]


def _add_section(embed, name: str, rows: list[dict], tail: str = "") -> None:
    values = _chunks(rows)
    for i, value in enumerate(values):
        if tail and i == len(values) - 1 and len(value) + len(tail) <= 1024:
            value += tail
        embed.add_field(name=name if i == 0 else f"{name} (continued)", value=value, inline=False)


def _build_dm(receipt: dict) -> discord.Embed:
    embed = discord.Embed(
        title="Landsraad rewards delivered",
        description="Welcome back. Here is what your portal claim delivered while you were away.",
        color=DUNE_COLOR,
        url=LANDSRAAD_URL,
    )
    backpack = list(receipt.get("backpack") or [])
    if receipt.get("solari"):
        backpack.insert(0, {"name": "Solari", "qty": receipt["solari"]})
    if backpack:
        _add_section(embed, "In your backpack", backpack)
    if receipt.get("exchange"):
        _add_section(embed, "Waiting at the CHOAM Exchange", receipt["exchange"],
                     "\n\nOpen any exchange terminal, go to Completed and press Take item. "
                     "They show as CANCELED, which is expected.")
    if receipt.get("unlocked"):
        _add_section(embed, "Swatches unlocked", receipt["unlocked"])
    if receipt.get("at_rep"):
        _add_section(embed, "Still at the house representative", receipt["at_rep"])
    embed.set_author(name="Last Sietch", url=f"{SITE}/dune/")
    embed.set_footer(text="Landsraad portal claim")
    return embed


class LandsraadClaims(commands.Cog):
    """Polls the admin backend for claim receipts of returning players and DMs them."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if not settings.cielago_alert_poll_key:
            log.warning("landsraad_claims.disabled_no_poll_key")
            return
        self._poll.change_interval(seconds=max(30, settings.cielago_market_alert_interval))
        self._poll.start()

    async def cog_unload(self) -> None:
        self._poll.cancel()

    def _base(self) -> str:
        return settings.cielago_admin_base_url.rstrip("/")

    async def _ack(self, client: httpx.AsyncClient, ids: list[int], note: str) -> None:
        if not ids:
            return
        try:
            resp = await client.post(
                f"{self._base()}/_internal/landsraad-claims/ack",
                json={"ids": ids, "note": note},
                headers={"X-Alert-Key": settings.cielago_alert_poll_key},
            )
            resp.raise_for_status()
        except Exception:
            log.warning("landsraad_claims.ack_failed", count=len(ids), note=note, exc_info=True)

    async def _deliver(self, claim: dict) -> tuple[int, str] | None:
        """(id, note) to ack, or None to retry next tick (transient)."""
        cid = claim.get("id")
        if cid is None:
            return None
        try:
            user = await self.bot.fetch_user(int(claim.get("discord_id")))
        except (discord.NotFound, ValueError, TypeError):
            return (cid, "user_not_found")
        except discord.HTTPException:
            return None
        try:
            await user.send(embed=_build_dm(claim.get("receipt") or {}))
            return (cid, "delivered")
        except discord.Forbidden:
            return (cid, "dms_closed")   # the portal receipt already told them
        except discord.HTTPException:
            return None

    @tasks.loop(seconds=120)
    async def _poll(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    resp = await client.get(
                        f"{self._base()}/_internal/landsraad-claims/pending",
                        params={"limit": _PENDING_LIMIT},
                        headers={"X-Alert-Key": settings.cielago_alert_poll_key},
                    )
                    resp.raise_for_status()
                    pending = resp.json().get("claims", [])
                except Exception:
                    log.warning("landsraad_claims.fetch_failed", exc_info=True)
                    return
                by_note: dict[str, list[int]] = {}
                for claim in pending:
                    result = await self._deliver(claim)
                    if result is not None:
                        by_note.setdefault(result[1], []).append(result[0])
                for note, ids in by_note.items():
                    await self._ack(client, ids, note)
                if pending:
                    log.info("landsraad_claims.tick", **{k: len(v) for k, v in by_note.items()})
        except Exception:
            log.warning("landsraad_claims.tick_failed", exc_info=True)

    @_poll.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LandsraadClaims(bot))
