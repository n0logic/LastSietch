"""Join welcome card and the opt-in sietch role.

On join, Cielago posts a card with four buttons in the intro channel: how to
connect, the rules, a link to the Player Portal login, and a sietch picker.
Nothing here gates anything. The card is help, not a door: a member who never
presses a button has exactly the access of one who presses all four, and the
portal character link stays self-serve.

The sietch pick grants ONE of Habbanya / Amtal so staff can reach a sietch's
players without @everyone. "Just looking" clears them. Kulon closed on
2026-10-28 (its bases moved to Amtal): its role is never granted, but any pick
strips a leftover one.

Both views use fixed custom_ids and are registered in cog_load, so cards posted
before a restart keep working.
"""

import json
import os

import discord
import structlog
from discord.ext import commands

from cielago.audit import audit
from cielago.cogs.connect import dune_embed
from cielago.config import settings

log = structlog.get_logger()

PORTAL = "https://portal.lastsietch.com"

# choice value -> (role name, one-line mode). Role names must match DEFAULT_ROLES.
SIETCHES = {
    "habbanya": ("Habbanya", "PvE"),
    "amtal": ("Amtal", "full PvP"),
}
# Closed sietches: removed from a member on any pick, never granted.
RETIRED_ROLES = ("Kulon",)
LOOKING = "looking"

# Headline summary of the portal /rules page and the pinned #rules post. Keep it
# short and unnumbered: the full code (and its numbering) lives on the portal.
RULES_ZERO_TOLERANCE = (
    "**Zero tolerance, instant permanent ban:** doxxing, threats or stalking, slurs, "
    "cheats or hacks, real-money trading, and attacks on the servers."
)
RULES = [
    "**The tribe.** Respect everyone, wear an honest name, keep chat and voice civil.",
    "**Play fair.** No griefing weaker players. Report exploits with `/report`, never use them.",
    "**Bases differ by sietch.** Habbanya: an unclaimed build with no fief is fair game. "
    "Amtal: keep your generators fueled; exploiting your way into a base is banned.",
    "**Honour your trades.** No scams. Staff do not insure trades.",
    "**Appeals.** Within 7 days, press **Appeal or private staff matter** on the "
    "`/report` panel. Reviewed decisions are final.",
]


def _find_channel(guild: discord.Guild, suffix: str) -> discord.TextChannel | None:
    for ch in guild.text_channels:
        if ch.name.lower().endswith(suffix):
            return ch
    return None


def _channel(guild: discord.Guild, channel_id: int | None, suffix: str):
    if channel_id:
        ch = guild.get_channel(channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        log.warning("onboarding.configured_channel_missing", channel_id=channel_id)
    return _find_channel(guild, suffix)


def plan_roles(choice: str, held: set[str]) -> tuple[str | None, list[str]]:
    """(role name to add or None, role names to remove) for a pick."""
    target = SIETCHES[choice][0] if choice in SIETCHES else None
    names = [name for name, _ in SIETCHES.values()] + list(RETIRED_ROLES)
    remove = [name for name in names if name != target and name in held]
    add = target if target and target not in held else None
    return add, remove


def welcome_embed(guild: discord.Guild) -> discord.Embed:
    start = _find_channel(guild, "start-here")
    more = f"\n\nEverything else lives in {start.mention}." if start else ""
    embed = discord.Embed(
        title="Welcome to the Last Sietch",
        description=(
            "Glad you found us. Four quick ways to get into the desert:\n\n"
            "**How to connect**: finding the server and what your Welcome Package holds.\n"
            "**Rules**: the code we live by.\n"
            "**Pick your sietch**: PvE (Habbanya) or PvP (Amtal). It only changes which "
            "notices reach you; every channel stays open to everyone.\n"
            "**Link your character**: sign in to the Player Portal and tie your "
            "character to Discord.\n\n"
            "None of it is required. Play first and come back whenever you like. "
            f"Tell us who you are when you're ready.{more}"
        ),
        color=0xD4A574,
    )
    embed.set_footer(text="The spice must flow.")
    return embed


def rules_embed() -> discord.Embed:
    lines = [RULES_ZERO_TOLERANCE, ""] + [f"\u2022 {rule}" for rule in RULES]
    return discord.Embed(
        title="Rules of the Sietch",
        description="\n".join(lines) + f"\n\nThe full rules: {PORTAL}/rules",
        color=0xD4A574,
    )


class SietchPickView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id="cielago:welcome:sietch-pick",
        placeholder="PvE or PvP?",
        options=[
            discord.SelectOption(label="PvE: Habbanya", value="habbanya",
                                 description="Notices for the PvE sietch"),
            discord.SelectOption(label="PvP: Amtal (full PvP)", value="amtal",
                                 description="Notices for the full PvP sietch"),
            discord.SelectOption(label="Just looking", value=LOOKING,
                                 description="No sietch notices for now"),
        ],
    )
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        cog = interaction.client.get_cog("Onboarding")
        if cog is None:
            await interaction.response.send_message(
                "Sietch picking is unavailable right now. Try again in a minute.",
                ephemeral=True)
            return
        await cog.apply_pick(interaction, select.values[0])


