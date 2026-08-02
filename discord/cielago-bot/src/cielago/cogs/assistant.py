"""Cielago Assistant — mod-facing support triage (Phase 0/1, no external LLM).

Watches the help channels, runs each substantive message through a deterministic
keyword classifier + a local-embedding dedup pass, and either:

  * bumps an existing open ticket / feature request (a near-duplicate), or
  * files a new ticket to the mod channel with a Claim / Escalate / Resolve /
    Merge lifecycle, or a new feature request with 👍 upvote aggregation.

Urgent items (server-down / exploit / harassment) ping the mods on arrival.
All state lives in support.sqlite; the bot never writes game state. Supersedes
the old tracker cog (its JSON is migrated in on first load).

Config (cielago.config.settings):
  CIELAGO_ASSISTANT_ENABLED            master on/off (kill switch also via /assistant)
  CIELAGO_ASSISTANT_DB_PATH            support.sqlite path
  CIELAGO_ASSISTANT_MOD_CHANNEL_ID     where ticket/FR embeds are posted
  CIELAGO_ASSISTANT_WATCH_CHANNEL_IDS  CSV of channels to watch (default: feedback)
  CIELAGO_ASSISTANT_OWNER_ID           pinged on Escalate-to-owner
  CIELAGO_ASSISTANT_MOD_ROLE_ID        pinged on urgent items
  CIELAGO_ASSISTANT_EMBED_MODEL_DIR    bge-small ONNX dir (falls back to hashing)
  CIELAGO_ASSISTANT_DUP_THRESHOLD      cosine dup cutoff override (else backend default)
  CIELAGO_ASSISTANT_SWEEP_MAX_AGE_HOURS  how far back the nightly sweep reads (0 = no bound)
"""

from __future__ import annotations
import os

import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import structlog
from discord import app_commands
from discord.ext import commands, tasks

from cielago.assistant import store as S
from cielago.assistant.classify import (
    CAT_FEATURE,
    CATEGORY_META,
    classify_category,
    classify_severity,
    is_actionable,
    looks_like_report,
    summarize_title,
)
from cielago.assistant.dedup import best_match
from cielago.assistant.embeddings import load_embedder
from cielago.assistant.migrate import migrate_tracker_json
from cielago.assistant.store import FeatureRequest, SupportStore, Ticket
from cielago.audit import audit
from cielago.config import settings
from cielago.permissions import is_admin

log = structlog.get_logger()

VOTE_EMOJI = "\U0001F44D"  # 👍
_SWEEP_SCAN = 200


def sweep_cutoff(max_age_hours: int) -> datetime | None:
    """Oldest message the nightly sweep will look at. None means no bound."""
    if max_age_hours <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(hours=max_age_hours)


async def newer_than(history, cutoff: datetime | None):
    """Yield from a NEWEST-FIRST history until one message predates `cutoff`.

    Stops rather than filters. Discord returns history newest-first, so the first
    message past the cutoff means every remaining one is older still, and a quiet
    channel's page stops costing anything the moment it goes stale. A None cutoff
    yields the whole page, which is the pre-guard behaviour.
    """
    async for message in history:
        if cutoff is not None and message.created_at < cutoff:
            return
        yield message
_DESC_LIMIT = 1000

# --- player-facing acknowledgement ------------------------------------------
# Filing used to be SILENT to the reporter: the card went to the mod channel and the
# player saw nothing, so a correctly-filed report was indistinguishable from being
# ignored. Same failure shape as the welcome-pack offline gate -- the system behaves
# correctly and looks broken -- and it is why three separate players re-reported
# BUG-011 (2026-07-24) instead of trusting that the first report had landed.
ACK_EMOJI = "\U0001F4DD"          # 📝 filed
ACK_DUP_EMOJI = "\U0001F517"      # 🔗 merged into an existing report
ACK_TEXT = {
    "bug": "Logged as **#{id}** and queued for admin processing. Thanks for the report.",
    "question": "Logged as **#{id}** and queued for an admin to answer.",
    CAT_FEATURE: ("Logged as **#{id}** and queued for review. "
                  "Others can react 👍 to add weight to it."),
}
ACK_TEXT_DEFAULT = "Logged as **#{id}** and queued for admin processing."
ACK_TEXT_DUP = ("Already tracked as **#{id}** — your report has been added to it, which "
                "bumps its priority. Thanks for flagging it.")

