import discord
import structlog
from discord.ext import commands

from cielago.config import settings

log = structlog.get_logger()


def _find_channel(guild: discord.Guild, suffix: str) -> discord.TextChannel | None:
    for ch in guild.text_channels:
        if ch.name.lower().endswith(suffix):
            return ch
    return None


class Onboarding(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id != settings.last_sietch_guild_id:
            return
        intro = _find_channel(member.guild, "intro")
        if intro is None:
            log.warning("onboarding.no_intro_channel", guild=member.guild.id)
            return
        rules = _find_channel(member.guild, "rules")
        rules_ref = rules.mention if rules else "the rules channel"
        info_ch = (
            self.bot.get_channel(settings.cielago_dune_status_channel_id)
            if settings.cielago_dune_status_channel_id
            else None
        )
        info_ref = info_ch.mention if info_ch else "#dune-server-info"
        try:
            await intro.send(
                f"Welcome to the **Last Sietch**, {member.mention}. "
                f"Start in {rules_ref}, then see {info_ref} for what makes our server different. "
                f"Tell us who you are when you're ready. The spice must flow. \U0001F3DC️"
            )
            log.info("onboarding.welcomed", member=member.id, guild=member.guild.id)
        except discord.DiscordException as exc:
            log.warning("onboarding.welcome_failed", member=member.id, error=str(exc))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Onboarding(bot))
