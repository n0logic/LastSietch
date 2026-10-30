"""Event contests: the rules, the schedule, the payout plan and the player copy.

No Discord in here. cogs/contests.py is the Discord surface and calls these, so
the vote rules, the one-entry replace, the schedule and the payouts are tested
without a live guild (scripts/tests/test_event_contests.py).

Contests are data: definitions live in data/contests.json (package data, times
in local wall-clock with an IANA zone so DST is the zone's problem, not ours);
runtime state (entries, votes, notices sent, payouts) lives in the state JSON at
CIELAGO_CONTEST_DATA_PATH and survives restarts.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CONTEST_ID_RE = re.compile(r"^[a-z0-9-]{1,24}$")
TITLE_MAX = 60
CAPTION_MAX = 200
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
PORTAL_URL = "https://portal.lastsietch.com"

# Each notice is posted once, when the contest first reaches its phase.
NOTICES = (("submit_open", "submit"), ("vote_open", "voting"), ("vote_close", "closed"))
PHASE_ORDER = ("scheduled", "submit", "between", "voting", "closed", "done")
# A retry may pay these again safely (the case id is idempotent); only these stop it.
FINAL_PAYOUTS = ("paid", "replay")
MEDALS = ("\U0001f947", "\U0001f948", "\U0001f949")


# --- model -------------------------------------------------------------------


@dataclass
class Entry:
    discord_id: int
    display_name: str
    title: str
    caption: str
    submitted_at: int
    message_id: int | None = None
    image_name: str = ""

    def to_dict(self) -> dict:
        return {"discord_id": self.discord_id, "display_name": self.display_name,
                "title": self.title, "caption": self.caption,
                "submitted_at": self.submitted_at, "message_id": self.message_id,
                "image_name": self.image_name}

    @classmethod
    def from_dict(cls, d: dict) -> Entry:
        return cls(discord_id=int(d["discord_id"]), display_name=str(d["display_name"]),
                   title=str(d["title"]), caption=str(d.get("caption") or ""),
                   submitted_at=int(d["submitted_at"]),
                   message_id=int(d["message_id"]) if d.get("message_id") else None,
                   image_name=str(d.get("image_name") or ""))


@dataclass
class Contest:
    id: str
    event_label: str
    title: str
    prompt: str
    submit_opens_at: int
    submit_closes_at: int
    vote_opens_at: int
    vote_closes_at: int
    entrant_reward: int
    winner_rewards: list[int]
    reward_lane: str
    entries: dict[int, Entry] = field(default_factory=dict)
    votes: dict[int, int] = field(default_factory=dict)  # voter id -> entrant id
    sent: list[str] = field(default_factory=list)
    staff_message_id: int | None = None
    confirmed_by: int | None = None
    confirmed_at: int | None = None
    payouts: dict[str, str] = field(default_factory=dict)  # case id -> status
    announced: bool = False
    # Set and saved BEFORE the winners post is sent: if it is set and `announced`
    # is not, a post may exist, and the channel is checked before posting again.
    announcing_at: int | None = None
    winners_message_id: int | None = None

    def state_dict(self) -> dict:
        return {"entries": {str(k): e.to_dict() for k, e in self.entries.items()},
                "votes": {str(k): v for k, v in self.votes.items()},
                "sent": list(self.sent), "staff_message_id": self.staff_message_id,
                "confirmed_by": self.confirmed_by, "confirmed_at": self.confirmed_at,
                "payouts": dict(self.payouts), "announced": self.announced,
                "announcing_at": self.announcing_at,
                "winners_message_id": self.winners_message_id}

    def apply_state(self, s: dict) -> None:
        self.entries = {int(k): Entry.from_dict(v) for k, v in (s.get("entries") or {}).items()}
        self.votes = {int(k): int(v) for k, v in (s.get("votes") or {}).items()}
        self.sent = list(s.get("sent") or [])
        self.staff_message_id = s.get("staff_message_id")
        self.confirmed_by = s.get("confirmed_by")
        self.confirmed_at = s.get("confirmed_at")
        self.payouts = dict(s.get("payouts") or {})
        self.announced = bool(s.get("announced"))
        self.announcing_at = s.get("announcing_at")
        self.winners_message_id = s.get("winners_message_id")


def parse_local(raw: str, tz: str) -> int:
    """'YYYY-MM-DD HH:MM' in the named zone -> unix seconds."""
    naive = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M")
    return int(naive.replace(tzinfo=ZoneInfo(tz)).timestamp())


def _nonneg_int(value, name: str, cid: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"contest {cid}: {name} must be a non-negative integer")
    return value


def parse_definitions(text: str) -> list[Contest]:
    """Validated contest definitions, sorted by submit open. Raises ValueError so
    a bad file stops the cog loudly instead of running a half-read schedule."""
    raw = json.loads(text)
    out = []
    for d in raw.get("contests", []):
        cid = d.get("id")
        if not isinstance(cid, str) or not CONTEST_ID_RE.fullmatch(cid):
            raise ValueError(f"contest id {cid!r} must be 1-24 of a-z 0-9 -")
        tz = d.get("timezone", "America/New_York")
        times = [parse_local(d[k], tz) for k in
                 ("submit_opens_at", "submit_closes_at", "vote_opens_at", "vote_closes_at")]
        if not (times[0] < times[1] <= times[2] < times[3]):
            raise ValueError(f"contest {cid}: windows must run submit open < submit close "
                             "<= vote open < vote close")
        for key in ("event_label", "title", "prompt"):
            if not isinstance(d.get(key), str) or not d[key].strip():
                raise ValueError(f"contest {cid}: {key} is required")
        winner_rewards = d.get("winner_rewards", [])
        if not isinstance(winner_rewards, list) or not 1 <= len(winner_rewards) <= 9:
            raise ValueError(f"contest {cid}: winner_rewards must list 1 to 9 amounts")
        lane = LANES.get(d.get("reward_lane"))
        if lane is None:
            raise ValueError(f"contest {cid}: reward_lane must be one of {sorted(LANES)}")
        entrant_reward = _nonneg_int(d.get("entrant_reward", 0), "entrant_reward", cid)
        winner_rewards = [_nonneg_int(r, "winner_rewards", cid) for r in winner_rewards]
        if len(winner_rewards) > lane["places"]:
            raise ValueError(f"contest {cid}: the {d['reward_lane']} lane pays at most "
                             f"{lane['places']} places")
        if max([entrant_reward, *winner_rewards]) > lane["max"]:
            raise ValueError(f"contest {cid}: a reward is above the lane's {lane['max']} per case")
        out.append(Contest(
            id=cid, event_label=d["event_label"].strip(), title=d["title"].strip(),
            prompt=d["prompt"].strip(), submit_opens_at=times[0], submit_closes_at=times[1],
            vote_opens_at=times[2], vote_closes_at=times[3],
            entrant_reward=entrant_reward, winner_rewards=winner_rewards,
            reward_lane=d["reward_lane"]))
    out.sort(key=lambda c: c.submit_opens_at)
    if len({c.id for c in out}) != len(out):
        raise ValueError("contest ids must be unique")
    for a, b in zip(out, out[1:], strict=False):
        if b.submit_opens_at < a.vote_closes_at:
            raise ValueError(f"contests {a.id} and {b.id} overlap")
    return out


class ContestStore:
    def __init__(self, definitions_text: str, state_path: str) -> None:
        self.state_path = Path(state_path)
        self.contests: dict[str, Contest] = {c.id: c for c in parse_definitions(definitions_text)}
        self.panel: dict = {}

    def load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        self.panel = dict(data.get("panel") or {})
        for cid, state in (data.get("contests") or {}).items():
            if cid in self.contests:
                self.contests[cid].apply_state(state)

    def save(self) -> None:
        # Write-then-rename: a crash mid-write must not leave a truncated file
        # that loads as "no entries, no votes".
        payload = {"panel": self.panel,
                   "contests": {cid: c.state_dict() for cid, c in self.contests.items()}}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_path.parent, prefix=".contests-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.state_path)


# --- schedule ------------------------------------------------------------------


def phase(c: Contest, now: int) -> str:
    if c.confirmed_at is not None:
        return "done"
    if now < c.submit_opens_at:
        return "scheduled"
    if now < c.submit_closes_at:
        return "submit"
    if now < c.vote_opens_at:
        return "between"
    if now < c.vote_closes_at:
        return "voting"
    return "closed"


def current_contest(contests, now: int) -> Contest | None:
    """The contest that has opened and is not yet confirmed (definitions never overlap)."""
    for c in sorted(contests, key=lambda c: c.submit_opens_at):
        if phase(c, now) not in ("scheduled", "done"):
            return c
    return None


def next_contest(contests, now: int) -> Contest | None:
    upcoming = [c for c in contests if phase(c, now) == "scheduled"]
    return min(upcoming, key=lambda c: c.submit_opens_at) if upcoming else None


def advance(c: Contest, now: int) -> list[str]:
    """Notices to post now, marked sent. Every notice whose phase has been reached
    is marked, but only the LATEST is returned: after downtime across a boundary,
    'entries are open' posted after entries closed would be wrong. vote_close is
    also the cue for the staff report, which the caller sends."""
    reached = PHASE_ORDER.index(phase(c, now))
    due = [key for key, ph in NOTICES
           if PHASE_ORDER.index(ph) <= reached and key not in c.sent]
    if not due:
        return []
    c.sent.extend(due)
    return [due[-1]]


# --- entries and votes -----------------------------------------------------------


def validate_entry(c: Contest, is_bot: bool, title: str, caption: str, now: int):
    """(error key or None, clean title, clean caption)."""
    if is_bot:
        return "bot", "", ""
    if phase(c, now) != "submit":
        return "closed", "", ""
    title = " ".join((title or "").split())
    caption = (caption or "").strip()
    if not 1 <= len(title) <= TITLE_MAX:
        return "bad_title", "", ""
    if len(caption) > CAPTION_MAX:
        return "bad_caption", "", ""
    return None, title, caption


def image_ok(content_type: str | None, filename: str | None) -> bool:
    """The declared type AND the filename extension must both say image."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    return ctype in IMAGE_TYPES and (filename or "").lower().endswith(IMAGE_EXTS)


