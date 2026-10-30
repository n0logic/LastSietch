"""Portal events -> Discord scheduled events for the Last Sietch guild.

The admin backend decides what Discord should show (/_internal/events/discord/pending:
one desired status per event, derived from the event and the clock). This cog makes the
guild match and acks each result with the event's `rev`, so an edit made while it was
syncing is picked up on the next poll. Plan: the design notes (not published)
WEEKEND-EVENTS-PLAN-2026-10-23.md (stage 2).

- EXTERNAL events: location is the map or "The Last Sietch".
- The portal link on the last line of the description is the adoption marker: before
  creating, an existing guild event carrying it is adopted, so a crash between create
  and ack never duplicates.
- Discord only moves SCHEDULED -> ACTIVE -> COMPLETED, or SCHEDULED -> CANCELED; the
  steps are walked one at a time. An active event cannot be cancelled, so it is ended.
- Scheduled events never mention anyone.

Same transport and key as market_alerts. Off unless CIELAGO_EVENTS_SYNC_ENABLED and
the poll key are set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord
import httpx
import structlog
from discord.ext import commands, tasks

from cielago.config import settings

log = structlog.get_logger()

_PENDING_LIMIT = 25
_STATUS = {
    discord.EventStatus.scheduled: "scheduled",
    discord.EventStatus.active: "active",
    discord.EventStatus.completed: "completed",
    discord.EventStatus.canceled: "canceled",
}
FINAL = ("completed", "canceled")


def status_steps(current: str, desired: str) -> list[str]:
    """The Discord calls ('start', 'end', 'cancel') that take an event from
    `current` to `desired`, in order. Empty when nothing can or needs to move."""
    if current == desired or current in FINAL:
        return []
    if desired == "active":
        return ["start"] if current == "scheduled" else []
    if desired == "completed":
        return ["start", "end"] if current == "scheduled" else ["end"]
    if desired == "canceled":
        return ["cancel"] if current == "scheduled" else ["end"]
    return []


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def edit_kwargs(item: dict, event, status: str) -> dict:
    """Fields that differ from what Discord shows. The start time can only move
    while the event is still scheduled."""
    want = {
        "name": item["name"],
        "description": item["description"],
        "location": item["location"],
        "end_time": parse_time(item["ends_utc"]),
    }
    if status == "scheduled":
        want["start_time"] = parse_time(item["starts_utc"])
    have = {
        "name": event.name,
        "description": event.description,
        "location": event.location,
        "end_time": event.end_time,
        "start_time": event.start_time,
    }
    return {k: v for k, v in want.items() if have.get(k) != v}


class EventsSync(commands.Cog):
    """Polls the admin backend for portal events whose Discord side needs work."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if not (settings.cielago_events_sync_enabled and settings.cielago_alert_poll_key):
            log.info("events_sync.disabled")
            return
        self._poll.change_interval(seconds=max(30, settings.cielago_events_sync_interval))
        self._poll.start()

    async def cog_unload(self) -> None:
        self._poll.cancel()

    def _base(self) -> str:
        return settings.cielago_admin_base_url.rstrip("/")

    def _headers(self) -> dict:
        return {"X-Alert-Key": settings.cielago_alert_poll_key}

    async def _banner(self, client: httpx.AsyncClient, slug: str | None) -> bytes | None:
        if not slug:
            return None
        try:
            resp = await client.get(f"{self._base()}/_internal/events/discord/banner/{slug}",
                                    headers=self._headers())
            resp.raise_for_status()
            return resp.content
        except Exception:
            log.warning("events_sync.banner_failed", slug=slug, exc_info=True)
            return None

    async def _find(self, guild: discord.Guild, item: dict):
        """The Discord event for this portal event: by stored id, else by marker."""
        if item.get("discord_event_id"):
            try:
                return await guild.fetch_scheduled_event(int(item["discord_event_id"]))
            except discord.NotFound:
                pass
        for event in await guild.fetch_scheduled_events():
            if item["marker"] in (event.description or ""):
                return event
        return None

    async def _sync(self, client: httpx.AsyncClient, guild: discord.Guild, item: dict) -> dict:
        result = {"id": item["id"], "rev": item["rev"]}
        desired = item["desired"]
        event = await self._find(guild, item)

        if event is None:
            if desired != "scheduled":
                # Nothing on Discord and nothing to create: record the end state.
                return {**result, "ok": True, "discord_event_id": None,
                        "discord_status": "canceled" if desired == "canceled" else desired}
            start = parse_time(item["starts_utc"])
            if start <= datetime.now(UTC) + timedelta(seconds=30):
                return {**result, "ok": False, "error": "start time already passed"}
            kwargs = {
                "name": item["name"],
                "description": item["description"],
                "start_time": start,
                "end_time": parse_time(item["ends_utc"]),
                "entity_type": discord.EntityType.external,
                "privacy_level": discord.PrivacyLevel.guild_only,
                "location": item["location"],
                "reason": f"portal event {item['id']}",
            }
            image = await self._banner(client, item.get("banner"))
            if image:
                kwargs["image"] = image
            event = await guild.create_scheduled_event(**kwargs)
            log.info("events_sync.created", event_id=item["id"], discord_event_id=event.id)

        status = _STATUS.get(event.status, "scheduled")
        if status not in FINAL and desired != "canceled":
            changes = edit_kwargs(item, event, status)
            if changes:
                event = await event.edit(**changes, reason=f"portal event {item['id']}")
        for step in status_steps(status, desired):
            event = await getattr(event, step)(reason=f"portal event {item['id']}")
        return {**result, "ok": True, "discord_event_id": str(event.id),
                "discord_status": _STATUS.get(event.status, status)}

    @tasks.loop(seconds=60)
    async def _poll(self) -> None:
        guild = self.bot.get_guild(settings.last_sietch_guild_id)
        if guild is None:
            return
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    resp = await client.get(
                        f"{self._base()}/_internal/events/discord/pending",
                        params={"limit": _PENDING_LIMIT},
                        headers=self._headers(),
                    )
                    resp.raise_for_status()
                    pending = resp.json().get("events", [])
                except Exception:
                    log.warning("events_sync.fetch_failed", exc_info=True)
                    return
                results = []
                for item in pending:
                    try:
                        results.append(await self._sync(client, guild, item))
                    except discord.HTTPException as exc:
                        log.warning("events_sync.discord_error",
                                    event_id=item.get("id"), status=exc.status)
                        results.append({"id": item.get("id"), "rev": item.get("rev"), "ok": False,
                                        "error": f"Discord {exc.status}: {exc.text}"[:300]})
                    except Exception as exc:  # noqa: BLE001
                        log.warning("events_sync.failed", event_id=item.get("id"), exc_info=True)
                        results.append({"id": item.get("id"), "rev": item.get("rev"), "ok": False,
                                        "error": f"{type(exc).__name__}: {exc}"[:300]})
                if results:
                    try:
                        resp = await client.post(f"{self._base()}/_internal/events/discord/ack",
                                                 json={"results": results}, headers=self._headers())
                        resp.raise_for_status()
                    except Exception:
                        log.warning("events_sync.ack_failed", count=len(results), exc_info=True)
        except Exception:
            log.exception("events_sync.poll_crashed")

    @_poll.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventsSync(bot))
