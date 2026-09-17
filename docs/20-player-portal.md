# 20. The player portal

`admin-backend/` is the web application that sits on top of the control plane
in `docs/15`: a player-facing portal and the admin panel behind it, as one
FastAPI server with a SvelteKit front end. It is the code we run, published as
a snapshot (v0.7) and re-synced on a cadence rather than mirrored live: after
each Funcom update that changes what the action scripts touch, and otherwise
about monthly. Expect it to trail our production copy by a few weeks.

Everything here assumes you already have the relay, the dispatcher and the
action scripts from `docs/15` in place. The portal cannot reach the game host
any other way, by design.

## What players get

Players sign in with Discord, prove they own an in-game character, and then get
a live picture of the server plus a set of actions that would otherwise need an
admin:

- character, inventory and equipped-gear views, with augment reroll and swap
- daily and weekly login rewards, claimed in the portal and delivered in game
- the CHOAM exchange, browsable by category, with sell-from-backpack
- storage moves between a base container, the bank and vehicles
- the Karum, a player-to-player trading venue with escrow, and Solari sends
- coordinate-accurate Deep Desert and Hagga Basin maps from your own database,
  with live positions, spice fields, storms and the Coriolis cycle
- guild directory, recruiting, Landsraad board and house standings
- base backup vault, a 3D base blueprint viewer and a blueprint market
- chat, mailbox, events with reminders, reports, and a help section

About 25 routes on the front end and roughly 120 portal API endpoints. Every
player action is idempotent, rate limited and audited, and every feature has a
switch.

## How it is built

**Front end.** SvelteKit 2 on Svelte 5, Vite 6, `adapter-static`. The build is
a static bundle the API server serves; there is no Node process in production.
three.js renders the 3D maps. No UI framework and no CSS framework.

**Back end.** Python, FastAPI on uvicorn, Jinja2 for the server-rendered admin
pages. The application's own state (accounts, sessions, character links,
preferences, reward ledgers, mirrors, caches) lives in SQLite. The server never
opens a connection to the game database.

**How it touches the game.** Browser to your TLS proxy, to this API server,
which calls the relay over an API key, which reaches the game host only over
SSH with a forced-command dispatcher, which allows a fixed list of on-host
action scripts. Neither the web app nor the relay can run arbitrary SQL. Reads
that must be fresh (positions, chat, server status) come from the telemetry
service in `dune-telemetry/`.

**Auth.** Discord OAuth is the primary sign-in, with passkeys (WebAuthn) and
Argon2 password accounts as alternatives. Sessions are signed server-side, every
state change carries a CSRF token, and the character-link flow proves ownership
of a character before any action is allowed, optionally with a short quiz for
multi-account cases.

## What ships, what you regenerate, what you supply

This repository never contains anything extracted from the game's package
files, so the portal is code plus a small set of curated tables. Three classes:

| Class | Where | How you get it |
|---|---|---|
| Code, templates, styles, the three.js helper modules, licence texts (`admin-backend/THIRD_PARTY_LICENSES/`) | shipped | `git clone` |
| Curated tables that are ours: house crest map, Deep Desert spice sites, the gameplay-settings knob catalog, vehicle packages, grant presets, the keystone catalog, canonical cvar values | shipped in `admin-backend/data/` | `git clone` |
| Item icons, names, durability, exchange categories, the grant picker catalog, map glyphs, stat glyphs | regenerated | `scripts/build-portal-assets.py` |
| The fonts (JetBrains Mono and Saira, OFL), the three.js core (MIT), placeholder PWA icons and favicon | fetched or drawn | the same builder; this repository holds no binary files |
| Map marker snapshots (POIs, resource nodes, stations) for the four maps | regenerated from your database | `scripts/dune-markers-export.py` then the same builder |
| Baked 3D relief, Deep Desert island backdrops, Hagga and hub map backdrops, house crest and faction images, hero art, posters, screenshots, your own app icons | not provided | yours to make; the portal renders without them |

The regenerated class comes from the awakening.wiki community API, whose item
data is sourced from the game files and published for exactly this kind of use.
Its content licence (CC BY-NC-SA) binds the downloaded files, which is why the
builder exists instead of a directory of PNGs in this repository. Coverage is
every item a player can see or trade, about 2,300 templates. Templates the wiki
does not carry show the generic unknown glyph and a name synthesised from the id.

