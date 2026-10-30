# Cielago — Last Sietch Discord Bot

> *"The voice that carries across the sand."*

All-in-one Discord administration bot for the Last Sietch server. Named after the cielago — the desert bat the Fremen imprint with distrans messages to carry word across Arrakis. Fittingly, it's also our in-game chat herald that greets new players and posts server news.

## Mission (admin-first build order)

1. **Phase 1 — Admin scaffolding** (built)
   - Bot connects, recognizes admin users
   - `/role-create`, `/role-bootstrap`, `/role-list`, `/role-assign` slash commands
   - `/channel-template` (builds the full layout from `data/server_layout.json`) + `/channel-create`
   - `/ping` health check
   - Audit log: every admin action posts to the private audit channel + appends JSONL on disk
2. **Phase 2 — Member onboarding**
   - Welcome card with an opt-in PvE/PvP sietch role (`Habbanya` / `Amtal`) for notices; no role ladder, nothing gated
3. **Phase 3 — Game integration**
   - Dune: Awakening server status (live/down, player count)
   - Optional Discord ↔ in-game account linking via OAuth
4. **Phase 4 — Community features**
   - Wormsign-style notifications for game events
   - Spice tracking, leaderboards, etc.

## Admins (initial)

- **Owner** (founder)
- **Co-founder**

> Additional admins added by mutual founder approval. Bribery accepted in spice melange (off-ledger).

## Stack

- **Python 3.12+** managed with `uv`
- **`discord.py` 2.4+** (matches existing internal ops tooling)
- **`pydantic-settings`** for typed env config
- **`structlog`** for structured logging to JSONL
- Slash commands only (no prefix commands)

## Layout

```
discord/bot/
├── README.md           — this file
├── pyproject.toml      — uv project + deps
├── .env.example        — copy to .env and fill in
├── .gitignore
└── src/
    ├── cielago/
    │   ├── __init__.py
    │   ├── bot.py          — entry point, loads cogs
    │   ├── config.py       — pydantic-settings env loader
    │   ├── permissions.py  — admin-check decorator
    │   ├── audit.py        — JSONL + audit-channel logging helper
    │   ├── data/
    │   │   └── server_layout.json  — category/channel layout (edit this)
    │   └── cogs/
    │       ├── __init__.py
    │       ├── admin.py     — /role-* and /ping
    │       ├── channels.py  — /channel-template, /channel-create
    │       ├── connect.py   — /connect, /mods (server connection info)
    │       ├── giveaways.py — /giveaway start|reroll|end|list|announce|weekend
    │       └── onboarding.py — on_member_join welcome
    │       # status.py (Phase 3 server status) lands later
    └── tests/
        ├── test_layout.py
        └── test_giveaways.py
```

## Member / game commands

Folded in from the old `jtc-bot` (the Node bot it replaces):

- `/connect <dune|conan|enshrouded>` — connection info for a Last Sietch game server.
  Passwords are never embedded; players are pointed to ask in Discord (matches the site).
- `/mods` — the required Conan Exiles Steam Workshop loadout.
- `/giveaway` (admin only) — full giveaway suite:
  - `start <prizes> <duration_hours> [channel]` — button-entry giveaway, auto-ends on a timer.
  - `weekend <games> <drawing "YYYY-MM-DD HH:MM" Eastern> [channel] [platform]` — weekend
    key giveaway with a 24h reminder and auto-drawing.
  - `announce <timing> <games> <time> <draw_time> <week> <total_weeks> [channel] [platform]` —
    pre-announcement post (no button).
  - `reroll <message_id>` / `end <message_id>` / `list`.

  Entries use a `DynamicItem` button keyed by the giveaway message ID, so the **Enter** button
  keeps working across bot restarts. State persists to `CIELAGO_GIVEAWAY_DATA_PATH`
  (`data/giveaways.json`); a 30s background loop ends due giveaways and fires weekend reminders.

**Event contests** (`cogs/contests.py`, rules in `cielago/contests.py`): one pinned panel in
🏆｜server-events with a **Submit entry** button (modal: one image, a title, an optional caption).
Each entry becomes a bot-posted card with a 🏮 **Vote** button; one entry and one vote per member
per contest (a resubmission replaces the old card; voting again moves or takes back the vote).
Tallies stay hidden until voting closes, then a results report with **Confirm and pay** goes to
the mod channel; nothing is paid or announced until staff confirm.

