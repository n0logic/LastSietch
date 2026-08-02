from typing import Literal

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cielago.config import settings

log = structlog.get_logger()

SITE = "https://lastsietch.com"

# Conan Exiles mod loadout (Steam Workshop). Keep in sync with the in-game server.
CONAN_MODS = [
    {"name": "Stacksize Plus", "id": "3720915336", "desc": "Bigger inventory stacks, less tetris"},
    {"name": "Fashionist", "id": "3720921242", "desc": "Transmog / appearance system"},
    {
        "name": "Enhanced Builder",
        "id": "3720763208",
        "desc": "New T1/T2/T3 building set, UE5-native",
    },
]


def _dune_embed() -> discord.Embed:
    embed = discord.Embed(
        title="\U0001F3DC️ Last Sietch - Dune: Awakening",
        color=0xD4A574,
    )
    embed.add_field(
        name="Find the Server",
        value=(
            "Open the in-game server browser, switch to the **Experimental** tab, set region "
            "**North America**, and search for **Last Sietch**. Expand the row and hit Join."
        ),
        inline=False,
    )
    embed.add_field(
        name="Sietches",
        value=(
            "Two persistent Hagga sietches, **Habbanya** and **Kulon**, "
            "plus Deep Desert in PvP and PvE flavors."
        ),
        inline=False,
    )
    # Copy tracks the live split delivery in /opt/lastsietch-welcome-pack/watcher.sh. The pack
    # lands during the FIRST session over RMQ; only the overflow and the Intel/research
    # half are deferred. The old "relog once to receive it" line described the retired
    # offline-gated grant and stranded new players waiting for something already in
    # their bag. Keep this wording aligned with WELCOME_WHISPER_PARTS in that script.
    embed.add_field(
        name="Welcome Package",
        value=(
            "Auto-granted **during your first session**, no relog needed. Most of it lands "
            "straight in your backpack. Anything that does not fit waits at any **CHOAM "
            "Exchange terminal** under the **Completed** tab, labelled CANCELED, just hit "
            "Take item. Intel points and Base Construction research arrive after your next "
            "log out and back in."
        ),
        inline=False,
    )
    embed.add_field(name="Build", value="Dune: Awakening 1.4, custom-tuned rules", inline=True)
    embed.add_field(
        name="Steam",
        value="[Dune: Awakening](https://store.steampowered.com/app/1172710/Dune_Awakening/)",
        inline=True,
    )
    embed.add_field(name="Website", value=f"{SITE}/dune/", inline=False)
    info_id = settings.cielago_dune_status_channel_id
    embed.add_field(
        name="Server Highlights",
        value=(
            f"See <#{info_id}> for the full feature list."
            if info_id
            else "See #dune-server-info for the full feature list."
        ),
        inline=False,
    )
    return embed


def _conan_embed() -> discord.Embed:
    embed = discord.Embed(
        title="\U0001F5E1️ Last Sietch - Conan Exiles",
        color=0xB7472A,
    )
    embed.add_field(name="Direct Connect", value="`conan.lastsietch.com:7777`", inline=True)
    embed.add_field(name="Mode", value="PvE-C", inline=True)
    embed.add_field(
        name="Password",
        value="Password-protected. Ask in #conan-general for the connect password.",
        inline=False,
    )
    embed.add_field(
        name="Mods",
        value="Run `/mods` for the required Steam Workshop loadout.",
        inline=False,
    )
    embed.add_field(name="Website", value=f"{SITE}/conan/", inline=False)
    return embed


def _enshrouded_embed() -> discord.Embed:
    embed = discord.Embed(
        title="\U0001F332 Last Sietch - Enshrouded",
        color=0x4A7C3F,
    )
    embed.add_field(name="Add Server", value="`enshrouded.lastsietch.com`", inline=True)
    embed.add_field(name="Mode", value="Co-op", inline=True)
    embed.add_field(
        name="Password",
        value="May be password-protected. Ask in #enshrouded-general for the connect password.",
        inline=False,
    )
    embed.add_field(name="Website", value=f"{SITE}/enshrouded/", inline=False)
    return embed


_BUILDERS = {
    "dune": _dune_embed,
    "conan": _conan_embed,
    "enshrouded": _enshrouded_embed,
}


class Connect(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="connect", description="Get server connection info")
    @app_commands.describe(game="Which game server?")
    async def connect(
        self, interaction: discord.Interaction, game: Literal["dune", "conan", "enshrouded"]
    ) -> None:
        embed = _BUILDERS[game]()
        await interaction.response.send_message(embed=embed)
        log.info("connect.sent", game=game, by=interaction.user.id)

    @app_commands.command(name="mods", description="List required Conan Exiles mods")
    async def mods(self, interaction: discord.Interaction) -> None:
        base = "https://steamcommunity.com/sharedfiles/filedetails/?id="
        lines = [
            f"**{i + 1}. [{m['name']}]({base}{m['id']})**: {m['desc']}"
            for i, m in enumerate(CONAN_MODS)
        ]
        embed = discord.Embed(
            title="\U0001F5E1️ Required Conan Exiles Mods (Enhanced/UE5)",
            description=(
                "\n".join(lines)
                + "\n\nSubscribe on Steam Workshop, then activate them inside Conan from the "
                "in-game **Mods** menu."
            ),
            color=0xD4A574,
        )
        embed.set_footer(text="Apply mods from the in-game Mods menu, not the standalone launcher.")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Connect(bot))
