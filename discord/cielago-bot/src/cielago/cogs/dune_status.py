"""Daily + weekly Dune server-status digest for the Last Sietch Discord.

Cielago owns scheduling, branding, and posting. The stats data lives on
lastsietch-dune (telemetry.db + postgres dune.*/holadmin.*); we fetch a structured
JSON blob from the lastsietch-relay endpoint /dune/stats/digest and render the embed
here. This replaces the retired House-0f-Fedaykin digest that posted to the
old Last Sietch Discord.

Schedule (US Eastern, matching the retired systemd timers):
  - daily  digest at 09:00 ET
  - weekly digest Sunday at 18:00 ET

Channel: CIELAGO_DUNE_STATUS_CHANNEL_ID (#dune-server-info).
"""

from __future__ import annotations

import re
import time as _time
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import discord
import httpx
import structlog
from discord import app_commands
from discord.ext import commands, tasks

from cielago.audit import audit
from cielago.config import settings
from cielago.permissions import admin_only

log = structlog.get_logger()

ET = ZoneInfo("America/New_York")
DUNE_COLOR = 0xD4A574  # desert tan, matches the /connect Dune embed
SITE = "https://lastsietch.com"
DAILY_AT = time(hour=9, minute=0, tzinfo=ET)
WEEKLY_AT = time(hour=18, minute=0, tzinfo=ET)
SUNDAY = 6  # datetime.weekday()

# Rotating "deaths" section flavor — advances once per UTC day.
DEATH_VARIANTS = [
    {
        "name": "🪱 Shai-Hulud's Due",
        "line1": lambda name, n: f"Shai-Hulud claimed {name} {'once' if n == 1 else f'{n} times'}",
        "empty": "Shai-Hulud collected no due this period. The desert rests.",
    },
    {
        "name": "🪱 The Maker's Table",
        "line1": lambda name, n: f"{name} fed the Maker {'once' if n == 1 else f'{n} times'}",
        "empty": "The Maker's table sat empty this period.",
    },
    {
        "name": "🏜️ The Road to Muad'Dib",
        "line1": lambda name, n: (
            f"{name} — {'1 trial' if n == 1 else f'{n} trials'} on the road to Muad'Dib"
        ),
        "empty": "No trials this period — the road was kind.",
    },
]


# ---- formatting helpers ---------------------------------------------------

def _et_hour_range(utc_hour: int) -> str:
    """Render a UTC hour as an ET (EDT, UTC-4) one-hour range, e.g. '11 AM - 12 PM ET'."""
    def label(h: int) -> str:
        h %= 24
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12} {suffix}"
    start = (utc_hour - 4) % 24
    return f"{label(start)} - {label(start + 1)} ET"


def _friendly_vehicle(raw: str) -> str:
    """Prettify a vehicle blueprint path or model name."""
    base = (raw or "").lstrip("#").rsplit("/", 1)[-1].removesuffix("_C").rsplit(".", 1)[-1]
    pretty = {
        "BP_LightOrnithopter_Choam": "Light Ornithopter",
        "BP_MediumOrnithopter_CHOAM": "Medium Ornithopter",
        "LightOrnithopterChoam": "light ornithopter",
        "MediumOrnithopterCHOAM": "medium ornithopter",
        "BuggyChoam": "buggy",
    }.get(base)
    if pretty:
        return pretty
    return base.removeprefix("BP_").replace("_", " ").strip() or "vehicle"


def _friendly_placeable(raw: str) -> str:
    """Prettify a raw building/placeable asset name into player-facing words,
    e.g. 'Totem_Small_Placeable' -> 'small totem'. Tokens read modifier-first,
    which is how these descriptors scan in English (small totem, lesser hut)."""
    base = (raw or "").lstrip("#").rsplit("/", 1)[-1]
    base = base.removeprefix("BP_").removesuffix("_C").removesuffix("_Placeable")
    parts = [p for p in base.split("_") if p]
    if not parts:
        return "structure"
    return " ".join(reversed(parts)).lower()


def _count_noun(n: int, singular: str, plural: str) -> str:
    """Plain 'N noun' with correct singular/plural (for monospace lines)."""
    return f"{n} {singular if n == 1 else plural}"


def _qty(n: int, singular: str, plural: str) -> str:
    """Bold '**N** noun' with correct singular/plural (for embed prose)."""
    return f"**{n}** {singular if n == 1 else plural}"


