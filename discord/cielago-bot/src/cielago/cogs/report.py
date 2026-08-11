"""Structured bug / feature intake — /report, and a pinned button that opens it.

WHY THIS EXISTS ALONGSIDE THE WATCHER. The Assistant cog watches channels and
files a ticket from whatever a player happened to type in chat. That catches
things nobody would have reported, but the ticket it produces carries only a
message: no in-game name, no idea whether the player means the portal or the
game, no server. Every one of those becomes a round trip in mod-ops before
anyone can act. This path asks for them up front.

The two paths are deliberately NOT merged. Passive detection is a safety net and
should stay cheap and forgiving; this is the deliberate front door and can ask
for structure because the player chose to open it. Tickets record which they
came from in `reported_via`, so the queue can tell a rich report from a scraped
one at a glance.

🔴 DISCORD CONSTRAINT THAT SHAPES ALL OF THIS: a modal can only be opened from
an interaction, and in discord.py 2.x a modal may contain TextInput and nothing
else. So there is no "react to the message and get a form" flow (a reaction is
not an interaction), and no dropdowns inside the form. Hence: a button (which IS
an interaction) opens a text-only modal, and the free-text answers are
normalised in assistant/intake.py rather than constrained in the UI.

The kind (bug vs feature) is carried by WHICH button was pressed, so the modal
does not have to waste one of its five fields asking.
"""

from __future__ import annotations

import time

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from cielago.assistant import intake
from cielago.assistant.store import Ticket

log = structlog.get_logger(__name__)

KIND_BUG = "bug"
KIND_FEATURE = "feature"

_KIND_META = {
    KIND_BUG: {"emoji": "\U0001F41E", "label": "Bug report", "category": "bug"},
    KIND_FEATURE: {"emoji": "\U0001F4A1", "label": "Feature request", "category": "feature"},
}

REPORTED_VIA = "report_modal"


class ReportModal(discord.ui.Modal):
    """Five text fields, the Discord maximum. Chosen so that every one of them
    either identifies the player, locates the problem, or describes it; nothing
    here is decoration.

    Only the description is required. A player who cannot remember their exact
    sietch must still be able to file: an incomplete report is worth far more
    than an abandoned one, and intake.normalise_* keeps blanks honest rather
    than inventing values.
    """

    def __init__(self, kind: str) -> None:
        meta = _KIND_META[kind]
        super().__init__(title=f"{meta['label']} · Last Sietch", timeout=900)
        self.kind = kind

        self.ingame_name = discord.ui.TextInput(
            label="Your in-game character name",
            placeholder="the name other players see",
            required=False, max_length=80,
        )
        self.surface = discord.ui.TextInput(
            label="Portal, in game, or both?",
            placeholder="portal / in game / both",
            required=False, max_length=40,
        )
        self.server = discord.ui.TextInput(
            label="Which server or map?",
            placeholder="Habbanya, Kulon-PvP, Deep Desert, or where it happened",
            required=False, max_length=120,
        )
        self.summary = discord.ui.TextInput(
            label="One-line summary",
            placeholder="what went wrong, in a few words",
            required=False, max_length=120,
        )
        self.description = discord.ui.TextInput(
            label="What happened?",
            style=discord.TextStyle.paragraph,
            placeholder="What you did, what you expected, what happened instead.",
            required=True, max_length=3500,
        )
        for item in (self.ingame_name, self.surface, self.server,
                     self.summary, self.description):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Reports")
        if cog is None:
            await interaction.response.send_message(
                "Reporting is unavailable right now. Please tell a mod directly.",
                ephemeral=True)
            return
        await cog.file_report(interaction, self)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        # Never leave the player staring at a spinner. They typed a real report;
        # losing it silently is the worst outcome here.
        log.warning("report.modal_error", error=str(error))
        msg = ("Something went wrong filing that. Nothing was saved, so please try "
               "again, or paste it in the channel and a mod will pick it up.")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.DiscordException:
            pass


