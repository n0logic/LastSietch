import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from random import SystemRandom
from typing import Literal

import discord
import structlog
from discord import app_commands
from discord.ext import commands, tasks

from cielago.audit import audit
from cielago.config import settings
from cielago.permissions import admin_only

log = structlog.get_logger()

_rng = SystemRandom()

LIVE_COLOR = 0x00FF41
ENDED_COLOR = 0x888888
WEEKEND_END_COLOR = 0xFFD700
SITE_URL = "https://lastsietch.com"

PLATFORM_LABELS = {
    "steam": "Steam",
    "xbox": "Xbox",
    "playstation": "PlayStation",
    "mixed": "Mixed platforms",
}


def platform_label(platform: str | None) -> str:
    return PLATFORM_LABELS.get((platform or "steam").lower(), "Steam")


# --- Pure helpers (unit-tested without a live Discord) ---


def ordinal(n: int) -> str:
    v = n % 100
    if 11 <= v <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def parse_game_entry(raw: str) -> dict:
    """Parse 'Name' or 'Name|https://store.url' into {name, url}."""
    parts = raw.strip().split("|")
    name = re.sub(r"\s+", " ", parts[0].strip())
    url = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return {"name": name, "url": url}


def group_game_entries(entries: list[dict]) -> list[dict]:
    """Collapse duplicate game names, counting keys."""
    grouped: dict[str, dict] = {}
    for entry in entries:
        key = entry["name"].strip().lower()
        if key in grouped:
            grouped[key]["count"] += 1
        else:
            grouped[key] = {"name": entry["name"].strip(), "url": entry["url"], "count": 1}
    return list(grouped.values())


def select_winners(entrants, count: int) -> list[int]:
    pool = list(entrants)
    return _rng.sample(pool, min(count, len(pool)))


# --- Model ---


@dataclass
class Giveaway:
    message_id: int
    channel_id: int
    guild_id: int
    prizes: list[str]
    ends_at: int
    entrants: set[int] = field(default_factory=set)
    winners: list[int] = field(default_factory=list)
    ended: bool = False
    is_weekend: bool = False
    game_entries: list[dict] | None = None
    platform: str | None = None
    reminder_sent: bool = False

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "guild_id": self.guild_id,
            "prizes": self.prizes,
            "ends_at": self.ends_at,
            "entrants": sorted(self.entrants),
            "winners": self.winners,
            "ended": self.ended,
            "is_weekend": self.is_weekend,
            "game_entries": self.game_entries,
            "platform": self.platform,
            "reminder_sent": self.reminder_sent,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Giveaway":
        return cls(
            message_id=int(data["message_id"]),
            channel_id=int(data["channel_id"]),
            guild_id=int(data["guild_id"]),
            prizes=list(data.get("prizes", [])),
            ends_at=int(data["ends_at"]),
            entrants=set(data.get("entrants", [])),
            winners=list(data.get("winners", [])),
            ended=bool(data.get("ended", False)),
            is_weekend=bool(data.get("is_weekend", False)),
            game_entries=data.get("game_entries"),
            platform=data.get("platform"),
            reminder_sent=bool(data.get("reminder_sent", False)),
        )


class GiveawayStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.giveaways: dict[int, Giveaway] = {}

    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("giveaway.load_bad_json", path=str(self.path))
            return
        for key, value in data.items():
            value.setdefault("message_id", key)
            g = Giveaway.from_dict(value)
            self.giveaways[g.message_id] = g
        log.info("giveaway.loaded", count=len(self.giveaways))

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {str(mid): g.to_dict() for mid, g in self.giveaways.items()}
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            log.warning("giveaway.save_failed", path=str(self.path), exc_info=True)

    def active(self) -> list[Giveaway]:
        return [g for g in self.giveaways.values() if not g.ended]


# --- Embeds (Last Sietch branded, no em dashes per house style) ---


