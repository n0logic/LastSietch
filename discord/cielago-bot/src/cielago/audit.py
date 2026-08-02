import json
from datetime import UTC, datetime
from pathlib import Path

import discord
import structlog

from cielago.config import settings

log = structlog.get_logger()

_SKIP_FIELDS = {"ts", "action", "actor", "actor_id", "guild_id"}


def _build_embed(entry: dict) -> discord.Embed:
    embed = discord.Embed(title=f"Cielago · {entry['action']}", color=0xA4441F)
    try:
        embed.timestamp = datetime.fromisoformat(entry["ts"])
    except (KeyError, ValueError):
        pass
    if entry.get("actor"):
        embed.add_field(name="by", value=f"{entry['actor']} (`{entry['actor_id']}`)", inline=False)
    for key, value in entry.items():
        if key in _SKIP_FIELDS:
            continue
        embed.add_field(name=key, value=str(value)[:1024] or "—", inline=False)
    return embed


async def audit(guild: discord.Guild | None, action: str, actor=None, **fields) -> None:
    """Record an admin action: append a JSONL line to disk and post to the audit
    channel. Soft-fails on every path so a logging problem never breaks a command."""
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "action": action,
        "actor_id": getattr(actor, "id", None),
        "actor": str(actor) if actor is not None else None,
        "guild_id": getattr(guild, "id", None),
        **fields,
    }

    try:
        path = Path(settings.cielago_audit_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        log.warning("audit.disk_failed", action=action, exc_info=True)

    try:
        if settings.cielago_audit_channel_id and guild is not None:
            channel = guild.get_channel(settings.cielago_audit_channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send(embed=_build_embed(entry))
    except Exception:
        log.warning("audit.channel_failed", action=action, exc_info=True)

    log.info("audit", **entry)