class ReportLauncher(discord.ui.View):
    """The pinned message's buttons. `timeout=None` plus stable custom_ids so it
    keeps working after a bot restart instead of going dead and needing the
    message reposted."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Report a bug", emoji="\U0001F41E",
                       style=discord.ButtonStyle.danger,
                       custom_id="cielago:report:bug")
    async def bug(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ReportModal(KIND_BUG))

    @discord.ui.button(label="Request a feature", emoji="\U0001F4A1",
                       style=discord.ButtonStyle.success,
                       custom_id="cielago:report:feature")
    async def feature(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ReportModal(KIND_FEATURE))


class Reports(commands.Cog):
    """Owns the front door. Reads the Assistant cog's store rather than opening
    its own connection, so both intake paths land in one queue with one set of
    ids and the dedup pass sees everything."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Register the persistent view so the pinned buttons survive a restart.
        self.bot.add_view(ReportLauncher())

    # --- plumbing ---

    def _assistant(self):
        return self.bot.get_cog("Assistant")

    def _store(self):
        cog = self._assistant()
        return getattr(cog, "store", None) if cog else None

    # --- filing ---

    async def file_report(self, interaction: discord.Interaction, modal: ReportModal) -> None:
        store = self._store()
        if store is None:
            await interaction.response.send_message(
                "The support store is not loaded, so this was not saved. Please tell a mod.",
                ephemeral=True)
            log.warning("report.no_store")
            return

        meta = _KIND_META[modal.kind]
        ingame = intake.normalise_ingame_name(str(modal.ingame_name.value))
        surface = intake.normalise_surface(str(modal.surface.value))
        server = intake.normalise_server(str(modal.server.value))
        description = str(modal.description.value)
        title = intake.shape_title(modal.kind,
                                   str(modal.summary.value) or description)

        t = Ticket(
            id=0,
            created_at=int(time.time()),
            author_id=interaction.user.id,
            author_name=interaction.user.display_name,
            category=meta["category"],
            # Severity is deliberately NOT asked. Self-reported urgency is not
            # comparable between players and would page mods on someone's
            # judgement of their own problem; a mod sets it from the queue.
            severity="normal",
            title=title,
            body=intake.report_body(description),
            guild_id=interaction.guild.id if interaction.guild else None,
            channel_id=interaction.channel.id if interaction.channel else None,
            auto=False,
            ingame_name=ingame or None,
            surface=surface,
            server=server or None,
            reported_via=REPORTED_VIA,
        )

        assistant = self._assistant()
        vec, backend = None, None
        embedder = getattr(assistant, "embedder", None) if assistant else None
        if embedder is not None:
            try:
                # .embed() takes a LIST and returns a LIST, exactly as the watcher
                # calls it in Assistant._process_message. Getting this wrong does
                # not raise to the player: the ticket still files, silently with
                # no vector, and is then invisible to dedup forever.
                vec = embedder.embed([f"{title}\n{description}"])[0]
                backend = embedder.backend
            except Exception as exc:  # noqa: BLE001 - embedding is an optimisation
                log.warning("report.embed_failed", error=str(exc))

        store.add_ticket(t, embedding=vec, backend=backend)
        store.record_audit(str(interaction.user), "report-open", str(t.id),
                           kind=modal.kind, via=REPORTED_VIA)

        # Post to mod-ops through the Assistant's own poster so the embed, the
        # claim/resolve buttons and the mod ping all stay in ONE place. If that
        # is unavailable the ticket is still saved; say so honestly rather than
        # implying a mod has seen it.
        posted = False
        if assistant is not None and hasattr(assistant, "_post_ticket"):
            try:
                await assistant._post_ticket(t)
                posted = True
            except Exception as exc:  # noqa: BLE001
                log.warning("report.post_failed", ticket=t.id, error=str(exc))

        context = intake.summarise_for_mods(ingame, surface, server)
        lines = [f"{meta['emoji']} Filed as **#{t.id}**."]
        if context:
            lines.append(context)
        lines.append("A mod will pick it up from here."
                     if posted else
                     "Saved, but the mod channel did not accept it; please also mention it "
                     "to a mod so it is not missed.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        log.info("report.filed", ticket=t.id, kind=modal.kind, surface=surface,
                 posted=posted)

    # --- commands ---

    @app_commands.command(name="report",
                          description="Report a bug or request a feature for Last Sietch")
    @app_commands.describe(kind="What kind of report is this?")
    @app_commands.choices(kind=[
        app_commands.Choice(name="Bug", value=KIND_BUG),
        app_commands.Choice(name="Feature request", value=KIND_FEATURE),
    ])
    async def report(self, interaction: discord.Interaction,
                     kind: app_commands.Choice[str]) -> None:
        await interaction.response.send_modal(ReportModal(kind.value))

    @app_commands.command(name="reportpanel",
                          description="Post the pinned report panel in this channel")
    @app_commands.default_permissions(manage_guild=True)
    async def reportpanel(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Found a bug? Want something added?",
            description=(
                "Use the buttons below. You will get a short form asking for your "
                "character name, whether it is the Player Portal or in game, and what "
                "happened.\n\n"
                "Only the description is required, so file it even if you cannot "
                "remember every detail. You can also type `/report` anywhere."
            ),
            color=0xD4A574,
        )
        embed.set_footer(text="Cielago Assistant")
        await interaction.response.send_message(embed=embed, view=ReportLauncher())
        log.info("report.panel_posted", channel=getattr(interaction.channel, "id", None))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reports(bot))
