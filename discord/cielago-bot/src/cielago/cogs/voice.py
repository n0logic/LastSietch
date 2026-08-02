import asyncio

import discord
import structlog
from better_profanity import profanity
from discord.ext import commands

from cielago.audit import audit
from cielago.config import settings

log = structlog.get_logger()

profanity.load_censor_words()

GRACE_SECONDS = 30
TRIGGER_SUFFIX = "join to create"


# --- Pure helpers (unit-tested without a live Discord) ---


def is_trigger_name(name: str) -> bool:
    return name.lower().endswith(TRIGGER_SUFFIX)


def temp_channel_name(display_name: str, game: str | None) -> str:
    base = game if game else f"{display_name}'s Channel"
    return base[:100]


def member_game(member: discord.Member) -> str | None:
    for activity in getattr(member, "activities", ()) or ():
        if activity.type == discord.ActivityType.playing and activity.name:
            return activity.name
    return None


class JoinToCreate(commands.Cog):
    """Auto-create a temporary voice channel when a member joins the trigger channel,
    name it after their game (needs the Presence intent), and clean it up when empty."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.temp_channels: dict[int, int] = {}  # channel_id -> owner_id
        self._delete_tasks: dict[int, asyncio.Task] = {}
        self._cleanup_done = False

    # --- Trigger resolution ---

    def _resolve_trigger(self, guild: discord.Guild) -> discord.VoiceChannel | None:
        tid = settings.cielago_jtc_trigger_channel_id
        if tid:
            channel = guild.get_channel(tid)
            return channel if isinstance(channel, discord.VoiceChannel) else None
        for channel in guild.voice_channels:
            if is_trigger_name(channel.name):
                return channel
        return None

    # --- Orphan cleanup on first ready ---

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._cleanup_done:
            return
        self._cleanup_done = True
        for guild in self.bot.guilds:
            if guild.id != settings.last_sietch_guild_id:
                continue
            trigger = self._resolve_trigger(guild)
            if trigger is None or trigger.category is None:
                continue
            for channel in trigger.category.voice_channels:
                if channel.id == trigger.id:
                    continue
                # A JTC channel is identifiable by a per-member permission overwrite (the owner).
                if not any(isinstance(t, discord.Member) for t in channel.overwrites):
                    continue
                if not channel.members:
                    try:
                        await channel.delete(reason="Cielago JTC: orphan cleanup")
                        log.info("jtc.orphan_deleted", channel=channel.name)
                    except discord.DiscordException:
                        log.warning("jtc.orphan_delete_failed", channel=channel.id, exc_info=True)
                else:
                    self.temp_channels[channel.id] = next(iter(channel.members)).id
                    log.info("jtc.retracked", channel=channel.name)

    # --- Core voice-state logic ---

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot or member.guild.id != settings.last_sietch_guild_id:
            return
        trigger = self._resolve_trigger(member.guild)
        if trigger is None:
            return

        joined_trigger = (
            after.channel is not None
            and after.channel.id == trigger.id
            and (before.channel is None or before.channel.id != trigger.id)
        )
        if joined_trigger:
            await self._create_temp(member, trigger)

        if before.channel is not None and before.channel.id in self.temp_channels:
            await self._schedule_delete(before.channel)

        if after.channel is not None and after.channel.id in self.temp_channels:
            self._cancel_delete(after.channel.id)

    async def _create_temp(self, member: discord.Member, trigger: discord.VoiceChannel) -> None:
        name = temp_channel_name(member.display_name, member_game(member))
        overwrites = {
            member: discord.PermissionOverwrite(
                manage_channels=True,
                mute_members=True,
                deafen_members=True,
                move_members=True,
                connect=True,
            )
        }
        try:
            temp = await member.guild.create_voice_channel(
                name,
                category=trigger.category,
                overwrites=overwrites,
                reason=f"Cielago JTC: {member}",
            )
            self.temp_channels[temp.id] = member.id
            await member.move_to(temp, reason="Cielago JTC")
            log.info("jtc.created", channel=temp.name, owner=member.id)
        except discord.DiscordException:
            log.warning("jtc.create_failed", member=member.id, exc_info=True)

    async def _schedule_delete(self, channel: discord.VoiceChannel) -> None:
        if channel.members:
            return
        self._cancel_delete(channel.id)
        self._delete_tasks[channel.id] = asyncio.create_task(
            self._delete_after_grace(channel.id, channel.guild.id)
        )

    async def _delete_after_grace(self, channel_id: int, guild_id: int) -> None:
        try:
            await asyncio.sleep(GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if isinstance(channel, discord.VoiceChannel) and not channel.members:
            name = channel.name
            try:
                await channel.delete(reason="Cielago JTC: empty after grace")
                log.info("jtc.deleted", channel=name)
            except discord.DiscordException:
                log.warning("jtc.delete_failed", channel=channel_id, exc_info=True)
        self.temp_channels.pop(channel_id, None)
        self._delete_tasks.pop(channel_id, None)

    def _cancel_delete(self, channel_id: int) -> None:
        task = self._delete_tasks.pop(channel_id, None)
        if task is not None:
            task.cancel()

    # --- Profanity filter on temp-channel rename ---

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        if after.id not in self.temp_channels or before.name == after.name:
            return
        if not profanity.contains_profanity(after.name):
            return
        owner_id = self.temp_channels[after.id]
        fallback = f"Channel {str(after.id)[-4:]}"
        try:
            await after.edit(name=fallback, reason="Cielago JTC: profanity filter")
        except discord.DiscordException:
            log.warning("jtc.profanity_revert_failed", channel=after.id, exc_info=True)
            return
        owner = after.guild.get_member(owner_id)
        if owner is not None:
            try:
                await owner.send("Your channel name contained prohibited words and was reset.")
            except discord.DiscordException:
                pass
        await audit(after.guild, "jtc-profanity", channel=fallback, owner_id=owner_id)
        log.info("jtc.profanity_filtered", channel=after.id, owner=owner_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JoinToCreate(bot))
