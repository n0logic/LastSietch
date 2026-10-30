"""Server event contests in 🏆｜server-events: a pinned panel with a Submit entry
button, bot-posted entry cards with a Vote button, a schedule that opens and
closes itself, and staff-confirmed payouts.

The rules, schedule, payout plan and copy live in cielago/contests.py; this file
is only the Discord surface. Every post here uses NO_PINGS: entrant names are
display names, never mentions, and no post can mass-ping the server.

Restart survival: the panel button is a persistent view (stable custom_id) and
the Vote / Confirm buttons are DynamicItems whose custom_id carries the contest
and entrant, so cards posted before a restart keep working.
"""

from __future__ import annotations

import asyncio
import io
import time
from datetime import UTC, datetime
from importlib.resources import files

import discord
import httpx
import structlog
from discord import app_commands
from discord.ext import commands, tasks

from cielago import contests as cx
from cielago.audit import audit
from cielago.config import settings
from cielago.permissions import is_admin

log = structlog.get_logger()

NO_PINGS = discord.AllowedMentions.none()
COLOR = 0xD4A574
STAFF_COLOR = 0xA4441F
VOTE_EMOJI = "\U0001f3ee"
STAFF_REPORT_ROWS = 10


def is_staff(user) -> bool:
    if is_admin(user.id):
        return True
    role_id = settings.cielago_assistant_mod_role_id
    if role_id and any(r.id == role_id for r in getattr(user, "roles", [])):
        return True
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


def entry_embed(c: cx.Contest, e: cx.Entry) -> discord.Embed:
    # Author names render no markdown, so the display name goes in as typed.
    embed = discord.Embed(title=cx.safe(e.title), description=cx.safe(e.caption) or None,
                          color=COLOR)
    embed.set_author(name=e.display_name)
    if e.image_name:
        embed.set_image(url=f"attachment://{e.image_name}")
    embed.set_footer(text=f"{c.event_label}: {c.title}")
    return embed


def staff_report_embed(c: cx.Contest) -> discord.Embed:
    rows = cx.tally(c)
    lines = []
    for i, (eid, n) in enumerate(rows[:STAFF_REPORT_ROWS], start=1):
        e = c.entries[eid]
        noun = "vote" if n == 1 else "votes"
        lines.append(f"{i}. **{cx.safe(e.title)}** by {cx.safe(e.display_name)} (<@{eid}>): "
                     f"{n} {noun}")
    embed = discord.Embed(title=f"Contest results to confirm: {c.title}",
                          description="\n".join(lines) or "No entries.", color=STAFF_COLOR)
    plan = cx.payout_plan(c)
    places = cx.winners(c)
    embed.add_field(name="Entries", value=str(len(c.entries)), inline=True)
    embed.add_field(name="Voters", value=str(len(c.votes)), inline=True)
    embed.add_field(name="Planned payout",
                    value=f"{sum(p['amount'] for p in plan)} {cx.LANES[c.reward_lane]['unit']} "
                          f"over {len(plan)} cases", inline=True)
    if places and len(rows) > len(places) and rows[len(places)][1] == places[-1][2]:
        embed.add_field(name="Tie", value="The last paid place is tied; the earlier entry wins "
                        "the tie. Remove an entry with /contest remove to change the order.",
                        inline=False)
    embed.set_footer(text=f"Nothing is paid until staff press Confirm (contest {c.id}).")
    return embed


class EntryModal(discord.ui.Modal):
    def __init__(self, contest_id: str) -> None:
        super().__init__(title="Submit your entry", timeout=900)
        self.contest_id = contest_id
        self.image = discord.ui.FileUpload(custom_id="cielago:contest:image",
                                           min_values=1, max_values=1, required=True)
        self.entry_title = discord.ui.TextInput(custom_id="cielago:contest:title",
                                                max_length=cx.TITLE_MAX, required=True,
                                                placeholder="Name your entry")
        self.caption = discord.ui.TextInput(custom_id="cielago:contest:caption",
                                            style=discord.TextStyle.paragraph,
                                            max_length=cx.CAPTION_MAX, required=False)
        self.add_item(discord.ui.Label(text="Your screenshot", description="One image",
                                       component=self.image))
        self.add_item(discord.ui.Label(text="Entry title", description="Up to 60 characters",
                                       component=self.entry_title))
        self.add_item(discord.ui.Label(text="Caption (optional)",
                                       description="Up to 200 characters", component=self.caption))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Contests")
        if cog is None:
            await interaction.response.send_message(cx.entry_reply("failed"), ephemeral=True)
            return
        await cog.submit_entry(interaction, self.contest_id, list(self.image.values),
                               str(self.entry_title.value or ""), str(self.caption.value or ""))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.warning("contest.modal_error", error=str(error))
        try:
            if interaction.response.is_done():
                await interaction.followup.send(cx.entry_reply("failed"), ephemeral=True)
            else:
                await interaction.response.send_message(cx.entry_reply("failed"), ephemeral=True)
        except discord.DiscordException:
            pass