# Default sub-fief console placeable names leak through the biggest-base label
# when a holding was never christened; render those as a generic holding.
_CONSOLE_LABELS = {"Sub-Fief Console", "Advanced Sub-Fief Console"}


def _clean_base_label(label: str) -> str:
    if label in _CONSOLE_LABELS or label.endswith("Console"):
        return "an unnamed holding"
    return label


def _clip(text: str) -> str:
    return text[:1024] if len(text) > 1024 else text


_ACCT_RE = re.compile(r"^acct \d+$")


def _name(n: str) -> str:
    """Friendly fallback when an account never resolved to a character name."""
    return "a fellow Sleeper" if n and _ACCT_RE.match(n) else n


# ---- section renderers (data dict -> embed field value) -------------------

def _f_new_players(d: dict) -> str:
    names = d.get("names", [])
    n = d.get("count", 0)
    if n == 0:
        body = "No new arrivals this period."
    elif n <= 5:
        body = f"**{n}** new: " + ", ".join(names)
    else:
        body = f"**{n}** new Sleepers, including {', '.join(names[:3])}"
    total = d.get("total_all_time")
    if total is not None:
        body += f"\nSietch members all-time: **{total}**"
    return _clip(body)


def _f_worlds(d: dict) -> str:
    online = d.get("online_total", 0)
    hagga = d.get("hagga_players", 0)
    deep = d.get("deep_desert_players", 0)
    elsewhere = online - hagga - deep
    where = f"{hagga} in Hagga, {deep} in Deep Desert"
    if elsewhere > 0:
        where += f", +{elsewhere} elsewhere"
    lines = [
        "**Habbanya** and **Kulon** are our two Hagga Basin maps; "
        "the Deep Desert runs in PvP and PvE flavors.",
        "A quiet hour on the sands right now." if online == 0
        else f"Online now: **{online}** ({where})",
    ]
    return "\n".join(lines)


def _f_server_pulse(d: dict, period: str) -> str:
    value = (
        f"Peak concurrent: **{d.get('peak', 0)}**\n"
        f"Total time in the deep desert: **{d.get('play_hours', 0):.1f}h**"
    )
    bh = d.get("busiest_hour_utc")
    if bh is not None:
        value += f"\nBusiest hour: **{_et_hour_range(bh)}**"
    if period == "weekly" and d.get("active_days") is not None:
        value += f"\nActive days: **{d['active_days']} / 7**"
    return value


def _f_most_active(rows: list) -> str:
    if not rows:
        return "No player activity yet."
    return "\n".join(f"{i}. {_name(r['name'])} — {r['hours']:.1f}h" for i, r in enumerate(rows, 1))


def _f_pilot(rows: list) -> str:
    if not rows:
        return "No thopters logged airtime this period. The skies are quiet."
    if len(rows) == 1:
        r = rows[0]
        veh = _friendly_vehicle(r["vehicle_raw"])
        return f"{_name(r['name'])} soared **{r['km']:.1f} km** across the dunes in a {veh}."
    return "\n".join(
        f"{i}. {_name(r['name'])} — {r['km']:.1f} km ({_friendly_vehicle(r['vehicle_raw'])})"
        for i, r in enumerate(rows, 1)
    )


def _f_deaths(rows: list, variant: dict) -> str:
    if not rows:
        return variant["empty"]
    lines = []
    for i, r in enumerate(rows):
        if i == 0:
            lines.append("🏆 " + variant["line1"](_name(r["name"]), r["count"]))
        else:
            lines.append(f"{_name(r['name'])} — {r['count']}")
    return "\n".join(lines)


def _f_spice(d: dict) -> str:
    active = d.get("active_now_total", 0)
    spawning = d.get("spawning_count", 0)
    total = d.get("field_count", 0)
    suppressed = total - spawning
    line = f"**{active}** spice fields active across the desert right now."
    if suppressed > 0:
        line += (
            f"\nSpawning enabled on **{spawning}/{total}** field types "
            f"({suppressed} suppressed)."
        )
    else:
        line += f"\nAll **{total}** field types spawning normally."
    return line


