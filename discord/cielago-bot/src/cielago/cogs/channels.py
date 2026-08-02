import json
from importlib.resources import files

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cielago.audit import audit
from cielago.permissions import admin_only

log = structlog.get_logger()

DEFAULT_SEP = "｜"


def load_layout() -> dict:
    raw = files("cielago").joinpath("data/server_layout.json").read_text(encoding="utf-8")
    return json.loads(raw)


def channel_name(emoji: str, name: str, sep: str = DEFAULT_SEP) -> str:
    """Compose the Last Sietch-style display name: '<emoji><sep><name>'."""
    return f"{emoji}{sep}{name}"


class Channels(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="channel-template",
        description="Build the Last Sietch category/channel layout (idempotent).",
    )
    @admin_only()
    async def channel_template(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        layout = load_layout()
        sep = layout.get("separator", DEFAULT_SEP)
        everyone = guild.default_role
        created_cats: list[str] = []
        created_chans: list[str] = []
        skipped = 0

        for cat in layout["categories"]:
            private = bool(cat.get("private"))
            overwrites = (
                {everyone: discord.PermissionOverwrite(view_channel=False)} if private else {}
            )
            category = discord.utils.get(guild.categories, name=cat["name"])
            if category is None:
                category = await guild.create_category(
                    cat["name"], overwrites=overwrites, reason="Cielago channel-template"
                )
                created_cats.append(cat["name"])

            for ch in cat["channels"]:
                composed = channel_name(ch["emoji"], ch["name"], sep)
                if discord.utils.get(category.channels, name=composed) is not None:
                    skipped += 1
                    continue
                if ch.get("type") == "voice":
                    await guild.create_voice_channel(
                        composed,
                        category=category,
                        overwrites=overwrites,
                        reason="Cielago channel-template",
                    )
                else:
                    ch_overwrites = dict(overwrites)
                    if ch.get("readonly"):
                        ow = ch_overwrites.get(everyone) or discord.PermissionOverwrite()
                        ow.send_messages = False
                        ow.create_public_threads = False
                        ow.create_private_threads = False
                        ow.send_messages_in_threads = False
                        ch_overwrites[everyone] = ow
                    await guild.create_text_channel(
                        composed,
                        category=category,
                        topic=ch.get("topic"),
                        nsfw=bool(ch.get("nsfw")),
                        overwrites=ch_overwrites,
                        reason="Cielago channel-template",
                    )
                created_chans.append(composed)

        await audit(
            guild,
            "channel-template",
            actor=interaction.user,
            categories_created=created_cats or "none",
            channels_created=len(created_chans),
            skipped=skipped,
        )
        log.info(
            "channel.template",
            cats=created_cats,
            chans=created_chans,
            skipped=skipped,
            by=interaction.user.id,
        )
        summary = (
            f"Categories created: {created_cats or 'none'}\n"
            f"Channels created ({len(created_chans)}): {', '.join(created_chans) or 'none'}\n"
            f"Skipped (already existed): {skipped}"
        )
        await interaction.followup.send(summary[:1900], ephemeral=True)

    @app_commands.command(name="channel-create", description="Create a single channel.")
    @app_commands.describe(
        name="Channel name without emoji/bar (e.g. dune-events)",
        emoji="Leading theme emoji",
        category="Category name to place it under (optional)",
        voice="Make it a voice channel",
    )
    @admin_only()
    async def channel_create(
        self,
        interaction: discord.Interaction,
        name: str,
        emoji: str = "💬",
        category: str | None = None,
        voice: bool = False,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        sep = load_layout().get("separator", DEFAULT_SEP)
        composed = channel_name(emoji, name, sep)
        cat_obj = discord.utils.get(guild.categories, name=category) if category else None
        if category and cat_obj is None:
            await interaction.followup.send(f"Category `{category}` not found.", ephemeral=True)
            return
        if discord.utils.get(guild.channels, name=composed) is not None:
            await interaction.followup.send(f"Channel `{composed}` already exists.", ephemeral=True)
            return

        if voice:
            await guild.create_voice_channel(
                composed, category=cat_obj, reason=f"Cielago: {interaction.user}"
            )
        else:
            await guild.create_text_channel(
                composed, category=cat_obj, reason=f"Cielago: {interaction.user}"
            )

        await audit(
            guild,
            "channel-create",
            actor=interaction.user,
            channel=composed,
            category=category,
            voice=voice,
        )
        await interaction.followup.send(f"Created `{composed}`.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Channels(bot))