- **Contests are data**: `src/cielago/data/contests.json` (local times + IANA zone). Add or edit a
  row and restart Cielago. The id is part of every payout case id; never rename one that has run.
- **State** (entries, votes, notices sent, payouts, panel id) persists to
  `CIELAGO_CONTEST_DATA_PATH` (`data/contests-state.json`).
- **Rewards** go through a lane in `contests.LANES`; only `ls_tokens` exists. It calls lastsietch-admin
  `POST /_internal/sietch/contest-award` (case ids `contest:<id>:entry:<discord_id>` and
  `contest:<id>:place:<n>`, idempotent) with `X-Sietch-Award-Key` = `CIELAGO_CONTEST_AWARD_KEY`,
  which must equal `LASTSIETCH_SIETCH_AWARD_KEY` on lastsietch-admin (unset there: 503). lastsietch-admin also pays
  only contest ids listed in `LASTSIETCH_SIETCH_CONTEST_IDS` (comma-separated, e.g. `halloween-2026`)
  and places 1 to 3; add a new contest's id there before its confirm.
- Off unless `CIELAGO_CONTESTS_ENABLED=1` and `CIELAGO_CONTEST_CHANNEL_ID` are set.
- Staff commands (`/contest panel|status|remove|results|confirm`) need Manage Server, an admin id
  or the mod role. `confirm` is safe to repeat: it retries only cases that are not yet paid.

**Join-to-Create voice** (`cogs/voice.py`): joining the trigger voice channel spawns a temp
channel named after the member's current game (uses the **Presence** intent, now enabled in code
and the dev portal), grants the creator channel-manage perms, and deletes the channel 30s after it
empties. Profane channel renames are auto-reverted (`better-profanity`) and the owner is DM'd.
Orphaned temp channels are cleaned up on startup. The trigger channel is `CIELAGO_JTC_TRIGGER_CHANNEL_ID`,
or auto-detected by the "Join to Create" voice channel that `/channel-template` creates.

## Cielago Assistant (support triage)

`cogs/assistant.py` + the `cielago/assistant/` package: a mod-facing support
triage layer (Phase 0/1, **no external LLM**). Supersedes the old tracker cog.

- **Watches** the configured help channels. Each substantive message is keyword-
  classified (bug / feature / question / player-report / chatter) and severity-
  triaged (urgent → mod ping).
- **Dedup before filing.** A local embedding (bge-small ONNX when staged, else a
  deterministic hashing fallback) is cosine-matched against open tickets / FRs. A
  near-duplicate bumps the existing item's report count or vote instead of filing
  a new one.
- **Ticket lifecycle** in the mod channel: **Claim / Escalate to owner / Resolve
  / Merge** buttons (restart-surviving `DynamicItem`s), plus `/ticket list|show|
  close`.
- **Feature requests** post with a 👍 reaction; votes aggregate live.
- **State** lives in `support.sqlite` (WAL) only — the bot never writes game data.
- **Kill switch**: `/assistant disable` (persisted) stops all auto-triage;
  `/assistant enable` clears it. `/assistant status` shows health + queue counts.
- **Embeddings** are an optional extra (`uv sync --extra embeddings`); see the
  deploy doc for staging the bge-small ONNX model. Without it, dedup uses the
  hashing fallback and the bot still runs.

Migrating from the tracker is automatic: the tracker JSON is imported into
`support.sqlite` on first load (bugs → tickets, features → feature requests).

## Setup (local dev)

```bash
cd discord/bot
uv sync
cp .env.example .env
# edit .env with DISCORD_BOT_TOKEN and admin Discord IDs
uv run python -m cielago.bot
```

## Discord Bot Application Setup

1. Create app at https://discord.com/developers/applications
2. Bot section → enable `Server Members Intent`, `Message Content Intent`
3. OAuth2 → URL Generator → scopes: `bot`, `applications.commands`
4. Permissions: `Administrator` for v1 (refine later)
5. Invite to Last Sietch server, paste token into `.env`

## Internal-only References

- Existing OAuth portal pattern (shipped 2026-05-25 on the internal parent guild's stack) — reuse for Phase 2 onboarding
- Existing `discord.py` cog structure from internal bot project

## Operating Principles

- **Idempotent commands.** `/role create` on an existing role should report "already exists" not fail.
- **Audit everything.** Every admin action logs to a private audit channel + JSONL on disk.
- **Soft-fail on permission errors.** User-friendly ephemeral error messages, never expose stack traces.
- **No DM commands.** All commands run in-server, scoped to a channel.