def _f_testing_stations(d: dict, period: str) -> str | None:
    """Highest difficulty cleared per repeatable testing station.

    Two things this deliberately does NOT do. It never presents the recorded runner as
    the sole clearer of a group run -- dungeon_completion_players stores about one row
    per completion whatever the party size, so a solo-looking credit on a 4-player clear
    would be wrong; party size is shown as "+N" instead. And on the first run there is no
    cursor baseline, so the "new this period" markers are suppressed rather than flagging
    every record as fresh.
    """
    stations = d.get("stations") or []
    if not stations:
        return None
    first_run = bool(d.get("first_run"))

    lines: list[str] = []
    for s in sorted(stations, key=lambda x: (-int(x.get("top_difficulty") or 0),
                                             str(x.get("name") or ""))):
        top = int(s.get("top_difficulty") or 0)
        if top <= 0:
            continue
        name = s.get("name") or s.get("dungeon_id") or "?"
        line = f"**{name}** — tier **{top}**"
        runner = (s.get("record_runner") or "").strip()
        if runner:
            party = int(s.get("record_party_size") or 0)
            line += f" · {_name(runner)}" + (f" +{party - 1}" if party > 1 else "")
        if not first_run and int(s.get("top_this_period") or 0) >= top:
            line += "  🆕"
        lines.append(line)

    if not lines:
        return None
    if any(l.endswith("🆕") for l in lines):
        window = "today" if period == "daily" else "this week"
        lines.append(f"*🆕 = record set {window}.*")
    return _clip("\n".join(lines))


def _abbr(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n:,}"


def _f_economy(d: dict) -> str | None:
    totals, market = d.get("totals") or {}, d.get("market") or {}
    lines = []
    if market.get("trades"):
        lines.append(
            f"Exchange trades: **{market['trades']}** · "
            f"goods sold: **{_abbr(market.get('sold_value', 0))}** Solari"
        )
    if totals.get("solari"):
        lines.append(
            f"Wealth across the sietch: **{_abbr(totals['solari'])}** Solari "
            f"· **{_abbr(totals.get('scrip', 0))}** Scrip"
        )
    return "\n".join(lines) or None


def _f_moderation(d: dict) -> str | None:
    kicks, bans, unbans = d.get("kicks", 0), d.get("bans", 0), d.get("unbans", 0)
    active = d.get("active_bans", 0)
    if not any((kicks, bans, unbans, active)):
        return None  # nothing to report — skip the field entirely
    parts = []
    if kicks:
        parts.append(f"Kicks: **{kicks}**")
    if bans:
        parts.append(f"Bans: **{bans}**")
    if unbans:
        parts.append(f"Unbans: **{unbans}**")
    line = " · ".join(parts) if parts else "No actions this period."
    line += f"\nActive bans standing: **{active}**"
    return line


def _f_raids(d: dict) -> tuple[str, str]:
    events = d.get("events", [])
    code = (
        f"`Self-demolitions: {d.get('self_demos', 0)} · "
        f"Lost to the storm: {_count_noun(d.get('storm_orni', 0), 'ornithopter', 'ornithopters')}, "
        f"{_count_noun(d.get('storm_buggy', 0), 'buggy', 'buggies')} · "
        f"Hostile destruction: {len(events)}`"
    )
    if not events:
        quiet = "All quiet on the sands: no hostile destruction reported.\n" + code
        return "Raid / Destruction Watch", quiet
    lines = ["Raiders crossed the sand. The sietch counts its losses:"]
    for ev in events:
        hhmm = _time.strftime("%H:%M", _time.gmtime(ev["epoch"]))
        shield = "shielded " if ev.get("shielded") else ""
        thing = ev["thing"]
        # vehicle losses arrive with a raw model in 'thing'; prettify those.
        if "/" in thing or thing.startswith("#") or "Ornithopter" in thing or "Buggy" in thing:
            thing = _friendly_vehicle(thing)
        # destroyed placeables leak raw asset names (e.g. Totem_Small_Placeable).
        elif "_" in thing or thing.startswith("BP_") or thing.endswith("Placeable"):
            thing = _friendly_placeable(thing)
        lines.append(
            f"`{hhmm}` — **{_name(ev['owner'])}**'s {shield}{thing} "
            f"was torn down by **{_name(ev['raider'])}**."
        )
    return "⚠️ Raid / Destruction Watch", _clip("\n".join(lines) + "\n" + code)


def _f_server_health(d: dict) -> str:
    # Player-facing: keep it general, no pod names or restart counts.
    if not d:
        return "Status unavailable."
    if d.get("troubled"):
        return "Servers are up and running; a few brief restarts occurred this period."
    return "All servers running smoothly."