Without the "not provided" class: the 3D maps stay on their seeded relief, the
Deep Desert board draws a plain sand backdrop, the hub maps plot markers on a
dark field, the Landsraad board shows house monograms instead of crests, and the
hero slots are empty. Nothing fails. The wiki carries city and overland maps
that could serve as backdrops, but each one needs calibrating (two known
landmarks give the affine transform in `map_model.py`) before its markers land
in the right place, so they are not wired in.

## Install

Prerequisites: Debian 12 (what we run), Python 3.11 or newer, Node 20 or newer
for the one-time front-end build, a TLS proxy (we use Caddy), and the relay
from `docs/15` reachable from this host.

### 1. API server

```bash
sudo mkdir -p /opt/lastsietch-admin && sudo chown "$USER" /opt/lastsietch-admin
cp -r admin-backend/. /opt/lastsietch-admin/
cd /opt/lastsietch-admin
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill in the four required values in `.env` (relay URL and key, session secret,
database path) and the Discord OAuth block. Every other default fails closed:
the server boots with the admin surface working and each player-facing feature
dark until you switch it on. Generate secrets with
`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`, one per
secret, never shared between two variables.

First start, in the foreground, to see it come up:

```bash
./venv/bin/uvicorn main:app --host 127.0.0.1 --port 8078
```

The database is created on first start. With no users yet, `/setup` creates
the first admin account (afterwards it refuses, and `/login` is the door); do
that before exposing the port. With no relay URL set, the background samplers
and live refreshers stay off and say so once at startup, so an unconfigured
server is quiet rather than noisy.

### 2. Front end

```bash
cd /opt/lastsietch-admin/portal-nextgen
npm ci
npm run build          # writes build/, served by the API server
```

The API server serves `build/` at the root of every host listed in
`PORTAL_ROOT_HOSTS` and under `/portal/v2/` everywhere else. Rebuild after
every update to the source; the service worker versions itself from
`package.json`, so bump the version when you ship a change.

For local development, `vite.config.js` proxies the API prefixes to a live
server. Its target is our domain; point it at yours.

### 3. Assets

On the web host, with network access:

```bash
python3 scripts/build-portal-assets.py --out /opt/lastsietch-admin
```

It fetches the item list (three requests), writes the JSON sidecars, then
downloads about 2,000 icons plus the map and stat glyphs, the six font files
from Google Fonts, the three.js core from its release, and draws placeholder
app icons (concentric rings; replace them with your own PNGs of the same
names). Allow several minutes the first time; later runs only fetch what is
missing. Add `--skip-icons` for the tables alone, `--force` to refresh
everything after a game update. Run it BEFORE the front-end build so the
fonts and icons are in place when `build/` is written.

For the map markers, on the game host:

```bash
sudo python3 scripts/dune-markers-export.py --maps 1,7,9,11 -o markers.json
```

Copy `markers.json` to the web host and run the builder once more with
`--markers-export markers.json`. The Deep Desert layer changes every Coriolis
reset, so re-export it then; the other three maps are static.

### 4. Service and proxy

`admin-backend/lastsietch-admin.service` is the unit we run. Adjust the paths,
install it under `/etc/systemd/system/`, then:

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now lastsietch-admin
curl -sI http://127.0.0.1:8078/login | head -1     # HTTP/1.1 200 OK
```

Caddy, minimal:

```
portal.<your-domain> {
    reverse_proxy 127.0.0.1:8078
}
```

Set `PORTAL_ROOT_HOSTS=portal.<your-domain>` so the portal answers at that
host's root, and register `https://portal.<your-domain>/portal/oauth/callback`
as the redirect in your Discord application (scope `identify`).

### Single-box mode

Our split exists because the game host is a remote box and the web tier needed
a stable egress address. Everything here runs on one machine as well:

- **Ports.** This server and the telemetry API both default to `8078`. Set
  `LASTSIETCH_ADMIN_PORT` in `.env` (the unit reads it) or move the telemetry
  API's `API_PORT`, and point the proxy at whichever you chose.
- **Relay.** Run it on the same host and set
  `LASTSIETCH_RELAY_URL=http://127.0.0.1:8077`. Keep the forced-command SSH hop
  even to `localhost`: the value is the allowlist, not the network boundary
  (`docs/15`, "Single host").