class ContestPanel(discord.ui.View):
    def __init__(self, enabled: bool = True) -> None:
        super().__init__(timeout=None)
        self.submit.disabled = not enabled

    @discord.ui.button(label="Submit entry", emoji="\U0001f3c6", style=discord.ButtonStyle.success,
                       custom_id="cielago:contest:submit")
    async def submit(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("Contests")
        if cog is None:
            await interaction.response.send_message(cx.entry_reply("closed"), ephemeral=True)
            return
        await cog.open_entry_form(interaction)


class VoteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"contest_vote:(?P<cid>[a-z0-9-]{1,24}):(?P<uid>[0-9]{1,20})",
):
    def __init__(self, contest_id: str, entrant_id: int) -> None:
        self.contest_id = contest_id
        self.entrant_id = entrant_id
        super().__init__(discord.ui.Button(label="Vote", emoji=VOTE_EMOJI,
                                           style=discord.ButtonStyle.primary,
                                           custom_id=f"contest_vote:{contest_id}:{entrant_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], int(match["uid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Contests")
        if cog is None:
            await interaction.response.send_message("Voting is unavailable right now.",
                                                    ephemeral=True)
            return
        await cog.handle_vote(interaction, self.contest_id, self.entrant_id)


class ConfirmButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"contest_confirm:(?P<cid>[a-z0-9-]{1,24})",
):
    def __init__(self, contest_id: str) -> None:
        self.contest_id = contest_id
        super().__init__(discord.ui.Button(label="Confirm and pay",
                                           style=discord.ButtonStyle.danger,
                                           custom_id=f"contest_confirm:{contest_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Contests")
        if cog is None:
            await interaction.response.send_message("Contests are not loaded.", ephemeral=True)
            return
        await cog.confirm(interaction, self.contest_id)


def entry_view(contest_id: str, entrant_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VoteButton(contest_id, entrant_id))
    return view


def confirm_view(contest_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ConfirmButton(contest_id))
    return view


class Contests(commands.Cog):
    contest = app_commands.Group(
        name="contest",
        description="Manage server event contests",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        definitions = files("cielago").joinpath("data/contests.json").read_text(encoding="utf-8")
        self.store = cx.ContestStore(definitions, settings.cielago_contest_data_path)
        self._member_locks: dict[int, asyncio.Lock] = {}
        self._pay_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.bot.add_view(ContestPanel())
        # Always load: staff commands still write the store while contests are
        # switched off, and must never overwrite real state with an empty one.
        self.store.load()
        if not settings.cielago_contests_enabled:
            log.info("contests.disabled")
            return
        self._tick.start()

    async def cog_unload(self) -> None:
        self._tick.cancel()

    # --- plumbing ---

    def _channel(self) -> discord.TextChannel | None:
        cid = settings.cielago_contest_channel_id
        channel = self.bot.get_channel(cid) if cid else None
        return channel if isinstance(channel, discord.TextChannel) else None

    def _staff_channel(self) -> discord.TextChannel | None:
        cid = settings.cielago_assistant_mod_channel_id
        channel = self.bot.get_channel(cid) if cid else None
        return channel if isinstance(channel, discord.TextChannel) else None

    def _lane_http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=settings.cielago_admin_base_url.rstrip("/"),
                                 headers={"X-Sietch-Award-Key": settings.cielago_contest_award_key},
                                 timeout=15.0)

    def _resolve(self, contest_id: str | None) -> cx.Contest | None:
        if contest_id:
            return self.store.contests.get(contest_id.strip())
        return cx.current_contest(self.store.contests.values(), int(time.time()))

    async def _delete_card(self, message_id: int) -> None:
        channel = self._channel()
        if channel is None:
            return
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.warning("contest.card_delete_failed", message_id=message_id, exc_info=True)

    # --- schedule ---

    @tasks.loop(seconds=30)
    async def _tick(self) -> None:
        try:
            now = int(time.time())
            for c in self.store.contests.values():
                due = cx.advance(c, now)
                if not due:
                    continue
                self.store.save()
                for key in due:
                    await self._post_notice(c, key)
                if "vote_close" in due:
                    await self._post_staff_report(c)
            await self._refresh_panel()
        except Exception:
            log.warning("contest.tick_failed", exc_info=True)

    @_tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _post_notice(self, c: cx.Contest, key: str) -> None:
        channel = self._channel()
        if channel is None:
            log.warning("contest.notice_no_channel", contest=c.id, notice=key)
            return
        try:
            await channel.send(cx.notice_text(c, key), allowed_mentions=NO_PINGS)
            log.info("contest.notice", contest=c.id, notice=key)
        except discord.HTTPException:
            log.warning("contest.notice_failed", contest=c.id, notice=key, exc_info=True)

    async def _post_staff_report(self, c: cx.Contest) -> bool:
        channel = self._staff_channel()
        if channel is None:
            log.warning("contest.staff_report_no_channel", contest=c.id)
            return False
        try:
            msg = await channel.send(embed=staff_report_embed(c), view=confirm_view(c.id),
                                     allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("contest.staff_report_failed", contest=c.id, exc_info=True)
            return False
        c.staff_message_id = msg.id
        self.store.save()
        return True

    async def _refresh_panel(self, force: bool = False) -> None:
        key, title, body, enabled = cx.panel_content(self.store.contests.values(), int(time.time()))
        panel = self.store.panel
        if not panel.get("message_id") or (panel.get("rendered") == key and not force):
            return
        channel = self.bot.get_channel(int(panel["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(title=title, description=body, color=COLOR)
        try:
            await channel.get_partial_message(int(panel["message_id"])).edit(
                embed=embed, view=ContestPanel(enabled), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("contest.panel_edit_failed", exc_info=True)
            return
        panel["rendered"] = key
        self.store.save()

    # --- entries ---

    async def open_entry_form(self, interaction: discord.Interaction) -> None:
        now = int(time.time())
        c = cx.current_contest(self.store.contests.values(), now)
        if not settings.cielago_contests_enabled or c is None or cx.phase(c, now) != "submit":
            await interaction.response.send_message(cx.entry_reply("closed"), ephemeral=True)
            return
        if interaction.user.bot:
            await interaction.response.send_message(cx.entry_reply("bot"), ephemeral=True)
            return
        await interaction.response.send_modal(EntryModal(c.id))

    async def submit_entry(self, interaction: discord.Interaction, contest_id: str,
                           attachments: list, title: str, caption: str) -> None:
        c = self.store.contests.get(contest_id)
        now = int(time.time())
        if c is None:
            await interaction.response.send_message(cx.entry_reply("closed"), ephemeral=True)
            return
        err, title, caption = cx.validate_entry(c, interaction.user.bot, title, caption, now)
        if err is None and (len(attachments) != 1 or not cx.image_ok(attachments[0].content_type,
                                                                      attachments[0].filename)):
            err = "bad_image"
        channel = self._channel()
        if err is None and channel is None:
            err = "failed"
        if err is None and attachments[0].size > channel.guild.filesize_limit:
            err = "too_large"
        if err is not None:
            await interaction.response.send_message(cx.entry_reply(err), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = interaction.user.id
        # Per member, not shared: a member's own two submissions must not race the
        # replace, but one member's upload must not queue everyone else's at 20:00.
        lock = self._member_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            key, old = await self._post_entry(c, channel, interaction.user, attachments[0],
                                              title, caption)
        if key not in ("ok", "replaced"):
            await interaction.followup.send(cx.entry_reply(key), ephemeral=True)
            return
        if old is not None and old.message_id:
            await self._delete_card(old.message_id)

        reply = cx.entry_reply(key, title)
        if settings.cielago_contest_award_key:
            async with self._lane_http() as http:
                linked = await cx.LANES[c.reward_lane]["linked"](http, uid)
            if linked is False:
                reply += "\n" + cx.link_hint(c)
        await interaction.followup.send(reply, ephemeral=True)
        log.info("contest.entry", contest=c.id, user=uid, replaced=old is not None)

    async def _post_entry(self, c: cx.Contest, channel: discord.TextChannel, user, att,
                          title: str, caption: str):
        """Download, prove it is an image, post the card, then commit. Returns
        (reply key, replaced entry). Entries can close while this runs, so the
        phase is checked before the work and again before the commit."""
        uid = user.id
        if cx.phase(c, int(time.time())) != "submit":
            return "closed", None
        try:
            data = await att.read()
        except discord.HTTPException:
            log.warning("contest.entry_download_failed", contest=c.id, user=uid, exc_info=True)
            return "failed", None
        ext = cx.image_ext(data)
        if ext is None:
            return "bad_image", None
        fname = f"entry-{uid}{ext}"
        entry = cx.Entry(discord_id=uid, display_name=user.display_name[:80], title=title,
                         caption=caption, submitted_at=int(time.time()), image_name=fname)
        try:
            msg = await channel.send(embed=entry_embed(c, entry),
                                     file=discord.File(io.BytesIO(data), filename=fname),
                                     view=entry_view(c.id, uid), allowed_mentions=NO_PINGS)
        except discord.HTTPException as exc:
            log.warning("contest.entry_post_failed", contest=c.id, user=uid, error=str(exc))
            return ("too_large" if getattr(exc, "status", None) == 413 else "failed"), None
        if cx.phase(c, int(time.time())) != "submit":
            await self._delete_card(msg.id)
            return "closed", None
        entry.message_id = msg.id
        old = cx.put_entry(c, entry)
        self.store.save()
        return ("replaced" if old else "ok"), old

    # --- votes ---

    async def handle_vote(self, interaction: discord.Interaction, contest_id: str,
                          entrant_id: int) -> None:
        c = self.store.contests.get(contest_id)
        if c is None or not settings.cielago_contests_enabled:
            await interaction.response.send_message("Voting is unavailable right now.",
                                                    ephemeral=True)
            return
        result = cx.cast_vote(c, interaction.user.id, interaction.user.bot, entrant_id,
                              int(time.time()))
        if result in ("voted", "moved", "removed"):
            self.store.save()
        await interaction.response.send_message(cx.vote_reply(result, c, entrant_id),
                                                ephemeral=True)

    # --- staff ---

    async def confirm(self, interaction: discord.Interaction, contest_id: str | None) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can confirm results.",
                                                    ephemeral=True)
            return
        c = self._resolve(contest_id)
        if c is None:
            await interaction.response.send_message("No such contest.", ephemeral=True)
            return
        if cx.phase(c, int(time.time())) not in ("closed", "done"):
            await interaction.response.send_message("Voting has not closed yet.", ephemeral=True)
            return
        if not settings.cielago_contest_award_key:
            await interaction.response.send_message(
                "CIELAGO_CONTEST_AWARD_KEY is not set, so nothing was paid or announced.",
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        payouts = c.payouts
        broke = False
        try:
            async with self._pay_lock:
                if c.confirmed_at is None:
                    c.confirmed_by = interaction.user.id
                    c.confirmed_at = int(time.time())
                    self.store.save()
                async with self._lane_http() as http:
                    payouts = await cx.pay_all(c, http)
                self.store.save()
                if not c.announced:
                    await self._announce_winners(c)
            await self._refresh_panel(force=True)
        except Exception:
            # Statuses recorded so far are saved below; staff still get a reply.
            broke = True
            log.warning("contest.confirm_failed", contest=c.id, exc_info=True)
        finally:
            self.store.save()

        counts: dict[str, int] = {}
        for status in payouts.values():
            counts[status] = counts.get(status, 0) + 1
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "nothing to pay"
        failed = sorted(k for k, v in payouts.items()
                        if v not in cx.FINAL_PAYOUTS and v != "unlinked")
        text = f"Confirmed **{c.title}**. Payouts: {summary}."
        if broke:
            text += f"\nSomething failed partway; run /contest confirm contest_id:{c.id} again."
        if failed:
            text += (f"\nRetry with /contest confirm contest_id:{c.id} (safe to repeat):\n"
                     + "\n".join(failed[:15]))
        if not c.announced:
            text += (f"\nThe winners post failed; run /contest confirm contest_id:{c.id} "
                     "to retry it.")
        await interaction.followup.send(text[:1900], ephemeral=True)
        await audit(interaction.guild, "contest-confirm", actor=interaction.user, contest=c.id,
                    payouts=summary, announced=c.announced)

    async def _announce_winners(self, c: cx.Contest) -> None:
        channel = self._channel()
        if channel is None:
            return
        if c.announcing_at is not None:
            # An earlier attempt may have posted and died before saving: look first.
            try:
                found = await self._find_winners_post(channel, c)
            except discord.HTTPException:
                log.warning("contest.winners_lookup_failed", contest=c.id, exc_info=True)
                return
            if found is not None:
                c.announced, c.winners_message_id = True, found
                self.store.save()
                return
        c.announcing_at = int(time.time())
        self.store.save()
        jump = {eid: f"https://discord.com/channels/{channel.guild.id}/{channel.id}/{e.message_id}"
                for eid, e in c.entries.items() if e.message_id}
        try:
            msg = await channel.send(cx.winners_text(c, jump), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("contest.winners_post_failed", contest=c.id, exc_info=True)
            return
        c.announced, c.winners_message_id = True, msg.id
        self.store.save()

    async def _find_winners_post(self, channel: discord.TextChannel, c: cx.Contest) -> int | None:
        after = datetime.fromtimestamp(c.announcing_at - 60, UTC)
        async for m in channel.history(limit=200, after=after, oldest_first=True):
            if m.author.id == self.bot.user.id and cx.is_winners_post(c, m.content):
                return m.id
        return None

    # --- commands ---

    @contest.command(name="panel", description="Post and pin the contest panel")
    async def panel(self, interaction: discord.Interaction) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        channel = self._channel()
        if channel is None:
            await interaction.response.send_message("CIELAGO_CONTEST_CHANNEL_ID is not set.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        key, title, body, enabled = cx.panel_content(self.store.contests.values(), int(time.time()))
        msg = await channel.send(embed=discord.Embed(title=title, description=body, color=COLOR),
                                 view=ContestPanel(enabled), allowed_mentions=NO_PINGS)
        try:
            await msg.pin(reason="Cielago contest panel")
        except discord.HTTPException:
            log.warning("contest.panel_pin_failed", exc_info=True)
        old = self.store.panel.get("message_id")
        self.store.panel = {"channel_id": channel.id, "message_id": msg.id, "rendered": key}
        self.store.save()
        note = f" The previous panel ({old}) is no longer updated; delete it." if old else ""
        await interaction.followup.send(f"Panel posted in {channel.mention}.{note}", ephemeral=True)
        await audit(interaction.guild, "contest-panel", actor=interaction.user,
                    channel=channel.name)

    @contest.command(name="status", description="Show the current contest's phase and counts")
    async def status(self, interaction: discord.Interaction) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        now = int(time.time())
        lines = []
        for c in self.store.contests.values():
            lines.append(f"`{c.id}` **{c.title}**: {cx.phase(c, now)}, {len(c.entries)} entries, "
                         f"{len(c.votes)} voters, entries <t:{c.submit_opens_at}:f> to "
                         f"<t:{c.submit_closes_at}:f>, votes to <t:{c.vote_closes_at}:f>")
        state = "on" if settings.cielago_contests_enabled else "OFF (CIELAGO_CONTESTS_ENABLED)"
        await interaction.response.send_message(
            f"Contests: {state}\n" + ("\n".join(lines) or "No contests defined."), ephemeral=True)

    @contest.command(name="remove", description="Remove a member's contest entry")
    @app_commands.describe(member="Whose entry to remove",
                           contest_id="Contest id (default: current)")
    async def remove(self, interaction: discord.Interaction, member: discord.User,
                     contest_id: str | None = None) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        c = self._resolve(contest_id)
        if c is None or c.confirmed_at is not None:
            await interaction.response.send_message("No open contest to remove from.",
                                                    ephemeral=True)
            return
        old = cx.remove_entry(c, member.id)
        if old is None:
            await interaction.response.send_message("That member has no entry.", ephemeral=True)
            return
        self.store.save()
        await interaction.response.defer(ephemeral=True)
        if old.message_id:
            await self._delete_card(old.message_id)
        if c.staff_message_id is not None:
            await self._post_staff_report(c)
        await interaction.followup.send(f"Removed **{old.title}** from **{c.title}**.",
                                        ephemeral=True)
        await audit(interaction.guild, "contest-remove", actor=interaction.user, contest=c.id,
                    entrant=member.id, title=old.title)

    @contest.command(name="results", description="Post the staff results report again")
    @app_commands.describe(contest_id="Contest id (default: current)")
    async def results(self, interaction: discord.Interaction,
                      contest_id: str | None = None) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        c = self._resolve(contest_id)
        if c is None or cx.phase(c, int(time.time())) not in ("closed", "done"):
            await interaction.response.send_message("Voting has not closed yet.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok = await self._post_staff_report(c)
        await interaction.followup.send("Report posted." if ok else "The staff channel is not set.",
                                        ephemeral=True)

    @contest.command(name="confirm", description="Confirm results, pay rewards, announce winners")
    @app_commands.describe(contest_id="Contest id (default: current)")
    async def confirm_cmd(self, interaction: discord.Interaction,
                          contest_id: str | None = None) -> None:
        await self.confirm(interaction, contest_id)


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(VoteButton, ConfirmButton)
    await bot.add_cog(Contests(bot))
