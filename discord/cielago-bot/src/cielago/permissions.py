import discord
from discord import app_commands

from cielago.config import settings


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user.id):
            await interaction.response.send_message(
                "Only the Naib's cielago may invoke that.", ephemeral=True
            )
            return False
        return True

    return app_commands.check(predicate)
