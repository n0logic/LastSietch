import asyncio
import logging

import discord
import structlog
from discord.ext import commands

from cielago.config import settings

log = structlog.get_logger()


def make_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True
    intents.presences = True  # Join-to-Create names temp channels after the member's game
    return intents


class Cielago(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=make_intents())

    async def setup_hook(self) -> None:
        await self.load_extension("cielago.cogs.admin")
        await self.load_extension("cielago.cogs.channels")
        await self.load_extension("cielago.cogs.connect")
        await self.load_extension("cielago.cogs.dune_status")
        await self.load_extension("cielago.cogs.dune_buildwatch")
        await self.load_extension("cielago.cogs.giveaways")
        await self.load_extension("cielago.cogs.market_alerts")
        await self.load_extension("cielago.cogs.onboarding")
        await self.load_extension("cielago.cogs.assistant")
        await self.load_extension("cielago.cogs.voice")
        guild = discord.Object(id=settings.last_sietch_guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("cielago.ready", admins=list(settings.admin_ids))

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("cielago.online", user=str(self.user), id=self.user.id)


def _setup_logging() -> None:
    logging.basicConfig(level=getattr(logging, settings.cielago_log_level.upper(), logging.INFO))


async def main() -> None:
    _setup_logging()
    bot = Cielago()
    async with bot:
        await bot.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(main())