class WelcomeView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Link your character", style=discord.ButtonStyle.link, url=f"{PORTAL}/login"))

    @discord.ui.button(label="How to connect", style=discord.ButtonStyle.primary,
                       custom_id="cielago:welcome:connect")
    async def connect(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=dune_embed(), ephemeral=True)

    @discord.ui.button(label="Rules", style=discord.ButtonStyle.secondary,
                       custom_id="cielago:welcome:rules")
    async def rules(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=rules_embed(), ephemeral=True)

    @discord.ui.button(label="Pick your sietch", style=discord.ButtonStyle.success,
                       custom_id="cielago:welcome:sietch")
    async def sietch(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "PvE or PvP? This only changes which sietch notices reach you. Everyone "
            "keeps every channel, including the information channels, and nothing is "
            "gated. Change it any time.",
            view=SietchPickView(), ephemeral=True)


class Onboarding(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._welcomed = self._load_welcomed()

    async def cog_load(self) -> None:
        self.bot.add_view(WelcomeView())
        self.bot.add_view(SietchPickView())

    # --- first-pick record ---

    def _load_welcomed(self) -> set[int]:
        try:
            with open(settings.cielago_sietch_welcomed_path, encoding="utf-8") as fh:
                return {int(x) for x in json.load(fh)}
        except (FileNotFoundError, ValueError, OSError, TypeError):
            return set()

    def _save_welcomed(self) -> None:
        path = settings.cielago_sietch_welcomed_path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sorted(self._welcomed), fh)
        os.replace(tmp, path)

    # --- join ---

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id != settings.last_sietch_guild_id:
            return
        intro = _channel(member.guild, settings.cielago_welcome_channel_id, "intro")
        if intro is None:
            log.warning("onboarding.no_intro_channel", guild=member.guild.id)
            return
        try:
            await intro.send(
                f"Welcome to the **Last Sietch**, {member.mention}.",
                embed=welcome_embed(member.guild),
                view=WelcomeView(),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[member]),
            )
            log.info("onboarding.welcomed", member=member.id, guild=member.guild.id)
        except discord.DiscordException as exc:
            log.warning("onboarding.welcome_failed", member=member.id, error=str(exc))

    # --- sietch pick ---

    async def apply_pick(self, interaction: discord.Interaction, choice: str) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Pick your sietch from inside the Last Sietch server.", ephemeral=True)
            return

        held = {r.name for r in member.roles}
        add_name, remove_names = plan_roles(choice, held)
        add_role = discord.utils.get(guild.roles, name=add_name) if add_name else None
        if add_name and add_role is None:
            await self._pick_failed(interaction, member, choice, f"role {add_name} not found")
            return
        remove_roles = [r for r in member.roles if r.name in remove_names]

        try:
            if remove_roles:
                await member.remove_roles(*remove_roles, reason="Cielago: sietch pick")
            if add_role is not None:
                await member.add_roles(add_role, reason="Cielago: sietch pick")
        except discord.DiscordException as exc:
            await self._pick_failed(interaction, member, choice, f"role change failed: {exc}")
            return

        if choice in SIETCHES:
            name, mode = SIETCHES[choice]
            msg = (f"Done. You'll get **{name}** ({mode}) notices. Every channel stays "
                   "open to you either way. Change it any time with Pick your sietch.")
        elif remove_roles:
            msg = ("Cleared. You won't get sietch notices, and every channel stays open "
                   "to you. Pick one whenever you're ready.")
        else:
            msg = ("No sietch picked, and every channel stays open to you. Pick one "
                   "whenever you're ready.")
        await interaction.response.send_message(msg, ephemeral=True)

        log.info("onboarding.sietch_pick", member=member.id, choice=choice,
                 added=add_name, removed=remove_names)
        await audit(guild, "sietch-pick", actor=member, choice=choice,
                    added=add_name or "none", removed=", ".join(remove_names) or "none")
        if choice in SIETCHES:
            await self._public_welcome(guild, member, choice)

    async def _pick_failed(self, interaction: discord.Interaction, member: discord.Member,
                           choice: str, problem: str) -> None:
        log.warning("onboarding.sietch_pick_failed", member=member.id, choice=choice,
                    problem=problem)
        await interaction.response.send_message(
            "That role could not be set right now. Staff have been told. Nothing else "
            "is affected, and you can keep playing as normal.", ephemeral=True)
        await audit(interaction.guild, "sietch-pick-failed", actor=member, choice=choice,
                    problem=problem)

    async def _public_welcome(self, guild: discord.Guild, member: discord.Member,
                              choice: str) -> None:
        if not settings.cielago_sietch_public_welcome or member.id in self._welcomed:
            return
        channel = _channel(guild, settings.cielago_sietch_welcome_channel_id, "dune-general")
        if channel is None:
            log.warning("onboarding.no_sietch_welcome_channel", guild=guild.id)
            return
        name, mode = SIETCHES[choice]
        try:
            await channel.send(
                f"{member.mention} picked **{name}** ({mode}) as their sietch. "
                "Welcome to the desert.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException as exc:
            log.warning("onboarding.sietch_welcome_failed", member=member.id, error=str(exc))
            return
        self._welcomed.add(member.id)
        try:
            self._save_welcomed()
        except OSError:
            log.warning("onboarding.welcomed_save_failed", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Onboarding(bot))