- **Telemetry.** With the collector on this host, `LASTSIETCH_TELEMETRY_DB_PATH`
  lets the admin monitor read its SQLite directly instead of going through the
  relay.

## Configuration

`.env.example` documents every variable, grouped: required, listen port,
Discord OAuth, passkeys, feature switches, caps and windows, the Coriolis cycle
anchor for your server, paths, the read-model mirror, Discord notifications,
the metrics sampler, and the QA harness (leave that empty in production).

Feature switches are read at startup from the environment and can be flipped at
runtime from the admin Systems panel, which writes `feature_flags.json` into
`LASTSIETCH_PORTAL_RUNTIME_DIR`. A runtime flip wins over the environment.
Several features have a second switch on the game host, a flag file such as
`/etc/lastsietch/reward-enabled` that the action script itself checks
(`docs/12`). Both must be on for a write to land, which is deliberate: the web
tier can offer a door, but only the host can open it.

Set the Coriolis anchor (`LASTSIETCH_CORIOLIS_ANCHOR`) to any past reset on
your server, or the spice-field cycle countdown will be wrong.

## Safety model

Read `docs/13` first. The portal applies its rules:

- **Giving is online-safe, taking is not.** Inserting into a bank renders at the
  next zone transition, so rewards and gifts are always allowed. Anything that
  removes or moves an item out of a loaded inventory is gated on the character
  being offline, or goes through the game's own stored procedures.
- **Idempotent writes.** Every claim, gift, move and trade carries a
  deterministic key derived from who, what and when, so a double click, a
  retry and a concurrent submit collapse to one grant on the host.
- **Rate limits and audit.** Per-account and per-pair limits on gifts, caps on
  repairs and blueprint publishing, and an audit row for every action.
- **Kill switches.** Every feature ships dark and stays dark until switched on;
  a switch off is immediate and needs no restart.
- **No database connection.** The server holds no game-database credential.
  Everything goes through the relay's fixed vocabulary.

## Operating

- **Backups.** `admin.db` is SQLite in WAL mode. Copying the file misses the
  write-ahead log and restores stale data; use
  `sqlite3 admin.db "VACUUM INTO '/backup/admin-$(date +%F).db'"` and back up
  the result. The same goes for `market_history.db` and `mirror.sqlite`.
- **Deploys.** Stop, copy, restart. Static assets carry content hashes, and the
  service worker version comes from `package.json`, so a version bump per
  release is enough for clients to refresh.
- **Game updates.** Re-run the asset builder, and read the action scripts'
  notes in `docs/14`: Funcom changes schema between updates (1.5.3 turned the
  currency id into an enum and removed a helper function, which broke every
  Solari write until the scripts were patched). Check the scripts before
  reopening the portal after an update.
- **Logs.** `journalctl -u lastsietch-admin -f`. Missing sidecars log one
  warning each at startup and degrade; they are not errors.

## Known limits of the public snapshot

- The wiki has no augment items, so the exchange's Augments tab relies on the
  id-stem heuristic in `market_categories.py`.
- The grant picker catalog (`dune-give-item-catalog.json`) is derived from the
  wiki's typed fields. Measured against our game-file-derived table on the
  1,174 shared items: the coarse armour/tool/weapon split agrees on 95 percent,
  the fine category on 85 percent (light versus heavy armour schematics is the
  main gap). Nothing depends on the fine value beyond grouping in the picker.
- Hub map backdrops and the Hagga backdrop are not shipped and the wiki maps
  are uncalibrated (see above).
- Two stat glyphs (level, unspent skill points) have no wiki source and show as
  text.

## Before you go live: branding points

The staging gate replaces our deployment specifics with placeholders, but the
project name and a few of our URLs are part of the product copy. Search and
replace before you publish your own instance:

- `lastsietch.com` (about 25 files): the public site links, the OpenGraph
  tags in `portal-nextgen/src/app.html`, `SITE` in `nextgen_unfurl.py`,
  `EVENT_URL` in `event_reminders.py` and `discord_post.py`, `PORTAL_V2_SITE`
  in `routers/solido.py`, and the dev proxy target in `vite.config.js`.
- `https://discord.gg/your-invite` and `discord.com/channels/<guild-id>/<channel-id>`:
  your community links, in `config.py`, `templates/portal/base.html` and two
  Svelte routes.
- "Last Sietch" as the server name in UI copy and the help section.
