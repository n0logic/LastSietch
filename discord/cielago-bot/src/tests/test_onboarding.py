"""Welcome card, sietch pick, and the private staff thread button.

Runs the real cog methods with Discord stubbed. Pins the things that would hurt
quietly in production: a missing role crashing the pick instead of telling
staff, a public welcome that pings or repeats, and a private thread that leaves
the player out or pings anyone.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
os.environ.setdefault("LAST_SIETCH_GUILD_ID", "123456789")

import discord  # noqa: E402

from cielago.cogs import admin as A  # noqa: E402
from cielago.cogs import connect as C  # noqa: E402
from cielago.cogs import onboarding as O  # noqa: E402
from cielago.cogs import report as R  # noqa: E402
from cielago.config import settings  # noqa: E402


class _Resp:
    def __init__(self):
        self.messages = []
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, ephemeral=False, **kw):
        self._done = True
        self.messages.append((content, ephemeral))

    async def defer(self, **kw):
        self._done = True


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, ephemeral=False, **kw):
        self.messages.append((content, ephemeral))


class _Role:
    def __init__(self, name):
        self.name = name


class _Member(discord.Member):
    # Subclassed only so isinstance(user, discord.Member) holds; never initialised.
    def __init__(self, uid, roles, fail=False):
        self._uid = uid
        self._roles_list = list(roles)
        self.fail = fail

    @property
    def id(self):
        return self._uid

    @property
    def roles(self):
        return self._roles_list

    @property
    def mention(self):
        return f"<@{self._uid}>"

    @property
    def display_name(self):
        return "Wanderer"

    def __str__(self):
        return "wanderer#0001"

    async def add_roles(self, *roles, reason=None):
        if self.fail:
            raise discord.DiscordException("Missing Permissions")
        self._roles_list.extend(roles)

    async def remove_roles(self, *roles, reason=None):
        self._roles_list = [r for r in self._roles_list if r not in roles]


class _Channel:
    def __init__(self, name):
        self.name = name
        self.id = 77
        self.sent = []

    @property
    def mention(self):
        return f"<#{self.id}>"

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


class _Guild:
    def __init__(self, roles, channels=()):
        self.id = 123456789
        self.roles = roles
        self.text_channels = list(channels)

    def get_channel(self, cid):
        return None


class _Interaction:
    def __init__(self, user, guild, channel=None):
        self.user = user
        self.guild = guild
        self.channel = channel
        self.response = _Resp()
        self.followup = _Followup()
        self.client = None


def _cog(tmp):
    settings.cielago_sietch_welcomed_path = str(Path(tmp) / "welcomed.json")
    settings.cielago_sietch_welcome_channel_id = None
    settings.cielago_sietch_public_welcome = True
    return O.Onboarding(bot=None)


def _capture_audit(module):
    calls = []

    async def fake(guild, action, actor=None, **fields):
        calls.append((action, fields))

    module.audit = fake
    return calls


def _sietch_roles():
    return [_Role("Habbanya"), _Role("Kulon"), _Role("Amtal")]


# --- pure planning ---

def test_plan_grants_one_and_removes_the_others():
    held = {"Habbanya", "Holders"}
    assert O.plan_roles("amtal", held) == ("Amtal", ["Habbanya"])


def test_kulon_is_never_granted_but_always_stripped():
    assert "kulon" not in O.SIETCHES
    assert O.plan_roles("habbanya", {"Kulon"}) == ("Habbanya", ["Kulon"])
    assert O.plan_roles("amtal", {"Amtal", "Kulon"}) == (None, ["Kulon"])
    # A stale "kulon" value from an old ephemeral picker grants nothing.
    assert O.plan_roles("kulon", {"Kulon", "Amtal"}) == (None, ["Amtal", "Kulon"])


def test_plan_looking_clears_all_and_adds_nothing():
    assert O.plan_roles(O.LOOKING, {"Amtal", "Kulon", "Holders"}) == (None, ["Amtal", "Kulon"])


def test_plan_repeat_pick_is_a_no_op():
    assert O.plan_roles("amtal", {"Amtal"}) == (None, [])


def test_sietch_roles_are_in_the_bootstrap_ladder_and_not_hoisted():
    ladder = {name: hoist for name, _color, hoist in A.DEFAULT_ROLES}
    for name, _mode in O.SIETCHES.values():
        assert ladder.get(name) is False
    for name in O.RETIRED_ROLES:
        assert name not in ladder


def test_picker_offers_habbanya_amtal_and_just_looking_only():
    async def build():
        return O.SietchPickView()

    select = asyncio.run(build()).children[0]
    assert [o.value for o in select.options] == ["habbanya", "amtal", O.LOOKING]
    assert [o.label for o in select.options] == [
        "PvE: Habbanya", "PvP: Amtal (full PvP)", "Just looking"]


def test_bootstrap_never_recreates_the_deleted_ladder():
    names = [name for name, _color, _hoist in A.DEFAULT_ROLES]
    assert names == ["Naib", "At The Gate", "Founder", "Habbanya", "Amtal"]


def test_bootstrap_colours_and_hoist_match_the_live_roles():
    roles = {name: (color, hoist) for name, color, hoist in A.DEFAULT_ROLES}
    assert roles["Naib"] == (16032072, True)
    assert roles["Founder"] == (3832789, False)
    assert roles["At The Gate"] == (5920594, False)


def test_copy_says_the_pick_gates_nothing():
    assert "every channel stays open" in O.welcome_embed(_Guild([], [])).description


# --- apply_pick ---

def test_pick_grants_role_and_welcomes_once_without_pings():
    with tempfile.TemporaryDirectory() as tmp:
        audits = _capture_audit(O)
        roles = _sietch_roles()
        general = _Channel("🏜️｜dune-general")
        guild = _Guild(roles, [general])
        member = _Member(5, [roles[2]])
        cog = _cog(tmp)

        inter = _Interaction(member, guild)
        asyncio.run(cog.apply_pick(inter, "habbanya"))
        assert [r.name for r in member.roles] == ["Habbanya"]
        assert inter.response.messages[0][1] is True
        assert "Habbanya" in inter.response.messages[0][0]
        assert audits[0][0] == "sietch-pick"
        assert len(general.sent) == 1
        content, kw = general.sent[0]
        assert "@everyone" not in content and "@here" not in content
        mentions = kw["allowed_mentions"]
        assert not mentions.users and not mentions.roles and not mentions.everyone

        # Switching sietch, and coming back, never posts a second public welcome,
        # including after a restart (fresh cog reading the same file).
        asyncio.run(cog.apply_pick(_Interaction(member, guild), O.LOOKING))
        asyncio.run(_cog(tmp).apply_pick(_Interaction(member, guild), "amtal"))
        assert [r.name for r in member.roles] == ["Amtal"]
        assert len(general.sent) == 1


def test_public_welcome_can_be_switched_off():
    with tempfile.TemporaryDirectory() as tmp:
        _capture_audit(O)
        general = _Channel("dune-general")
        guild = _Guild(_sietch_roles(), [general])
        cog = _cog(tmp)
        settings.cielago_sietch_public_welcome = False
        try:
            asyncio.run(cog.apply_pick(_Interaction(_Member(6, []), guild), "amtal"))
        finally:
            settings.cielago_sietch_public_welcome = True
        assert general.sent == []


def test_missing_role_tells_player_and_staff_without_raising():
    with tempfile.TemporaryDirectory() as tmp:
        audits = _capture_audit(O)
        guild = _Guild([_Role("Habbanya")])
        member = _Member(7, [])
        inter = _Interaction(member, guild)
        asyncio.run(_cog(tmp).apply_pick(inter, "amtal"))
        assert "Staff have been told" in inter.response.messages[0][0]
        assert audits == [("sietch-pick-failed",
                           {"choice": "amtal", "problem": "role Amtal not found"})]
        assert member.roles == []


def test_role_permission_error_is_handled():
    with tempfile.TemporaryDirectory() as tmp:
        audits = _capture_audit(O)
        member = _Member(8, [], fail=True)
        inter = _Interaction(member, _Guild(_sietch_roles()))
        asyncio.run(_cog(tmp).apply_pick(inter, "amtal"))
        assert "Staff have been told" in inter.response.messages[0][0]
        assert audits[0][0] == "sietch-pick-failed"


def test_pick_strips_a_leftover_kulon_role():
    with tempfile.TemporaryDirectory() as tmp:
        _capture_audit(O)
        roles = _sietch_roles()
        member = _Member(10, [roles[1], _Role("Holders")])
        inter = _Interaction(member, _Guild(roles))
        asyncio.run(_cog(tmp).apply_pick(inter, "amtal"))
        assert sorted(r.name for r in member.roles) == ["Amtal", "Holders"]


def test_just_looking_clears_and_posts_nothing_public():
    with tempfile.TemporaryDirectory() as tmp:
        _capture_audit(O)
        roles = _sietch_roles()
        general = _Channel("dune-general")
        member = _Member(9, [roles[1]])
        inter = _Interaction(member, _Guild(roles, [general]))
        asyncio.run(_cog(tmp).apply_pick(inter, O.LOOKING))
        assert member.roles == []
        assert "Cleared" in inter.response.messages[0][0]
        assert general.sent == []


# --- views and copy ---

def test_views_are_persistent():
    async def build():
        return O.WelcomeView(), O.SietchPickView(), R.ReportLauncher()

    welcome, pick, launcher = asyncio.run(build())
    assert welcome.is_persistent() and pick.is_persistent() and launcher.is_persistent()
    ids = {getattr(i, "custom_id", None) for i in launcher.children}
    assert "cielago:report:private" in ids
    links = [i.url for i in welcome.children if getattr(i, "url", None)]
    assert links == [f"{O.PORTAL}/login"]


def test_connect_build_field_has_no_stale_version():
    build = next(f.value for f in C.dune_embed().fields if f.name == "Build")
    assert "1.4" not in build
    assert "Current Funcom build" in build


def test_rules_summary_matches_the_expanded_code_and_links_out():
    text = O.rules_embed().description
    assert text.startswith("**Zero tolerance")
    for headline in ("The tribe", "Play fair", "Bases differ by sietch",
                     "Honour your trades", "Appeal or private staff matter", "7 days"):
        assert headline in text
    assert text.rstrip().endswith(f"{O.PORTAL}/rules")
    assert "seven" not in text.lower()


def test_player_facing_copy_has_no_em_dashes():
    guild = _Guild([], [])
    texts = [O.welcome_embed(guild).description, O.rules_embed().description]
    assert all("—" not in t for t in texts)


# --- private staff thread ---

class _Thread:
    def __init__(self, fail_add=False):
        self.id = 4321
        self.archived = False
        self.mention = "<#4321>"
        self.jump_url = "https://discord.com/channels/1/4321"
        self.added = []
        self.sent = []
        self.deleted = False
        self.fail_add = fail_add

    async def add_user(self, user):
        if self.fail_add:
            raise discord.DiscordException("Missing Permissions")
        self.added.append(user.id)

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))

    async def delete(self):
        self.deleted = True


class _TextChannel(discord.TextChannel):
    # Subclassed only so isinstance(channel, discord.TextChannel) holds.
    def __init__(self, thread, threads=None):
        self._thread = thread
        self._threads = threads or {}
        self.created = []

    @property
    def guild(self):
        return self

    def get_thread(self, tid):
        return self._threads.get(tid)

    async def create_thread(self, **kw):
        self.created.append(kw)
        return self._thread


class _Bot:
    def __init__(self, mod):
        self.mod = mod

    def get_channel(self, cid):
        return self.mod


class _ModChannel(discord.TextChannel):
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


def test_private_thread_adds_player_and_admins_and_pings_nobody():
    audits = _capture_audit(R)
    thread = _Thread()
    channel = _TextChannel(thread)
    mod = _ModChannel()
    cog = R.Reports(_Bot(mod))
    settings.cielago_admin_ids = "111,222"
    try:
        inter = _Interaction(_Member(5, []), None, channel)
        asyncio.run(cog.open_private_thread(inter))
    finally:
        settings.cielago_admin_ids = ""
    kw = channel.created[0]
    assert kw["type"] is discord.ChannelType.private_thread
    assert kw["invitable"] is False
    assert thread.added[0] == 5 and set(thread.added[1:]) == {111, 222}
    for _content, sent_kw in thread.sent + mod.sent:
        m = sent_kw["allowed_mentions"]
        assert not m.users and not m.roles and not m.everyone
    assert thread.jump_url in mod.sent[0][0]
    assert inter.followup.messages[0][1] is True
    assert audits[0][0] == "private-thread-open"

    # A second press points at the open thread rather than making another.
    channel._threads = {thread.id: thread}
    again = _Interaction(_Member(5, []), None, channel)
    asyncio.run(cog.open_private_thread(again))
    assert len(channel.created) == 1
    assert thread.mention in again.response.messages[0][0]


def test_private_thread_cleans_up_when_player_cannot_be_added():
    audits = _capture_audit(R)
    thread = _Thread(fail_add=True)
    mod = _ModChannel()
    cog = R.Reports(_Bot(mod))
    inter = _Interaction(_Member(5, []), None, _TextChannel(thread))
    asyncio.run(cog.open_private_thread(inter))
    assert thread.deleted
    assert "Staff have been told" in inter.followup.messages[0][0]
    assert audits[0][0] == "private-thread-failed"
    assert mod.sent and mod.sent[0][1]["allowed_mentions"].users is False


def test_private_thread_refuses_outside_a_text_channel():
    cog = R.Reports(_Bot(None))
    inter = _Interaction(_Member(5, []), None, channel=object())
    asyncio.run(cog.open_private_thread(inter))
    assert inter.response.messages[0][1] is True


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    failures = 0
    for fn in _all_tests():
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(_all_tests()) - failures}/{len(_all_tests())} passed")
    raise SystemExit(1 if failures else 0)