def _f_construction(d: dict) -> str:
    parts = [
        f"**{d.get('total_subfiefs', 0)} sub-fiefs** anchor the basin — "
        f"**{d.get('great', 0)}** great holdings, **{d.get('lesser', 0)}** lesser camps · "
        f"**{d.get('pieces_total', 0):,} build pieces** placed across Hagga."
    ]
    biggest = d.get("biggest", [])
    if biggest:
        parts.append("*Biggest bases:*")
        for i, b in enumerate(biggest, 1):
            parts.append(f"{i}. {_clean_base_label(b['label'])} — **{b['pieces']}** pieces")
    pacts = d.get("pacts", [])
    if pacts:
        parts.append("*Pacts struck this week:*")
        for p in pacts:
            parts.append(f"• {_name(p['host'])} welcomed {_name(p['guest'])} into their sub-fief")
    renames = d.get("renames", [])
    if renames:
        parts.append("*Newly christened:*")
        for nm in renames:
            parts.append(f'• a base renamed to "{nm}"')
    return _clip("\n".join(parts))


def _f_origins(rows: list) -> str:
    if not rows:
        return "No connection data yet."
    return "\n".join(f"{r['country']}: {r['count']}" for r in rows)


def _f_almanac(d: dict) -> str | None:
    """Weekly flavor facts; renders only the facts actually present in the blob."""
    lines = []

    km = d.get("flight_km_total")
    if km:
        lines.append(
            f"Sleepers flew a combined **{km:.1f} km** across the dunes this week."
        )

    grew = d.get("structures_delta")
    if grew and grew > 0:
        lines.append(f"The basin grew by {_qty(grew, 'build piece', 'build pieces')}.")

    veh = d.get("vehicles_now")
    if veh:
        roam = "roams" if veh == 1 else "roam"
        lines.append(f"{_qty(veh, 'vehicle', 'vehicles')} {roam} the sands.")

    breaches = d.get("worm_breaches")
    if breaches:
        per_day = breaches / 7.0
        lines.append(
            f"Shai-Hulud breached the sand an average of **{per_day:.1f}** times per day."
        )

    storms = d.get("sandstorms")
    if storms:
        hagga = storms.get("HaggaBasin") or 0
        deep = storms.get("DeepDesert") or 0
        storm_line = None
        if hagga and deep:
            storm_line = (
                f"Wild weather on Arrakis: {_qty(hagga, 'sandstorm', 'sandstorms')} "
                f"swept Hagga, {_qty(deep, 'sandstorm', 'sandstorms')} crossed the Deep Desert."
            )
        elif deep:
            storm_line = (
                f"Wild weather on Arrakis: {_qty(deep, 'sandstorm', 'sandstorms')} "
                "crossed the Deep Desert."
            )
        elif hagga:
            storm_line = (
                f"Wild weather on Arrakis: {_qty(hagga, 'sandstorm', 'sandstorms')} swept Hagga."
            )
        if storm_line:
            lines.append(storm_line)

    return "\n".join(lines) or None