def build_giveaway_embed(giveaway: Giveaway, ended: bool = False) -> discord.Embed:
    prize_lines = [
        f"\U0001F3C6 **Grand Prize:** {p}"
        if i == 0
        else f"\U0001F381 **{ordinal(i + 1)} Place:** {p}"
        for i, p in enumerate(giveaway.prizes)
    ]
    embed = discord.Embed(
        title="\U0001F389 Giveaway Ended!" if ended else "\U0001F389 Last Sietch Giveaway!",
        color=ENDED_COLOR if ended else LIVE_COLOR,
        description="\n".join(prize_lines),
    )
    if ended:
        if giveaway.winners:
            winner_lines = []
            for i, w in enumerate(giveaway.winners):
                prize = giveaway.prizes[i] if i < len(giveaway.prizes) else "Prize"
                if i == 0:
                    winner_lines.append(f"\U0001F3C6 **Grand Prize** ({prize}): <@{w}>")
                else:
                    winner_lines.append(f"\U0001F381 **{ordinal(i + 1)} Place** ({prize}): <@{w}>")
            embed.add_field(name="Winners", value="\n".join(winner_lines), inline=False)
        else:
            embed.add_field(name="Winners", value="Not enough entrants!", inline=False)
        embed.set_footer(text=f"{len(giveaway.entrants)} entries")
    else:
        embed.add_field(
            name="How to Enter",
            value="Click the **Enter Giveaway** button below!",
            inline=False,
        )
        embed.add_field(name="Entries", value=str(len(giveaway.entrants)), inline=True)
        embed.add_field(name="Winners", value=str(len(giveaway.prizes)), inline=True)
        embed.add_field(name="Ends", value=f"<t:{giveaway.ends_at}:R>", inline=True)
        embed.set_footer(text="All members are eligible | One entry per person")
    return embed


def build_weekend_embed(giveaway: Giveaway) -> discord.Embed:
    games = giveaway.game_entries or [{"name": p, "url": None} for p in giveaway.prizes]
    grouped = group_game_entries(games)
    game_lines = []
    for g in grouped:
        label = f"{g['name']} ({g['count']} keys)" if g["count"] > 1 else g["name"]
        if g["url"]:
            game_lines.append(f"\U0001F3AE [{label}]({g['url']})")
        else:
            game_lines.append(f"\U0001F3AE {label}")
    game_list = "\n".join(game_lines)
    ts = giveaway.ends_at
    total_keys = len(giveaway.prizes)
    label = platform_label(giveaway.platform)
    embed = discord.Embed(
        title="\U0001F389 Weekend Game Giveaway!",
        color=LIVE_COLOR,
        description=(
            "**The Last Sietch free game weekend is here!**\n\n"
            f"**Games up for grabs:**\n{game_list}\n\n"
            "**How to enter:**\nClick the **Enter Giveaway** button below, "
            "one click and you're in!\n\n"
            f"**Drawing:**\nWinners will be selected <t:{ts}:F> (<t:{ts}:R>)\n\n"
            f"**{total_keys} winners** will be selected and an admin will DM you your {label} key."
        ),
    )
    embed.add_field(name="Entries", value=str(len(giveaway.entrants)), inline=True)
    embed.add_field(name="Keys", value=str(total_keys), inline=True)
    embed.add_field(name="Drawing", value=f"<t:{ts}:R>", inline=True)
    embed.set_footer(text=f"{label} keys | One entry per person | Good luck!")
    return embed


def build_weekend_end_embed(giveaway: Giveaway) -> discord.Embed:
    label = platform_label(giveaway.platform)
    embed = discord.Embed(title="\U0001F389 Weekend Giveaway: Winners!", color=WEEKEND_END_COLOR)
    if giveaway.winners:
        winner_lines = []
        for i, w in enumerate(giveaway.winners):
            prize = giveaway.prizes[i] if i < len(giveaway.prizes) else "Game"
            winner_lines.append(f"\U0001F3AE **{prize}**: <@{w}>")
        embed.description = (
            "Congratulations to our winners!\n\n"
            + "\n".join(winner_lines)
            + f"\n\nAn admin will DM you your {label} key shortly. \U0001F381"
        )
    else:
        embed.description = "Not enough entrants to pick winners, better luck next time!"
    embed.set_footer(text=f"{len(giveaway.entrants)} total entries")
    return embed