def image_ext(data: bytes) -> str | None:
    """The extension the file's magic bytes prove, or None when they prove nothing."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def put_entry(c: Contest, entry: Entry) -> Entry | None:
    """One entry per member: the new one replaces theirs. Returns the replaced
    entry so the caller can take its card down."""
    old = c.entries.get(entry.discord_id)
    c.entries[entry.discord_id] = entry
    return old


def remove_entry(c: Contest, entrant_id: int) -> Entry | None:
    """Staff removal. Votes for the entry are dropped so those voters can vote again."""
    old = c.entries.pop(entrant_id, None)
    if old is not None:
        c.votes = {v: e for v, e in c.votes.items() if e != entrant_id}
    return old


def cast_vote(c: Contest, voter_id: int, voter_is_bot: bool, entrant_id: int, now: int) -> str:
    """One vote per member per contest. A vote on another entry moves it; a vote
    on the current choice takes it back."""
    if voter_is_bot:
        return "bot"
    ph = phase(c, now)
    if ph in ("scheduled", "submit", "between"):
        return "too_early"
    if ph != "voting":
        return "closed"
    if entrant_id not in c.entries:
        return "no_entry"
    if voter_id == entrant_id:
        return "self"
    current = c.votes.get(voter_id)
    if current == entrant_id:
        del c.votes[voter_id]
        return "removed"
    c.votes[voter_id] = entrant_id
    return "voted" if current is None else "moved"


def tally(c: Contest) -> list[tuple[int, int]]:
    """[(entrant id, votes)], most votes first; a tie goes to the earlier entry."""
    counts = {eid: 0 for eid in c.entries}
    for eid in c.votes.values():
        if eid in counts:
            counts[eid] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], c.entries[kv[0]].submitted_at, kv[0]))


def winners(c: Contest) -> list[tuple[int, int, int]]:
    """[(place, entrant id, votes)] for each paid place; an entry needs a vote to place."""
    ranked = [(eid, n) for eid, n in tally(c) if n > 0]
    return [(i + 1, eid, n) for i, (eid, n) in enumerate(ranked[:len(c.winner_rewards)])]


def payout_plan(c: Contest) -> list[dict]:
    label = f"{c.event_label}: {c.title}"[:150]
    plan = []
    if c.entrant_reward > 0:
        for eid in sorted(c.entries):
            plan.append({"discord_id": eid, "amount": c.entrant_reward,
                         "case_id": f"contest:{c.id}:entry:{eid}", "reason": f"{label} entry"})
    for place, eid, _votes in winners(c):
        amount = c.winner_rewards[place - 1]
        if amount > 0:
            plan.append({"discord_id": eid, "amount": amount,
                         "case_id": f"contest:{c.id}:place:{place}",
                         "reason": f"{label} place {place}"})
    return plan


# --- reward lanes ------------------------------------------------------------------
#
# A lane is {unit, max, pay, linked}; max is the per-case cap the backend
# enforces (AWARD_MAX in portal_sietch.py) and places the most paid places it
# accepts (contest:<id>:place:1..3). pay(http, item) -> 'paid' | 'replay' | 'unlinked'
# | 'error:<why>'; linked(http, discord_id) -> True | False | None (unknown).
# `http` is an httpx.AsyncClient the cog builds with the lane's base URL and key.


def _json_object(resp) -> dict | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def _ls_tokens_pay(http, item: dict) -> str:
    try:
        resp = await http.post("/_internal/sietch/contest-award", json={
            "discord_id": str(item["discord_id"]), "amount": item["amount"],
            "reason": item["reason"], "case_id": item["case_id"]})
    except Exception as exc:  # noqa: BLE001 - any transport failure is retryable
        return f"error:{type(exc).__name__}"
    body = _json_object(resp)
    if resp.status_code == 200:
        # A 200 we cannot read is not proof of payment: retryable, the case id is idempotent.
        if body is None or not body.get("ok"):
            return "error:200:unreadable"
        return "replay" if body.get("replay") else "paid"
    detail = str((body or {}).get("detail") or "")
    if resp.status_code == 409 and detail == "unlinked":
        return "unlinked"
    return f"error:{resp.status_code}:{detail[:40]}"


async def _ls_tokens_linked(http, discord_id: int) -> bool | None:
    try:
        resp = await http.get(f"/_internal/sietch/contest-link/{int(discord_id)}")
    except Exception:  # noqa: BLE001
        return None
    body = _json_object(resp)
    if resp.status_code != 200 or body is None or not isinstance(body.get("linked"), bool):
        return None
    return body["linked"]


LANES = {
    "ls_tokens": {"unit": "LS Tokens", "max": 50, "places": 3, "pay": _ls_tokens_pay,
                  "linked": _ls_tokens_linked},
}


async def pay_all(c: Contest, http) -> dict[str, str]:
    """Pay every planned case not already final; record each status on the contest.
    Re-running is safe: the backend treats a repeated case id as a replay."""
    lane = LANES[c.reward_lane]
    for item in payout_plan(c):
        if c.payouts.get(item["case_id"]) in FINAL_PAYOUTS:
            continue
        try:
            status = await lane["pay"](http, item)
        except Exception as exc:  # noqa: BLE001 - one bad case must not stop the rest
            status = f"error:{type(exc).__name__}"
        c.payouts[item["case_id"]] = status
    return c.payouts


# --- copy (player-facing: no em dashes, no mass pings) ------------------------------


_MARKDOWN_RE = re.compile(r"([\\*_~`|>#\[\]()<-])")


def safe(text: str) -> str:
    """Member-written text (titles, captions, display names) for a markdown
    surface. Every markdown and link character is escaped, so `x](https://evil) [y`
    renders literally and cannot become a hidden link, and a URL scheme is broken
    with a zero-width space so a bare URL does not autolink either. Hand-rolled to
    keep this module free of discord.py; the cog uses it for every post."""
    return _MARKDOWN_RE.sub(r"\\\1", (text or "").replace("://", ":\u200b//"))


def _unit(c: Contest) -> str:
    return LANES.get(c.reward_lane, {}).get("unit", "rewards")


def rewards_line(c: Contest) -> str:
    amounts = [str(r) for r in c.winner_rewards]
    places = amounts[0] if len(amounts) == 1 else ", ".join(amounts[:-1]) + " and " + amounts[-1]
    top = "the winner earns" if len(amounts) == 1 else f"the top {len(amounts)} earn"
    lead = f"Every entrant earns {c.entrant_reward} {_unit(c)}; " if c.entrant_reward else ""
    return f"{lead}{top} {places} {_unit(c)}. Link your Discord on the portal to receive them."


def panel_content(contests, now: int) -> tuple[str, str, str, bool]:
    """(render key, embed title, embed body, submit button enabled)."""
    c = current_contest(contests, now)
    if c is None:
        nxt = next_contest(contests, now)
        body = "No contest open right now."
        if nxt is not None:
            body += f"\n\nNext up: **{nxt.title}**, entries open <t:{nxt.submit_opens_at}:F>."
        return f"none:{nxt.id if nxt else ''}", "\U0001f3c6 Server events", body, False
    ph = phase(c, now)
    title = f"\U0001f3c6 {c.event_label}: {c.title}"
    if ph == "submit":
        body = (f"{c.prompt}\n\n"
                "Press **Submit entry** to send one screenshot with a title. One entry per "
                "member; sending again replaces your entry.\n\n"
                f"Entries close <t:{c.submit_closes_at}:F> (<t:{c.submit_closes_at}:R>). "
                f"Voting runs <t:{c.vote_opens_at}:F> to <t:{c.vote_closes_at}:F>.\n\n"
                f"{rewards_line(c)}")
        return f"{c.id}:submit", title, body, True
    if ph == "between":
        body = f"Entries are closed. Voting opens <t:{c.vote_opens_at}:F>."
    elif ph == "voting":
        body = ("Entries are closed and voting is open. Press \U0001f3ee **Vote** on your "
                "favourite entry below. One vote per member: press another entry to move it, "
                f"or the same one again to take it back.\n\nVoting closes <t:{c.vote_closes_at}:F> "
                f"(<t:{c.vote_closes_at}:R>).")
    else:
        body = "Voting is closed. Winners are announced here once staff confirm the results."
    return f"{c.id}:{ph}", title, body, False


def notice_text(c: Contest, key: str) -> str:
    if key == "submit_open":
        return (f"\U0001f3c6 **{c.event_label}: {c.title}** is open for entries!\n{c.prompt}\n\n"
                "Press **Submit entry** on the pinned panel. Entries close "
                f"<t:{c.submit_closes_at}:F>.")
    if key == "vote_open":
        return (f"\U0001f3ee Entries are closed and voting is open for **{c.title}**! Press Vote "
                "on your favourite entry. One vote per member, and you can move it until "
                f"<t:{c.vote_closes_at}:F>.")
    return (f"Voting is closed for **{c.title}**. Thank you to everyone who took part! "
            "Winners are announced here once staff confirm the results.")


ENTRY_REPLIES = {
    "ok": "Your entry **{title}** is in. Good luck!",
    "replaced": "Your entry is now **{title}**. Your previous entry was taken down.",
    "closed": "Entries are not open right now.",
    "bot": "Bots cannot enter.",
    "bad_title": "Please give your entry a title of 1 to 60 characters.",
    "bad_caption": "Please keep the caption to 200 characters or fewer.",
    "bad_image": "Please attach one image (PNG, JPG, WEBP or GIF).",
    "too_large": "That image is too large to post here. Please send a smaller screenshot.",
    "failed": "Something went wrong posting your entry. Nothing was saved, so please try again.",
}

VOTE_REPLIES = {
    "voted": "\U0001f3ee Your vote is in for **{title}**. Votes stay hidden until voting closes.",
    "moved": "\U0001f3ee Your vote moved to **{title}**. You still have one vote.",
    "removed": "Your vote is taken back. You can vote for any entry until voting closes.",
    "self": "You cannot vote for your own entry.",
    "bot": "Bots cannot vote.",
    "too_early": "Voting opens <t:{vote_opens_at}:F>.",
    "closed": "Voting is closed for this contest.",
    "no_entry": "That entry is no longer in the contest.",
}


def entry_reply(key: str, title: str = "") -> str:
    return ENTRY_REPLIES[key].format(title=safe(title))


def vote_reply(key: str, c: Contest, entrant_id: int) -> str:
    e = c.entries.get(entrant_id)
    return VOTE_REPLIES[key].format(title=safe(e.title) if e else "",
                                    vote_opens_at=c.vote_opens_at)


def link_hint(c: Contest) -> str:
    return f"Link your Discord on the portal ({PORTAL_URL}) to receive {_unit(c)} for taking part."


def winners_header(c: Contest) -> str:
    return f"\U0001f3c6 **{c.event_label}: {c.title}** winners!"


def no_votes_text(c: Contest) -> str:
    return f"\U0001f3c6 **{c.title}** has closed with no votes cast. Thank you for taking part!"


def is_winners_post(c: Contest, content: str) -> bool:
    """Whether a bot message is this contest's winners post (crash recovery)."""
    first = (content or "").split("\n", 1)[0]
    return first in (winners_header(c), no_votes_text(c))


def winners_text(c: Contest, jump_urls: dict[int, str]) -> str:
    places = winners(c)
    if not places:
        return no_votes_text(c)
    lines = [winners_header(c)]
    for place, eid, votes in places:
        e = c.entries[eid]
        medal = MEDALS[place - 1] if place <= len(MEDALS) else f"#{place}"
        title = safe(e.title)
        label = f"[{title}]({jump_urls[eid]})" if eid in jump_urls else f"**{title}**"
        noun = "vote" if votes == 1 else "votes"
        lines.append(f"{medal} {safe(e.display_name)} with {label}, {votes} {noun}, "
                     f"{c.winner_rewards[place - 1]} {_unit(c)}")
    if c.entrant_reward:
        lines.append(f"\nEvery entrant earns {c.entrant_reward} {_unit(c)}. Thank you all!")
    return "\n".join(lines)
