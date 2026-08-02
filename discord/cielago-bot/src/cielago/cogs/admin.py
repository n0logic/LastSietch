import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cielago.audit import audit
from cielago.permissions import admin_only

log = structlog.get_logger()

DEFAULT_ROLES = [
    ("Naib", 0xF4A148, True),
    ("Gatekeepers", 0xD4AF37, True),
    ("Wallwatchers", 0xC8923D, True),
    ("Holdouts", 0xA4441F, True),
    ("Holders", 0x8B6F47, False),
    ("Wanderers", 0x6B6555, False),
    ("At The Gate", 0x5A5752, False),
]


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="role-create", description="Create a role.")
    @app_commands.describe(name="Role display name", color_hex="Hex color, e.g. 0xA4441F")
    @admin_only()
    async def role_create(
        self, interaction: discord.Interaction, name: str, color_hex: str = "0x808080"
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        existing = discord.utils.get(guild.roles, name=name)
        if existing is not None:
            await interaction.response.send_message(
                f"Role `{name}` already exists.", ephemeral=True
            )
            return
        color = discord.Color(int(color_hex, 16))
        role = await guild.create_role(
            name=name, color=color, reason=f"Cielago: {interaction.user}"
        )
        log.info("role.created", name=name, role_id=role.id, by=interaction.user.id)
        await audit(
            guild,
            "role-create",
            actor=interaction.user,
            role=name,
            role_id=role.id,
            color=str(color),
        )
        await interaction.response.send_message(f"Created `{name}` ({color}).", ephemeral=True)

    @app_commands.command(
        name="role-bootstrap", description="Create the default Last Sietch role ladder."
    )
    @admin_only()
    async def role_bootstrap(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        created, skipped = [], []
        for name, color_int, hoist in DEFAULT_ROLES:
            if discord.utils.get(guild.roles, name=name) is not None:
                skipped.append(name)
                continue
            await guild.create_role(
                name=name, color=discord.Color(color_int), hoist=hoist, reason="Cielago bootstrap"
            )
            created.append(name)
        log.info("role.bootstrap", created=created, skipped=skipped, by=interaction.user.id)
        await audit(
            guild,
            "role-bootstrap",
            actor=interaction.user,
            created=created or "none",
            skipped=skipped or "none",
        )
        await interaction.followup.send(
            f"Created: {created or 'none'} | Skipped (existed): {skipped or 'none'}", ephemeral=True
        )

    @app_commands.command(name="role-list", description="List server roles with member counts.")
    @admin_only()
    async def role_list(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        lines = [
            f"{role.name} — {len(role.members)} member(s){' [managed]' if role.managed else ''}"
            for role in sorted(guild.roles, key=lambda r: r.position, reverse=True)
            if role.name != "@everyone"
        ]
        body = "\n".join(lines) or "No roles."
        await interaction.response.send_message(f"```\n{body[:1900]}\n```", ephemeral=True)

    @app_commands.command(name="role-assign", description="Assign a role to a member.")
    @app_commands.describe(member="Member to assign the role to", role="Role to assign")
    @admin_only()
    async def role_assign(
        self, interaction: discord.Interaction, member: discord.Member, role: discord.Role
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        if role >= guild.me.top_role:
            await interaction.response.send_message(
                f"I can't assign `{role.name}` — it sits at or above my highest role. "
                "Move the Cielago role above it.",
                ephemeral=True,
            )
            return
        if role in member.roles:
            await interaction.response.send_message(
                f"{member.mention} already has `{role.name}`.", ephemeral=True
            )
            return
        await member.add_roles(role, reason=f"Cielago: {interaction.user}")
        log.info("role.assigned", role=role.name, to=member.id, by=interaction.user.id)
        await audit(
            guild,
            "role-assign",
            actor=interaction.user,
            role=role.name,
            target=str(member),
            target_id=member.id,
        )
        await interaction.response.send_message(
            f"Assigned `{role.name}` to {member.mention}.", ephemeral=True
        )

    @app_commands.command(name="ping", description="Health check.")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("The spice flows.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