SEV_COLOR = {
    "urgent": 0xCC2B2B,
    "high": 0xD9822B,
    "normal": 0xD4A574,
    "low": 0x6E8B5A,
}
STATUS_LABEL = {
    S.TICKET_OPEN: "🟡 Open",
    S.TICKET_CLAIMED: "🔵 Claimed",
    S.TICKET_RESOLVED: "🟢 Resolved",
    S.TICKET_ESCALATED: "🔴 Escalated",
    S.TICKET_MERGED: "⚪ Merged",
    S.TICKET_DISREGARDED: "🚫 Disregarded",
}

# The Cielago Ops agent (Claude channels bridge). New ticket/FR cards mention it so
# the ops session is auto-notified via the bridge and can work the request. 0 disables.
OPS_AGENT_ID = int(os.environ.get("OPS_AGENT_ID", "0"))


def _ops_mention() -> str:
    return f"<@{OPS_AGENT_ID}>" if OPS_AGENT_ID else ""


def _ops_summary(kind: str, item_id: int, category: str | None,
                 channel_id: int | None, jump_url: str | None) -> str:
    """One-line plain-text digest of a card, for the message `content`.

    The card itself is a rich embed, but the Claude bridge that feeds the ops
    session surfaces a message's `content` only — embeds are invisible to it.
    Without this line the ops agent gets a bare mention and has to query
    support.sqlite just to learn which ticket fired and where it came from.

    The channel renders as a clickable mention for humans; the jump URL is
    wrapped in <> so Discord suppresses the link preview and the card stays
    compact. Nothing here changes what the embed shows.
    """
    bits = [f"{kind} #{item_id}"]
    if category:
        bits.append(category)
    if channel_id:
        bits.append(f"<#{channel_id}>")
    if jump_url:
        bits.append(f"<{jump_url}>")
    return " · ".join(bits)


# --- embeds ---


def build_ticket_embed(t: Ticket) -> discord.Embed:
    meta = CATEGORY_META.get(t.category, {"emoji": "📌", "label": t.category.title()})
    embed = discord.Embed(
        title=f"{meta['emoji']} Ticket #{t.id} · {meta['label']}",
        description=t.title[:_DESC_LIMIT],
        color=SEV_COLOR.get(t.severity, 0xD4A574),
    )
    embed.add_field(name="Reporter", value=t.author_name, inline=True)
    embed.add_field(name="Severity", value=t.severity.title(), inline=True)
    embed.add_field(name="Status", value=STATUS_LABEL.get(t.status, t.status), inline=True)
    if t.claimed_by_name:
        embed.add_field(name="Claimed by", value=t.claimed_by_name, inline=True)
    if t.dedup_count > 1:
        embed.add_field(name="Reports", value=f"×{t.dedup_count}", inline=True)
    url = t.jump_url()
    if url:
        embed.add_field(name="Source", value=f"[jump to message]({url})", inline=False)
    if t.resolution:
        embed.add_field(name="Resolution", value=t.resolution[:1000], inline=False)
    embed.set_footer(text="Cielago Assistant · mod actions below")
    return embed


def build_fr_embed(fr: FeatureRequest) -> discord.Embed:
    embed = discord.Embed(
        title=f"💡 Feature Request #{fr.id}",
        description=fr.title[:_DESC_LIMIT],
        color=0x6E8B5A,
    )
    embed.add_field(name="Requested by", value=fr.author_name, inline=True)
    embed.add_field(name="Votes", value=f"👍 {fr.votes}", inline=True)
    embed.add_field(name="Status", value=fr.status.title(), inline=True)
    embed.set_footer(text=f"React {VOTE_EMOJI} to upvote")
    return embed


# --- restart-surviving lifecycle buttons (DynamicItem, keyed by ticket id) ---


