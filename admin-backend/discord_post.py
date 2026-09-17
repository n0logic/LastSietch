"""Post messages to Discord channels via bot token (matches /opt/lastsietch/update-status.sh pattern)."""
import re

import httpx

from config import DISCORD_BOT_TOKEN, DISCORD_CH_EVENTS

DISCORD_API = "https://discord.com/api/v10"

# Last Sietch voice rule: no em dashes or en dashes in user-facing copy.
EM_EN_DASH_RE = re.compile(r"[—–]")

# Invisible characters a poster can wedge into "@everyone" to slip the literal
# strip below. They render as nothing, so the text still reads as a ping to a
# player, which means they have to come out BEFORE the mention match, not after.
ZERO_WIDTH_RE = re.compile("[\u00ad\u200b-\u200f\u2060\ufeff]")

# Server-side mass-mention strip. allowed_mentions already stops Discord from
# resolving one, but the literal text still reads as a ping, and a future caller
# that builds its own payload inherits this defence for free.
MASS_MENTION_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)

# Canonical player-facing event permalink. The portal is the one place an event
# is described in full; the Discord post is a pointer, never a second copy.
EVENT_URL = "https://portal.lastsietch.com/events/{id}"

EVENT_DESCRIPTION_MAX = 200


def _clean(content: str) -> str:
    content = EM_EN_DASH_RE.sub("-", content or "")
    content = ZERO_WIDTH_RE.sub("", content)
    return MASS_MENTION_RE.sub(r"\1", content)


async def post_to_channel(channel_id: str, content: str) -> dict | None:
    """Post a message to a Discord channel. Returns response dict on success, None on failure (logs only).
    Best-effort: never raises, so a Discord outage doesn't break orchestration."""
    if not DISCORD_BOT_TOKEN or not channel_id:
        return None
    content = _clean(content)
    if len(content) > 2000:
        content = content[:1997] + "..."
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "lastsietch-admin/1.0",
    }
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            print(f"[discord_post] {channel_id} -> {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.json()
    except Exception as e:
        print(f"[discord_post] {channel_id} exception: {e}")
        return None


def _event_content(event: dict) -> str:
    """The announcement body: what it is, when it starts, where, one line of
    flavour, and the permalink."""
    lines = []
    title = str(event.get("title") or "").strip()
    if title:
        lines.append(title)
    kind = str(event.get("kind") or "").strip()
    if kind:
        lines.append(f"Kind: {kind}")
    starts = str(event.get("starts_utc") or "").strip()
    if starts:
        lines.append(f"Starts: {starts} UTC")
    map_name = str(event.get("map") or event.get("map_name") or "").strip()
    if map_name:
        lines.append(f"Map: {map_name}")
    description = str(event.get("description") or "").strip()
    if description:
        first = description.splitlines()[0].strip()[:EVENT_DESCRIPTION_MAX]
        if first:
            lines.append(first)
    lines.append(EVENT_URL.format(id=event.get("id")))
    return "\n".join(lines)


async def post_event(event: dict) -> None:
    """Cross-post one published event to the events channel. Best effort and
    fire-and-forget: a publish must succeed whether or not Discord answers.

    Posts ONLY when DISCORD_CH_EVENTS is set. There is deliberately no fallback
    channel: botlogs is an operator feed, and dropping player-facing announcement
    copy into it is worse than posting nothing at all."""
    try:
        channel_id = (DISCORD_CH_EVENTS or "").strip()
        if not channel_id:
            return
        await post_to_channel(channel_id, _event_content(event))
    except Exception as exc:  # noqa: BLE001
        print(f"[discord_post] post_event failed: {exc}")