def build_embed(data: dict) -> discord.Embed:
    """Render the structured stats blob into a Last Sietch-branded embed."""
    period = data.get("period", "daily")
    title = "Sietch Daily Report" if period == "daily" else "Sietch Weekly Report"
    variant = DEATH_VARIANTS[datetime.now(UTC).toordinal() % len(DEATH_VARIANTS)]

    embed = discord.Embed(title=f"🏜️ {title}", color=DUNE_COLOR)
    embed.set_author(name="Last Sietch", url=f"{SITE}/dune/")
    embed.set_footer(text=f"Last Sietch · {SITE.split('://')[1]}")
    embed.timestamp = datetime.now(UTC)

    def add(name: str, value: str | None, inline: bool = False) -> None:
        if value:
            embed.add_field(name=name, value=value, inline=inline)

    add("New Arrivals", _f_new_players(data.get("new_players") or {}))
    if data.get("worlds"):
        add("🏜️ The Sietches", _f_worlds(data["worlds"]))

    pulse = data.get("server_pulse")
    if pulse:
        add("🜂 Server Pulse", _f_server_pulse(pulse, period), inline=True)
        add("⛏️ Most Active Wanderer", _f_most_active(data.get("most_active") or []), inline=True)
    else:
        add("🜂 Server Pulse", "Sampler not yet running.")

    if period == "weekly":
        add("🪶 Pilot of the Week", _f_pilot(data.get("pilot") or []))

    if data.get("spice"):
        add("🌶️ Spice Fields", _f_spice(data["spice"]))

    add("💰 CHOAM Ledger", _f_economy(data.get("economy") or {}))

    if data.get("testing_stations"):
        add("⚙️ Testing Station Records",
            _f_testing_stations(data["testing_stations"], period))

    add(variant["name"], _f_deaths(data.get("deaths") or [], variant), inline=True)

    raid_name, raid_value = _f_raids(data.get("raids") or {})
    add(raid_name, raid_value)

    mod = _f_moderation(data.get("moderation") or {})
    add("⚖️ Sietch Justice", mod)

    add("Server Health", _f_server_health(data.get("server_health") or {}), inline=True)

    if period == "weekly":
        if data.get("construction"):
            add("🏗️ Sietch Construction", _f_construction(data["construction"]))
        add("📜 Desert Almanac", _f_almanac(data.get("almanac") or {}))
        add("Origins", _f_origins(data.get("origins") or []))

    improvements = data.get("improvements") or []
    if improvements:
        label = "Improvements This Week" if period == "weekly" else "Recent Improvements"
        body = "\n".join(f"- {t}" for t in improvements)
        add(label, _clip(body))

    return embed


class DuneStatus(commands.Cog):
    """Posts the daily + weekly Dune server digest to #dune-server-info."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if not settings.cielago_dune_status_channel_id:
            log.warning("dune_status.disabled_no_channel")
            return
        if not settings.cielago_relay_api_key:
            log.warning("dune_status.disabled_no_relay_key")
            return
        self._daily.start()
        self._weekly.start()

    async def cog_unload(self) -> None:
        self._daily.cancel()
        self._weekly.cancel()

    # --- data fetch ---

    async def _fetch(self, period: str) -> dict:
        url = f"{settings.cielago_relay_base_url.rstrip('/')}/dune/stats/digest"
        async with httpx.AsyncClient(timeout=95.0) as client:
            resp = await client.get(
                url,
                params={"period": period},
                headers={"X-API-Key": settings.cielago_relay_api_key},
            )
            resp.raise_for_status()
            return resp.json()

    async def _post(self, period: str) -> bool:
        channel = self.bot.get_channel(settings.cielago_dune_status_channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.warning(
                "dune_status.channel_missing",
                channel_id=settings.cielago_dune_status_channel_id,
            )
            return False
        try:
            data = await self._fetch(period)
        except Exception:
            log.warning("dune_status.fetch_failed", period=period, exc_info=True)
            return False
        embed = build_embed(data)
        try:
            await channel.send(embed=embed)
        except discord.DiscordException:
            log.warning("dune_status.post_failed", period=period, exc_info=True)
            return False
        log.info("dune_status.posted", period=period,
                 online=(data.get("worlds") or {}).get("online_total"))
        return True

    # --- scheduled loops ---

    @tasks.loop(time=DAILY_AT)
    async def _daily(self) -> None:
        await self._post("daily")

    @_daily.before_loop
    async def _before_daily(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(time=WEEKLY_AT)
    async def _weekly(self) -> None:
        # tasks.loop(time=...) fires daily at 18:00 ET; only Sunday posts the weekly.
        if datetime.now(ET).weekday() == SUNDAY:
            await self._post("weekly")

    @_weekly.before_loop
    async def _before_weekly(self) -> None:
        await self.bot.wait_until_ready()

    # --- manual trigger (admin) ---

    @app_commands.command(
        name="dune-digest", description="Post the Dune server digest now (admin)."
    )
    @app_commands.describe(period="daily or weekly")
    @app_commands.choices(period=[
        app_commands.Choice(name="daily", value="daily"),
        app_commands.Choice(name="weekly", value="weekly"),
    ])
    @admin_only()
    async def dune_digest(
        self, interaction: discord.Interaction, period: app_commands.Choice[str]
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        ok = await self._post(period.value)
        await audit(
            interaction.guild, "dune-digest", actor=interaction.user,
            period=period.value, ok=ok,
        )
        msg = f"Posted the {period.value} digest." if ok else (
            "Could not post the digest (see logs — channel, relay, or fetch issue)."
        )
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DuneStatus(bot))
