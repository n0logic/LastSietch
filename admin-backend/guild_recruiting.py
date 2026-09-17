"""Guild recruiting metadata: portal-side CRUD for the "we're recruiting" flag,
the portal-authored recruitment blurb, and an optional contact note.

This is OUR metadata, not game-state. A guild Leader/Officer (authorization is
enforced in the route via the live dune.guild_members role check) flips their
guild "recruiting" and writes a short blurb; it is then surfaced read-only to
every player on the portal Guild directory. The actual guild join still happens
in-game. All state lives in admin.db (portal_guild_recruiting). Fully read-only
on the game DB.
"""
from __future__ import annotations

from datetime import datetime, timezone

from database import get_db

BLURB_MAX = 600
CONTACT_MAX = 200
# Structured Signal-Board filter fields (social layer Tier 2).
PLAYSTYLE_MAX = 40
TIMEZONE_MAX = 40
LANGUAGE_MAX = 40
DISCORD_URL_MAX = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Only https:// Discord-invite hosts are storable — blocks javascript:/data: and
# other scheme-based stored-XSS vectors (the render side adds its own guard).
_DISCORD_URL_HOSTS = ("discord.gg", "discord.com", "www.discord.com",
                      "discordapp.com", "www.discordapp.com")


def _clean_discord_url(url: str | None) -> str | None:
    url = (url or "").strip()[:DISCORD_URL_MAX]
    if not url:
        return None
    low = url.lower()
    if not low.startswith("https://"):
        return None
    host = low[len("https://"):].split("/", 1)[0].split(":", 1)[0]
    if host not in _DISCORD_URL_HOSTS:
        return None
    return url


def get_recruiting(guild_id: int) -> dict | None:
    """The recruiting record for one guild, or None if never set."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM portal_guild_recruiting WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_all() -> dict[int, dict]:
    """Every recruiting record, keyed by guild_id. Used to merge metadata into
    the live guild directory in one pass."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM portal_guild_recruiting").fetchall()
    finally:
        conn.close()
    return {int(r["guild_id"]): dict(r) for r in rows}


def set_recruiting(guild_id: int, *, recruiting: bool, blurb: str | None,
                   contact_note: str | None, account_id: int,
                   discord_id: str, char_name: str,
                   playstyle: str | None = None, timezone: str | None = None,
                   language: str | None = None,
                   new_player_friendly: bool = False,
                   discord_url: str | None = None) -> dict:
    """Upsert the recruiting flag + blurb + contact + structured Signal-Board
    filters for a guild. Authorization (Leader/Officer of this guild) MUST be
    checked by the caller. Free-text fields are trimmed and length-capped here.
    Returns the resulting record."""
    blurb = (blurb or "").strip()[:BLURB_MAX] or None
    contact_note = (contact_note or "").strip()[:CONTACT_MAX] or None
    playstyle = (playstyle or "").strip()[:PLAYSTYLE_MAX] or None
    timezone = (timezone or "").strip()[:TIMEZONE_MAX] or None
    language = (language or "").strip()[:LANGUAGE_MAX] or None
    discord_url = _clean_discord_url(discord_url)
    now = _now_iso()
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO portal_guild_recruiting
                   (guild_id, recruiting, blurb, contact_note,
                    set_by_account_id, set_by_discord_id, set_by_char_name,
                    playstyle, timezone, language, new_player_friendly, discord_url,
                    created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   recruiting          = excluded.recruiting,
                   blurb               = excluded.blurb,
                   contact_note        = excluded.contact_note,
                   set_by_account_id   = excluded.set_by_account_id,
                   set_by_discord_id   = excluded.set_by_discord_id,
                   set_by_char_name    = excluded.set_by_char_name,
                   playstyle           = excluded.playstyle,
                   timezone            = excluded.timezone,
                   language            = excluded.language,
                   new_player_friendly = excluded.new_player_friendly,
                   discord_url         = excluded.discord_url,
                   updated_at          = excluded.updated_at""",
            (int(guild_id), 1 if recruiting else 0, blurb, contact_note,
             int(account_id), discord_id, char_name,
             playstyle, timezone, language, 1 if new_player_friendly else 0,
             discord_url, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_recruiting(guild_id)