class _LifecycleButton(discord.ui.DynamicItem[discord.ui.Button], template=r""):
    """Base: parse the ticket id out of the custom_id and dispatch to the cog."""

    action = ""
    label = ""
    style = discord.ButtonStyle.secondary

    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(
            discord.ui.Button(
                label=self.label,
                style=self.style,
                custom_id=f"assist:{self.action}:{ticket_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["tid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Assistant")
        if cog is None:
            await interaction.response.send_message(
                "The assistant is unavailable right now.", ephemeral=True
            )
            return
        await cog.handle_action(interaction, self.action, self.ticket_id)


class ClaimButton(_LifecycleButton, template=r"assist:claim:(?P<tid>\d+)"):
    action = "claim"
    label = "Claim"
    style = discord.ButtonStyle.primary


class EscalateButton(_LifecycleButton, template=r"assist:escalate:(?P<tid>\d+)"):
    action = "escalate"
    label = "Escalate to owner"
    style = discord.ButtonStyle.danger


class ResolveButton(_LifecycleButton, template=r"assist:resolve:(?P<tid>\d+)"):
    action = "resolve"
    label = "Resolve"
    style = discord.ButtonStyle.success


class MergeButton(_LifecycleButton, template=r"assist:merge:(?P<tid>\d+)"):
    action = "merge"
    label = "Merge"
    style = discord.ButtonStyle.secondary


class DisregardButton(_LifecycleButton, template=r"assist:disregard:(?P<tid>\d+)"):
    action = "disregard"
    label = "Disregard"
    style = discord.ButtonStyle.secondary


def lifecycle_view(ticket_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ClaimButton(ticket_id))
    view.add_item(EscalateButton(ticket_id))
    view.add_item(ResolveButton(ticket_id))
    view.add_item(MergeButton(ticket_id))
    view.add_item(DisregardButton(ticket_id))
    return view


class MergeModal(discord.ui.Modal, title="Merge ticket"):
    target = discord.ui.TextInput(
        label="Merge into ticket # (the canonical one)",
        placeholder="e.g. 12",
        required=True,
        max_length=8,
    )

    def __init__(self, source_id: int) -> None:
        super().__init__()
        self.source_id = source_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Assistant")
        if cog is None:
            await interaction.response.send_message("Unavailable.", ephemeral=True)
            return
        raw = str(self.target.value).strip().lstrip("#")
        if not raw.isdigit():
            await interaction.response.send_message("That isn't a ticket number.", ephemeral=True)
            return
        await cog.do_merge(interaction, self.source_id, int(raw))


# --- cog ---


class Assistant(commands.Cog):
    assistant = app_commands.Group(
        name="assistant",
        description="Cielago Assistant controls",
        default_permissions=discord.Permissions(administrator=True),
    )
    ticket = app_commands.Group(
        name="ticket",
        description="Browse the support queue",
        parent=assistant,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = SupportStore(settings.cielago_assistant_db_path)
        self.embedder = None
        self._disabled_marker = Path(settings.cielago_assistant_db_path + ".disabled")
        self.enabled = settings.cielago_assistant_enabled and not self._disabled_marker.exists()

    async def cog_load(self) -> None:
        self.store.connect()
        self.embedder = load_embedder(
            settings.cielago_assistant_embed_model_dir, prefer_onnx=True
        )
        try:
            migrate_tracker_json(self.store, self.embedder, settings.cielago_tracker_data_path)
        except Exception:
            log.warning("assistant.migrate_failed", exc_info=True)
        if self._watch_ids() and self._mod_channel_id():
            self._sweep.start()

    async def cog_unload(self) -> None:
        self._sweep.cancel()
        self.store.close()

    # --- config helpers ---

    def _dup_threshold(self) -> float:
        override = settings.cielago_assistant_dup_threshold
        if override is not None:
            return override
        return self.embedder.dup_threshold if self.embedder else 0.85

    def _watch_ids(self) -> set[int]:
        return settings.assistant_watch_channel_ids

    def _ack_ids(self) -> set[int]:
        return settings.assistant_ack_channel_ids

    def _mod_channel_id(self) -> int | None:
        return settings.cielago_assistant_mod_channel_id

    def _mod_channel(self) -> discord.TextChannel | None:
        cid = self._mod_channel_id()
        if not cid:
            return None
        ch = self.bot.get_channel(cid)
        return ch if isinstance(ch, discord.TextChannel) else None

    # --- core pipeline ---

    async def _process_message(self, message: discord.Message, live: bool = True) -> None:
        if not self.enabled or self.embedder is None:
            return
        if message.author.bot or not message.guild:
            return
        # Staff replies are not reports. Admins answering players in a watched
        # channel kept opening tickets about themselves: the keyword classifier
        # sees "bug" in "these are the bugs we need to polish out" and files it,
        # and ordinary conversational phrasing ("can't", "doesn't") trips it too.
        # Two of those had to be disregarded by hand on 2026-07-24 alone.
        if is_admin(message.author.id):
            return
        if message.channel.id not in self._watch_ids():
            return
        # Filing is server-wide; SPEAKING is not. The ack is a reaction plus a
        # quote-reply, so in a general-chat channel it turns every stray "this is
        # broken" into a bot interruption in the middle of a conversation -- those
        # channels were designed for players talking to each other. Monitor them
        # silently: the ticket still opens and still surfaces in mod-ops, the
        # reporter just isn't answered by a bot in a room meant for people.
        if live and message.channel.id not in self._ack_ids():
            live = False
        content = message.content or ""
        if not looks_like_report(content):
            return
        if message.id in self.store.processed_message_ids():
            return
        category = classify_category(content)
        if not is_actionable(category):
            return

        title = summarize_title(content)
        vec = self.embedder.embed([content])[0]
        if category == CAT_FEATURE:
            await self._handle_feature(message, title, content, vec, live=live)
        else:
            severity = classify_severity(content, category)
            await self._handle_ticket(message, category, severity, title, content, vec, live=live)

    async def _handle_ticket(self, message, category, severity, title, body, vec,
                             live: bool = True) -> None:
        cands = self.store.candidate_vectors(
            "tickets", self.embedder.backend, len(vec), S.OPEN_TICKET_STATES
        )
        match = best_match(vec, cands, self._dup_threshold())
        if match is not None:
            tid, score = match
            self.store.bump_ticket_dedup(tid)
            self.store.record_audit(str(message.author), "ticket-dup", str(tid),
                                    score=round(score, 3))
            await self._refresh_ticket(tid)
            if live:
                await self._ack_player(message, tid, category, dup=True)
            log.info("assistant.ticket_dup", ticket=tid, score=round(score, 3))
            return

        t = Ticket(
            id=0, created_at=int(time.time()), author_id=message.author.id,
            author_name=message.author.display_name, category=category, severity=severity,
            title=title, body=body[:4000], guild_id=message.guild.id,
            channel_id=message.channel.id, message_id=message.id, auto=True,
        )
        self.store.add_ticket(t, embedding=vec, backend=self.embedder.backend)
        await self._post_ticket(t)
        if live:
            await self._ack_player(message, t.id, category)
        self.store.record_audit(str(message.author), "ticket-open", str(t.id),
                                category=category, severity=severity)
        log.info("assistant.ticket_open", ticket=t.id, category=category, severity=severity)

    async def _handle_feature(self, message, title, body, vec, live: bool = True) -> None:
        cands = self.store.candidate_vectors(
            "feature_requests", self.embedder.backend, len(vec), S.OPEN_FR_STATES
        )
        match = best_match(vec, cands, self._dup_threshold())
        if match is not None:
            fid, score = match
            votes = self.store.add_vote(fid, 1)
            self.store.record_audit(str(message.author), "fr-dup", str(fid), score=round(score, 3))
            await self._refresh_fr(fid)
            if live:
                await self._ack_player(message, fid, CAT_FEATURE, dup=True)
            log.info("assistant.fr_dup", fr=fid, votes=votes, score=round(score, 3))
            return

        fr = FeatureRequest(
            id=0, created_at=int(time.time()), author_id=message.author.id,
            author_name=message.author.display_name, title=title, body=body[:4000],
            guild_id=message.guild.id, channel_id=message.channel.id, message_id=message.id,
        )
        self.store.add_feature_request(fr, embedding=vec, backend=self.embedder.backend)
        await self._post_fr(fr)
        if live:
            await self._ack_player(message, fr.id, CAT_FEATURE)
        self.store.record_audit(str(message.author), "fr-open", str(fr.id))
        log.info("assistant.fr_open", fr=fr.id)

    # --- player-facing acknowledgement ---

    async def _ack_player(self, message: discord.Message, item_id: int,
                          category: str | None, dup: bool = False) -> None:
        """Tell the REPORTER their message was filed: a reaction plus a short reply
        quoting the id.

        Both, not either. The reaction is instant and survives a busy channel; the
        reply carries the id so the player can refer to it later and can see their
        report was read rather than swallowed.

        Best-effort by design. A missing Add Reactions / Send Messages permission, a
        deleted message, or a rate limit must never take down filing -- the ticket is
        already committed to support.sqlite by the time we get here, so an ack failure
        is cosmetic and is logged, not raised.

        🔴 ONLY ever reached with live=True, i.e. from on_message. The nightly
        `_sweep` re-reads 200 messages of channel HISTORY through the same
        `_process_message`, so acking from there replies to messages players wrote weeks
        ago. Not hypothetical: on 2026-07-25 the 23:50 sweep filed ~74 tickets in 80
        seconds and acked every one across four community channels, including an urgent
        mod ping for a weeks-old message. The flag is threaded explicitly rather than
        inferred from message age -- "how old is too old" is a guess, and the caller
        already knows the answer for certain.
        """
        try:
            await message.add_reaction(ACK_DUP_EMOJI if dup else ACK_EMOJI)
        except discord.DiscordException as exc:
            log.warning("assistant.ack_reaction_failed", item=item_id, error=str(exc))
        body = (ACK_TEXT_DUP if dup
                else ACK_TEXT.get(category or "", ACK_TEXT_DEFAULT)).format(id=item_id)
        try:
            # Quote-reply so it threads under their own message, and suppress the ping:
            # they are already looking at the channel they just posted in, and an
            # every-report mention reads as noise rather than service.
            await message.reply(body, mention_author=False)
        except discord.DiscordException as exc:
            log.warning("assistant.ack_reply_failed", item=item_id, error=str(exc))

    # --- posting / refreshing mod-channel embeds ---

    async def _post_ticket(self, t: Ticket) -> None:
        channel = self._mod_channel()
        if channel is None:
            log.warning("assistant.no_mod_channel")
            return
        urgent = self._urgent_mention(channel.guild) if t.severity == "urgent" else ""
        summary = _ops_summary("Ticket", t.id, t.category, t.channel_id, t.jump_url())
        content = " ".join(x for x in (_ops_mention(), urgent, summary) if x) or None
        try:
            msg = await channel.send(content=content, embed=build_ticket_embed(t),
                                     view=lifecycle_view(t.id))
            self.store.update_ticket(t.id, mod_message_id=msg.id)
        except discord.DiscordException:
            log.warning("assistant.ticket_post_failed", ticket=t.id, exc_info=True)

    async def _post_fr(self, fr: FeatureRequest) -> None:
        channel = self._mod_channel()
        if channel is None:
            return
        summary = _ops_summary("FR", fr.id, None, fr.channel_id, fr.jump_url())
        content = " ".join(x for x in (_ops_mention(), summary) if x) or None
        try:
            msg = await channel.send(content=content, embed=build_fr_embed(fr))
            self.store.update_feature_request(fr.id, mod_message_id=msg.id)
            await msg.add_reaction(VOTE_EMOJI)
        except discord.DiscordException:
            log.warning("assistant.fr_post_failed", fr=fr.id, exc_info=True)

    async def _refresh_ticket(self, ticket_id: int) -> None:
        t = self.store.get_ticket(ticket_id)
        channel = self._mod_channel()
        if t is None or channel is None or not t.mod_message_id:
            return
        active = t.status in S.OPEN_TICKET_STATES
        try:
            msg = await channel.fetch_message(t.mod_message_id)
            await msg.edit(embed=build_ticket_embed(t),
                           view=lifecycle_view(t.id) if active else None)
        except discord.NotFound:
            self.store.update_ticket(t.id, mod_message_id=None)
        except discord.DiscordException:
            log.warning("assistant.ticket_refresh_failed", ticket=ticket_id, exc_info=True)

    async def _refresh_fr(self, fr_id: int) -> None:
        fr = self.store.get_feature_request(fr_id)
        channel = self._mod_channel()
        if fr is None or channel is None or not fr.mod_message_id:
            return
        try:
            msg = await channel.fetch_message(fr.mod_message_id)
            await msg.edit(embed=build_fr_embed(fr))
        except discord.NotFound:
            self.store.update_feature_request(fr.id, mod_message_id=None)
        except discord.DiscordException:
            log.warning("assistant.fr_refresh_failed", fr=fr_id, exc_info=True)

    def _urgent_mention(self, guild: discord.Guild) -> str:
        role_id = settings.cielago_assistant_mod_role_id
        if role_id:
            return f"<@&{role_id}> 🚨 urgent"
        admins = " ".join(f"<@{a}>" for a in settings.admin_ids)
        return f"{admins} 🚨 urgent".strip() or "🚨 urgent"

    # --- button handlers ---

    async def handle_action(
        self, interaction: discord.Interaction, action: str, ticket_id: int
    ) -> None:
        if not is_admin(interaction.user.id):
            await interaction.response.send_message(
                "Only the Naib's cielago may invoke that.", ephemeral=True
            )
            return
        t = self.store.get_ticket(ticket_id)
        if t is None:
            await interaction.response.send_message(f"No ticket #{ticket_id}.", ephemeral=True)
            return

        if action == "merge":
            await interaction.response.send_modal(MergeModal(ticket_id))
            return

        if action == "claim":
            self.store.update_ticket(ticket_id, status=S.TICKET_CLAIMED,
                                     claimed_by=interaction.user.id,
                                     claimed_by_name=interaction.user.display_name)
            note = f"Claimed by {interaction.user.display_name}."
        elif action == "resolve":
            self.store.update_ticket(ticket_id, status=S.TICKET_RESOLVED,
                                     resolution=f"Resolved by {interaction.user.display_name}")
            note = f"#{ticket_id} resolved."
        elif action == "escalate":
            self.store.update_ticket(ticket_id, status=S.TICKET_ESCALATED)
            note = "Escalated to the owner."
            await self._ping_owner(interaction, t)
        elif action == "disregard":
            self.store.update_ticket(
                ticket_id, status=S.TICKET_DISREGARDED,
                resolution=f"Disregarded as a false positive by {interaction.user.display_name}")
            note = f"#{ticket_id} disregarded (false positive)."
        else:
            await interaction.response.send_message("Unknown action.", ephemeral=True)
            return

        self.store.record_audit(str(interaction.user), f"ticket-{action}", str(ticket_id))
        await audit(interaction.guild, f"assist-{action}", actor=interaction.user, ticket=ticket_id)
        await self._refresh_ticket(ticket_id)
        if not interaction.response.is_done():
            await interaction.response.send_message(note, ephemeral=True)

    async def _ping_owner(self, interaction: discord.Interaction, t: Ticket) -> None:
        channel = self._mod_channel()
        if channel is None:
            return
        owner = settings.cielago_assistant_owner_id
        who = f"<@{owner}>" if owner else " ".join(f"<@{a}>" for a in settings.admin_ids)
        url = t.jump_url()
        tail = f" — [source]({url})" if url else ""
        try:
            await channel.send(
                f"{who} ⛏️ Ticket **#{t.id}** escalated ({t.severity}): {t.title}{tail}"
            )
        except discord.DiscordException:
            log.warning("assistant.escalate_ping_failed", ticket=t.id, exc_info=True)

    async def do_merge(
        self, interaction: discord.Interaction, source_id: int, target_id: int
    ) -> None:
        if source_id == target_id:
            await interaction.response.send_message(
                "Can't merge a ticket into itself.", ephemeral=True
            )
            return
        target = self.store.get_ticket(target_id)
        if target is None:
            await interaction.response.send_message(
                f"No ticket #{target_id} to merge into.", ephemeral=True
            )
            return
        self.store.update_ticket(source_id, status=S.TICKET_MERGED, dedup_into=target_id,
                                 resolution=f"Merged into #{target_id}")
        self.store.bump_ticket_dedup(target_id)
        self.store.record_audit(str(interaction.user), "ticket-merge", str(source_id),
                                into=target_id)
        await audit(interaction.guild, "assist-merge", actor=interaction.user,
                    ticket=source_id, into=target_id)
        await self._refresh_ticket(source_id)
        await self._refresh_ticket(target_id)
        await interaction.response.send_message(
            f"Merged #{source_id} into #{target_id}.", ephemeral=True
        )

    # --- feature-request vote reactions ---

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_vote(payload, +1)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_vote(payload, -1)

    async def _handle_vote(self, payload: discord.RawReactionActionEvent, delta: int) -> None:
        if str(payload.emoji) != VOTE_EMOJI:
            return
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        fr = self.store.fr_by_mod_message(payload.message_id)
        if fr is None:
            return
        self.store.add_vote(fr.id, delta)
        await self._refresh_fr(fr.id)

    # --- message watcher ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self._process_message(message)
        except Exception:
            log.warning("assistant.process_failed", message_id=message.id, exc_info=True)

    # --- slash: queue browsing ---

    @ticket.command(name="list", description="List open support tickets")
    async def ticket_list(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        items = self.store.list_tickets()
        if not items:
            await interaction.response.send_message("No open tickets. The sietch is at peace.",
                                                    ephemeral=True)
            return
        lines = [
            f"`#{t.id}` [{t.severity}] {t.title} — {t.author_name}"
            + (f" ×{t.dedup_count}" if t.dedup_count > 1 else "")
            for t in items
        ]
        await interaction.response.send_message(
            "**Open tickets:**\n" + "\n".join(lines)[:1900], ephemeral=True
        )

    @ticket.command(name="show", description="Show one ticket")
    @app_commands.describe(ticket_id="Ticket number")
    async def ticket_show(self, interaction: discord.Interaction, ticket_id: int) -> None:
        if not await self._guard(interaction):
            return
        t = self.store.get_ticket(ticket_id)
        if t is None:
            await interaction.response.send_message(f"No ticket #{ticket_id}.", ephemeral=True)
            return
        await interaction.response.send_message(embed=build_ticket_embed(t), ephemeral=True)

    @ticket.command(name="close", description="Resolve a ticket by number")
    @app_commands.describe(ticket_id="Ticket number", resolution="Optional note")
    async def ticket_close(self, interaction: discord.Interaction, ticket_id: int,
                           resolution: str | None = None) -> None:
        if not await self._guard(interaction):
            return
        t = self.store.get_ticket(ticket_id)
        if t is None:
            await interaction.response.send_message(f"No ticket #{ticket_id}.", ephemeral=True)
            return
        self.store.update_ticket(
            ticket_id, status=S.TICKET_RESOLVED,
            resolution=resolution or f"Resolved by {interaction.user.display_name}",
        )
        self.store.record_audit(str(interaction.user), "ticket-resolve", str(ticket_id))
        await audit(interaction.guild, "assist-resolve", actor=interaction.user, ticket=ticket_id)
        await self._refresh_ticket(ticket_id)
        await interaction.response.send_message(f"Resolved #{ticket_id}.", ephemeral=True)

    # --- slash: assistant control ---

    @assistant.command(name="status", description="Assistant health + queue counts")
    async def assistant_status(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        c = self.store.counts()
        backend = self.embedder.backend if self.embedder else "none"
        lines = [
            f"**Enabled:** {'yes' if self.enabled else 'no (kill switch)'}",
            f"**Embedder:** {backend} (dup ≥ {self._dup_threshold():.2f})",
            f"**Open tickets:** {c['open_tickets']}",
            f"**Resolved:** {c['resolved_tickets']}",
            f"**Open feature requests:** {c['open_feature_requests']}",
            f"**Watching:** {len(self._watch_ids())} channel(s)",
            f"**Mod channel:** {'set' if self._mod_channel_id() else 'UNSET — surfacing disabled'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @assistant.command(name="enable", description="Enable auto-triage (clear the kill switch)")
    async def assistant_enable(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        self.enabled = True
        self._disabled_marker.unlink(missing_ok=True)
        await audit(interaction.guild, "assist-enable", actor=interaction.user)
        await interaction.response.send_message(
            "Assistant auto-triage **enabled**.", ephemeral=True
        )

    @assistant.command(name="disable", description="Kill switch: stop auto-triage")
    async def assistant_disable(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        self.enabled = False
        try:
            self._disabled_marker.parent.mkdir(parents=True, exist_ok=True)
            self._disabled_marker.write_text("disabled\n", encoding="utf-8")
        except OSError:
            log.warning("assistant.killswitch_persist_failed", exc_info=True)
        await audit(interaction.guild, "assist-disable", actor=interaction.user)
        await interaction.response.send_message(
            "Assistant auto-triage **disabled**. Existing tickets stay; no new ones are filed.",
            ephemeral=True,
        )

    @assistant.command(name="features", description="Top open feature requests by votes")
    async def assistant_features(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        frs = self.store.list_feature_requests()
        if not frs:
            await interaction.response.send_message("No open feature requests.", ephemeral=True)
            return
        lines = [f"`#{fr.id}` 👍{fr.votes} — {fr.title} ({fr.author_name})" for fr in frs[:25]]
        await interaction.response.send_message(
            "**Top feature requests:**\n" + "\n".join(lines)[:1900], ephemeral=True
        )

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user.id):
            await interaction.response.send_message(
                "Only the Naib's cielago may invoke that.", ephemeral=True
            )
            return False
        return True

    # --- daily sweep of watched channels ---

    @tasks.loop(time=dtime(hour=23, minute=50, tzinfo=ZoneInfo("America/New_York")))
    async def _sweep(self) -> None:
        """Nightly safety net: re-read recent history so nothing on_message missed
        stays missed (a bot restart, a Discord outage, a classifier fix like the
        past-tense patch that made three swallowed reports actionable).

        Bounded on BOTH axes. `_SWEEP_SCAN` bounds how many messages, and
        `sweep_max_age_hours` bounds how far back in time -- without the second
        one, a quiet channel's 200 messages reach back a month and the sweep
        re-litigates conversations everyone has moved on from. On 2026-07-26 it
        ran across messages 4 to 26 days old for exactly that reason.

        The age bound is not what keeps the sweep quiet -- `live=False` is, and it
        held on 07-26 with zero player-facing output. This bounds the mod-side
        noise: stale tickets in mod-ops that nobody can action any more.
        """
        if not self.enabled:
            return
        max_age = settings.cielago_assistant_sweep_max_age_hours
        cutoff = sweep_cutoff(max_age)
        for cid in self._watch_ids():
            channel = self.bot.get_channel(cid)
            if not isinstance(channel, discord.TextChannel):
                continue
            scanned = 0
            try:
                async for message in newer_than(
                    channel.history(limit=_SWEEP_SCAN), cutoff
                ):
                    scanned += 1
                    # live=False: the sweep re-reads HISTORY, so acking from here
                    # replies to messages players have already moved on from. Did
                    # exactly that on 2026-07-25 -- ~74 tickets filed in 80s, each
                    # one pinging a stale message across four community channels.
                    await self._process_message(message, live=False)
            except discord.DiscordException:
                log.warning("assistant.sweep_failed", channel=cid, exc_info=True)
            else:
                log.info("assistant.sweep_channel", channel=cid, scanned=scanned)
        log.info("assistant.sweep_done", max_age_hours=max_age,
                 open=len(self.store.list_tickets()))

    @_sweep.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(ClaimButton, EscalateButton, ResolveButton, MergeButton,
                          DisregardButton)
    await bot.add_cog(Assistant(bot))