# --- Restart-surviving enter button ---


class GiveawayEnterButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"giveaway_enter:(?P<mid>\d+)",
):
    def __init__(self, message_id: int, disabled: bool = False) -> None:
        self.message_id = message_id
        super().__init__(
            discord.ui.Button(
                label="\U0001F389 Enter Giveaway",
                style=discord.ButtonStyle.success,
                custom_id=f"giveaway_enter:{message_id}",
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["mid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Giveaways")
        if cog is None:
            await interaction.response.send_message(
                "Giveaways are unavailable right now.", ephemeral=True
            )
            return
        await cog.handle_entry(interaction, self.message_id)


def giveaway_view(message_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(GiveawayEnterButton(message_id, disabled=disabled))
    return view


# --- Cog ---


class Giveaways(commands.Cog):
    giveaway = app_commands.Group(
        name="giveaway",
        description="Manage Last Sietch giveaways",
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = GiveawayStore(settings.cielago_giveaway_data_path)

    async def cog_load(self) -> None:
        self.store.load()
        self._tick.start()

    async def cog_unload(self) -> None:
        self._tick.cancel()

    # --- Background ticker: auto-end + 24h weekend reminder ---

    @tasks.loop(seconds=30)
    async def _tick(self) -> None:
        now = int(time.time())
        for giveaway in list(self.store.giveaways.values()):
            if giveaway.ended:
                continue
            if now >= giveaway.ends_at:
                await self._end_giveaway(giveaway)
            elif (
                giveaway.is_weekend
                and not giveaway.reminder_sent
                and (giveaway.ends_at - now) <= 24 * 3600
            ):
                await self._send_reminder(giveaway)

    @_tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    def _channel(self, giveaway: Giveaway) -> discord.TextChannel | None:
        channel = self.bot.get_channel(giveaway.channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _send_reminder(self, giveaway: Giveaway) -> None:
        giveaway.reminder_sent = True
        self.store.save()
        channel = self._channel(giveaway)
        if channel is None:
            return
        try:
            await channel.send(
                "⏰ **24 hours left to enter the Weekend Giveaway!**\n\n"
                f"{len(giveaway.entrants)} entries so far, don't miss out!\n"
                f"Drawing: <t:{giveaway.ends_at}:F> (<t:{giveaway.ends_at}:R>)\n\n"
                "Scroll up and click **Enter Giveaway** if you haven't yet!"
            )
            log.info("giveaway.reminder_sent", message_id=giveaway.message_id)
        except discord.DiscordException:
            log.warning("giveaway.reminder_failed", message_id=giveaway.message_id, exc_info=True)

    async def _end_giveaway(self, giveaway: Giveaway) -> None:
        if giveaway.ended:
            return
        giveaway.ended = True
        giveaway.winners = select_winners(giveaway.entrants, len(giveaway.prizes))
        self.store.save()

        channel = self._channel(giveaway)
        if channel is None:
            log.warning("giveaway.end_no_channel", message_id=giveaway.message_id)
            return
        guild = channel.guild
        end_embed = (
            build_weekend_end_embed(giveaway)
            if giveaway.is_weekend
            else build_giveaway_embed(giveaway, ended=True)
        )
        try:
            message = await channel.fetch_message(giveaway.message_id)
            await message.edit(
                embeds=[end_embed],
                view=giveaway_view(giveaway.message_id, disabled=True),
            )
        except discord.DiscordException:
            log.warning("giveaway.end_edit_failed", message_id=giveaway.message_id, exc_info=True)

        if giveaway.winners:
            mentions = "\n".join(
                f"\U0001F3AE **{_prize_at(giveaway, i)}**: <@{w}>"
                for i, w in enumerate(giveaway.winners)
            )
            claim = (
                "An admin will DM you your key shortly!"
                if giveaway.is_weekend
                else "Contact an admin to claim your prize!"
            )
            await channel.send(
                "**\U0001F389 Giveaway ended!** Congratulations to our winners!\n\n"
                f"{mentions}\n\n{claim}"
            )
        else:
            await channel.send("Giveaway ended, not enough entrants to pick winners.")

        await audit(
            guild,
            "giveaway-ended",
            prize=giveaway.prizes[0] if giveaway.prizes else "none",
            winners=len(giveaway.winners),
            entries=len(giveaway.entrants),
            weekend=giveaway.is_weekend,
        )
        log.info(
            "giveaway.ended",
            message_id=giveaway.message_id,
            winners=len(giveaway.winners),
            entries=len(giveaway.entrants),
        )

    # --- Button entry ---

    async def handle_entry(self, interaction: discord.Interaction, message_id: int) -> None:
        giveaway = self.store.giveaways.get(message_id)
        if giveaway is None or giveaway.ended:
            await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
            return
        if interaction.user.id in giveaway.entrants:
            await interaction.response.send_message("You're already entered!", ephemeral=True)
            return
        giveaway.entrants.add(interaction.user.id)
        self.store.save()
        await interaction.response.send_message(
            f"You're in! \U0001F389 Entry #{len(giveaway.entrants)}, good luck!", ephemeral=True
        )
        updated = (
            build_weekend_embed(giveaway)
            if giveaway.is_weekend
            else build_giveaway_embed(giveaway)
        )
        try:
            await interaction.message.edit(embeds=[updated], view=giveaway_view(message_id))
        except discord.DiscordException:
            pass

    # --- Slash commands ---

    @giveaway.command(name="start", description="Start a giveaway")
    @app_commands.describe(
        prizes="Prizes (comma-separated, first = grand prize)",
        duration="Duration in hours (1-168)",
        channel="Channel to post in (default: current)",
    )
    @admin_only()
    async def start(
        self,
        interaction: discord.Interaction,
        prizes: str,
        duration: app_commands.Range[int, 1, 168],
        channel: discord.TextChannel | None = None,
    ) -> None:
        prize_list = [p.strip() for p in prizes.split(",") if p.strip()]
        if not prize_list:
            await interaction.response.send_message("Provide at least one prize.", ephemeral=True)
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "Pick a text channel to post in.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        ends_at = int(time.time()) + duration * 3600
        giveaway = Giveaway(
            message_id=0,
            channel_id=target.id,
            guild_id=interaction.guild_id or 0,
            prizes=prize_list,
            ends_at=ends_at,
        )
        message = await target.send(embed=build_giveaway_embed(giveaway))
        giveaway.message_id = message.id
        self.store.giveaways[message.id] = giveaway
        self.store.save()
        await message.edit(view=giveaway_view(message.id))
        await audit(
            interaction.guild,
            "giveaway-start",
            actor=interaction.user,
            prize=prize_list[0],
            prizes=len(prize_list),
            channel=target.name,
        )
        await interaction.followup.send(
            f"Giveaway started in {target.mention}! "
            f"{len(prize_list)} prize(s), ends <t:{ends_at}:R>.",
            ephemeral=True,
        )

    @giveaway.command(name="reroll", description="Re-roll winners for an ended giveaway")
    @app_commands.describe(giveaway_id="Giveaway message ID")
    @admin_only()
    async def reroll(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        giveaway = self.store.giveaways.get(_to_int(giveaway_id))
        if giveaway is None:
            await interaction.response.send_message(
                "Giveaway not found. Provide the message ID.", ephemeral=True
            )
            return
        if not giveaway.ended:
            await interaction.response.send_message(
                "Giveaway is still active. End it first or wait.", ephemeral=True
            )
            return
        giveaway.winners = select_winners(giveaway.entrants, len(giveaway.prizes))
        self.store.save()
        channel = self._channel(giveaway)
        if channel is not None:
            try:
                message = await channel.fetch_message(giveaway.message_id)
                await message.edit(embeds=[build_giveaway_embed(giveaway, ended=True)])
            except discord.DiscordException:
                pass
            mentions = []
            for i, w in enumerate(giveaway.winners):
                prize = giveaway.prizes[i] if i < len(giveaway.prizes) else "Prize"
                if i == 0:
                    mentions.append(f"\U0001F3C6 **Grand Prize**, {prize}: <@{w}>")
                else:
                    mentions.append(f"\U0001F381 **{ordinal(i + 1)} Place**, {prize}: <@{w}>")
            await channel.send(
                "**\U0001F504 Re-roll!** New winners:\n\n"
                + "\n".join(mentions)
                + "\n\nContact an admin to claim your prize!"
            )
        await audit(
            interaction.guild, "giveaway-reroll", actor=interaction.user, prize=giveaway.prizes[0]
        )
        await interaction.response.send_message("Winners re-rolled!", ephemeral=True)

    @giveaway.command(name="end", description="End a giveaway early")
    @app_commands.describe(giveaway_id="Giveaway message ID")
    @admin_only()
    async def end(self, interaction: discord.Interaction, giveaway_id: str) -> None:
        giveaway = self.store.giveaways.get(_to_int(giveaway_id))
        if giveaway is None:
            await interaction.response.send_message(
                "Giveaway not found. Provide the message ID.", ephemeral=True
            )
            return
        if giveaway.ended:
            await interaction.response.send_message("Giveaway already ended.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._end_giveaway(giveaway)
        await interaction.followup.send("Giveaway ended!", ephemeral=True)

    @giveaway.command(name="list", description="List active giveaways")
    @admin_only()
    async def list_(self, interaction: discord.Interaction) -> None:
        active = self.store.active()
        if not active:
            await interaction.response.send_message("No active giveaways.", ephemeral=True)
            return
        lines = []
        for g in active:
            extra = f" + {len(g.prizes) - 1} more" if len(g.prizes) > 1 else ""
            lines.append(
                f"• **{g.prizes[0]}**{extra}, {len(g.entrants)} entries, "
                f"ends <t:{g.ends_at}:R>, ID: `{g.message_id}`"
            )
        await interaction.response.send_message(
            "**Active Giveaways:**\n" + "\n".join(lines), ephemeral=True
        )

    @giveaway.command(name="announce", description="Post a giveaway pre-announcement")
    @app_commands.describe(
        timing="When does it start?",
        games='Games (comma-separated, use "Name|URL" for store links)',
        time="Entry start time (e.g. 6:00 PM EST)",
        draw_time="Drawing time (e.g. Sunday at 6:00 PM EST)",
        week="Week number (e.g. 1)",
        total_weeks="Total weeks (e.g. 2)",
        channel="Channel to post in (default: current)",
        platform="Key platform (default: Steam)",
    )
    @admin_only()
    async def announce(
        self,
        interaction: discord.Interaction,
        timing: Literal["tomorrow", "tonight", "today"],
        games: str,
        time: str,
        draw_time: str,
        week: app_commands.Range[int, 1, 10],
        total_weeks: app_commands.Range[int, 1, 10],
        channel: discord.TextChannel | None = None,
        platform: Literal["Steam", "Xbox", "PlayStation", "Mixed"] = "Steam",
    ) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "Pick a text channel to post in.", ephemeral=True
            )
            return
        entries = [parse_game_entry(g) for g in games.split(",")]
        entries = [e for e in entries if e["name"]]
        grouped = group_game_entries(entries)
        total_keys = len(entries)
        game_lines = []
        for g in grouped:
            label = f"({g['count']} keys)" if g["count"] > 1 else "(1 key)"
            if g["url"]:
                game_lines.append(f"\U0001F3AE {g['name']}, [{platform} Store]({g['url']}) {label}")
            else:
                game_lines.append(f"\U0001F3AE {g['name']} {label}")
        game_list = "\n".join(game_lines)
        timing_text = {
            "tomorrow": "STARTS TOMORROW",
            "tonight": "STARTS TONIGHT",
            "today": "STARTS TODAY",
        }[timing]
        timing_line = {
            "tomorrow": f"Come back **tomorrow at {time}**",
            "tonight": f"Come back **tonight at {time}**",
            "today": f"Come back **today at {time}**",
        }[timing]
        embed = discord.Embed(
            title=f"\U0001F3AE FREE GAME WEEKEND {timing_text}!",
            color=LIVE_COLOR,
            description=(
                "To celebrate the Last Sietch Discord, website, and game servers, "
                f"we're giving away free {platform} keys over "
                f"**{total_weeks} weekends of giveaways!**\n\n"
                f"**Week {week}, Games up for grabs:**\n{game_list}\n\n"
                "**How it works:**\n"
                f"• {timing_line}, an entry button will appear right here\n"
                f"• You have from **{time} until {draw_time}** to enter\n"
                "• Click it once to enter (one entry per person)\n"
                f"• Winners drawn **{draw_time}**\n"
                f"• An admin will DM winners their {platform} key\n\n"
                f"**{total_keys} keys. {total_keys} winners. "
                f"Week {week} of {total_weeks}, don't miss it!**\n\n"
                f"\U0001F310 [lastsietch.com]({SITE_URL})"
            ),
        )
        await interaction.response.defer(ephemeral=True)
        await target.send(embed=embed)
        await audit(
            interaction.guild,
            "giveaway-announce",
            actor=interaction.user,
            channel=target.name,
            week=week,
            keys=total_keys,
        )
        await interaction.followup.send(f"Announcement posted in {target.mention}!", ephemeral=True)

    @giveaway.command(
        name="weekend",
        description="Start a weekend giveaway with announcement + reminder + drawing",
    )
    @app_commands.describe(
        games='Games (comma-separated, use "Name|URL" for store links)',
        drawing="Drawing date/time in Eastern (e.g. 2026-04-06 19:00)",
        channel="Announcement channel (default: current)",
        platform="Key platform",
    )
    @admin_only()
    async def weekend(
        self,
        interaction: discord.Interaction,
        games: str,
        drawing: str,
        channel: discord.TextChannel | None = None,
        platform: Literal["steam", "xbox", "playstation", "mixed"] = "steam",
    ) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "Pick a text channel to post in.", ephemeral=True
            )
            return
        entries = [parse_game_entry(g) for g in games.split(",")]
        entries = [e for e in entries if e["name"]]
        prize_list = [e["name"] for e in entries]
        if not prize_list:
            await interaction.response.send_message("Provide at least one game.", ephemeral=True)
            return
        ends_at = _parse_eastern(drawing)
        if ends_at is None:
            await interaction.response.send_message(
                "Invalid date. Use format: `2026-04-06 19:00`", ephemeral=True
            )
            return
        now = int(time.time())
        if ends_at <= now:
            await interaction.response.send_message(
                "Drawing time must be in the future.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        has_reminder = (ends_at - now) > 24 * 3600
        giveaway = Giveaway(
            message_id=0,
            channel_id=target.id,
            guild_id=interaction.guild_id or 0,
            prizes=prize_list,
            ends_at=ends_at,
            is_weekend=True,
            game_entries=entries,
            platform=platform,
            reminder_sent=not has_reminder,
        )
        message = await target.send(embed=build_weekend_embed(giveaway))
        giveaway.message_id = message.id
        self.store.giveaways[message.id] = giveaway
        self.store.save()
        await message.edit(view=giveaway_view(message.id))
        await audit(
            interaction.guild,
            "giveaway-weekend",
            actor=interaction.user,
            channel=target.name,
            games=len(prize_list),
        )
        reminder_note = "scheduled" if has_reminder else "skipped (less than 24hrs to drawing)"
        await interaction.followup.send(
            f"Weekend giveaway started in {target.mention}!\n"
            f"{len(prize_list)} game(s), drawing <t:{ends_at}:F>.\n"
            f"24hr reminder: {reminder_note}.",
            ephemeral=True,
        )


def _prize_at(giveaway: "Giveaway", index: int, default: str = "Prize") -> str:
    return giveaway.prizes[index] if index < len(giveaway.prizes) else default


def _to_int(value: str) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return 0


def _parse_eastern(raw: str) -> int | None:
    """Parse 'YYYY-MM-DD HH:MM' as US Eastern, return unix seconds."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        naive = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    eastern = naive.replace(tzinfo=ZoneInfo("America/New_York"))
    return int(eastern.timestamp())


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(GiveawayEnterButton)
    await bot.add_cog(Giveaways(bot))
