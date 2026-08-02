#!/usr/bin/env python3
"""One-shot: at Hagga Basin PvP poll close, read the FINAL result, record it,
and DM the owner the winning outcome + the ready-to-run staged command.

READ-ONLY: this never edits game config. Applying the change stays gated to an
announced empty-server window via ops/stage-hagga-pvp.sh (the owner approval).

Runs as the cielago service user from /opt/cielago (loads the bot token from
/opt/cielago/.env). Scheduled by a transient systemd timer for ~5 min after the
poll's expiry (2026-06-15 04:40:40 UTC)."""
import json
import os
from datetime import datetime, timezone

import discord
from dotenv import load_dotenv

load_dotenv("/opt/cielago/.env")
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))        # dune-general
POLL_MSG_ID = int(os.environ.get("POLL_MSG_ID", "0"))       # "What should Hagga Basin PvP look like?"
OWNER_ID = 215146359479730176           # the owner
RESULT_PATH = "/opt/cielago/data/hagga-poll-result.json"

# poll option text (lowercased) -> stage-hagga-pvp.sh --outcome value
OUTCOME_MAP = {
    "kulon pvp (habbanya stays pve)": "kulon",
    "habbanya pvp (kulon stays pve)": "habbanya",
    "both maps pvp": "both",
    "keep both pve": "pve",
}

intents = discord.Intents.default()
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    try:
        ch = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
        msg = await ch.fetch_message(POLL_MSG_ID)
        poll = msg.poll
        answers = []
        for a in poll.answers:
            txt = getattr(a, "text", None)
            txt = getattr(txt, "text", txt)
            answers.append({"text": str(txt), "votes": int(getattr(a, "vote_count", 0))})
        answers.sort(key=lambda x: x["votes"], reverse=True)
        top = answers[0]
        tie = len(answers) > 1 and answers[1]["votes"] == top["votes"]
        outcome = OUTCOME_MAP.get(top["text"].strip().lower(), "UNKNOWN")
        if tie:
            outcome = "TIE"
        finalised = getattr(poll, "is_finalised", None)
        finalised = finalised() if callable(finalised) else finalised

        result = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "question": str(getattr(poll.question, "text", poll.question)),
            "expires_at": str(getattr(poll, "expires_at", None)),
            "is_finalised": finalised,
            "answers": answers,
            "winner": top["text"],
            "outcome": outcome,
            "tie": tie,
            "total_votes": sum(a["votes"] for a in answers),
        }
        with open(RESULT_PATH, "w") as f:
            json.dump(result, f, indent=2)
        print("RESULT:", json.dumps(result))

        lines = [
            "**Hagga Basin PvP poll closed.**",
            f"Q: {result['question']}",
            f"Finalised: {finalised} | total votes: {result['total_votes']}",
        ]
        for a in answers:
            mark = "  <- winner" if (a["text"] == top["text"] and not tie) else ""
            lines.append(f"  - [{a['votes']}] {a['text']}{mark}")
        if tie:
            lines.append("WARNING: TIE for first place - you pick the outcome.")
            lines.append("Then: ops/stage-hagga-pvp.sh apply --outcome <kulon|habbanya|both|pve>")
        elif outcome == "UNKNOWN":
            lines.append("WARNING: winning option text did not map to a known outcome - check manually.")
        elif outcome == "pve":
            lines.append("Outcome: KEEP both PvE -> no change needed (no-op). Nothing to stage.")
        else:
            lines.append(f"Outcome: {outcome.upper()} -> at the next announced empty-server window run:")
            lines.append(f"`ops/stage-hagga-pvp.sh apply --outcome {outcome}` (House0fL0gic repo)")
            lines.append("See our internal design notes for the approval gate + steps.")
        try:
            user = client.get_user(OWNER_ID) or await client.fetch_user(OWNER_ID)
            await user.send("\n".join(lines))
            print("DM sent to owner")
        except Exception as e:  # noqa: BLE001
            print("DM failed:", e)
    finally:
        await client.close()


client.run(TOKEN)
