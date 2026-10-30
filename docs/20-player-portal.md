# 20. The player portal

`admin-backend/` is the web application that sits on top of the control plane
in `docs/15`: a player-facing portal and the admin panel behind it, as one
FastAPI server with a SvelteKit front end. It is the code we run, published as
a snapshot (v0.7, refreshed in v0.8) and re-synced on a cadence rather than mirrored live: after
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
  with live positions and the Coriolis cycle, under the Sand Table overview
- self-rescue: a player stuck on a PvE map returns to their nearest base
- guild directory, recruiting, Landsraad board and house standings
- a PvP page for a PvP sietch: kills board, titles and streaks
- base backup vault, a 3D base blueprint viewer and a blueprint market
- chat, mailbox, events with reminders and the weekend event card, reports, and a
  help section

About 25 routes on the front end and roughly 120 portal API endpoints. Every
player action is idempotent, rate limited and audited, and every feature has a
switch.

## New in v0.8.1

- **Map instances from config.** Map instance names and partitions come from configuration and a status endpoint,
  so the front end holds no instance names. A closed map's old links fall back to an open map. See
  [Map instances](#map-instances) below.
- **Requests to staff.** Players file a request (a base relocation, a Deep Desert base, a vehicle recovery, an
  inactive base report, and similar) and follow its status in the portal; staff triage, note and close it in the admin panel's Requests queue. A player only ever sees their
  own requests, never the staff note. The portal side is off until `LASTSIETCH_PORTAL_REQUESTS_ENABLED` is set.
- **New navigation** for the new Home (`LASTSIETCH_PORTAL_SHELL_NEXT_ENABLED`): five sections on a rail, each
  section's pages as tabs, with the active tab in the URL so links and the back button land on the right page.
- **Gear stage.** The Character page shows the character's equipped set on a 3D stage, dyed in their colours. The
  garment models, the layer colours and the index maps are game-derived and **not shipped**, and there is no public
  builder for them; the API server serves them from `data/` to signed-in players only, and answers 404 without them.
- **Weekend banner art.** The weekend hero shows a per-effect art plate (served under `/img/v3/weekend/`, the
  effect-to-file map is in `src/lib/shellHome.js`); an open weekend keeps the map plate. Like the other portal
  images, the plates are not shipped: supply your own.

## New in v0.8

- **New Home**, dark, behind `LASTSIETCH_PORTAL_SHELL_NEXT_ENABLED` (off by default; the previous Home stays the
  default). It opens with one line for where the player stands, then wallet, storm, orders and Landsraad.
- **Sand Table.** One page for every map: a carved overview with a stone per location that lights when the map is
  online, and a click down into each map. Phones and reduced motion get a flat chart instead. The relief and the
  location miniatures are game-derived and **not shipped**, like the other baked maps above. The storm layer ships
  **off**: the storm data source is not part of this repository, and the layer says "no storm data"
  rather than guessing.
- **PvP page, board and titles** for a PvP sietch: this week's and this season's kills, deaths, K/D and best
  streak, who is on a streak now, and four titles. How the titles are computed and mirrored to Discord is in
  [22-amtal-pvp-sietch.md](22-amtal-pvp-sietch.md#pvp-board-titles-and-streak-bounties).
- **Self-rescue.** A player stuck on a PvE Hagga map, or in the PvE Deep Desert, sends themselves to their nearest
  base (or, in the Deep Desert, to an ornithopter pilot). The page shows where they will land before they send it.
  One rescue per account per hour, recorded in SQLite so it survives a deploy. **Never on a PvP map**: the server
  refuses it there, not just the button.
- **Weekend events card.** Home and Events show this weekend's and next weekend's event, what it does, and when it
  starts and ends in server time and the player's own. Events are created in the admin panel's Events Manager; the
  in-portal event composer is retired. The runner that applies an event's effect is
  [23-events-runner.md](23-events-runner.md).
- **Help rotation and rules.** The help section carries the weekend rotation and an expanded rules page. Player
  copy never promises more than the server enforces; keep yours the same way.
- **Fixes** from running the 1.5.x schema: the currency enum, exchange expiry, Claims, and the 1.5.3 schema readers.

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

Variables added in v0.8, all empty or off by default:

| Variable | What it does |
|---|---|
| `LASTSIETCH_PORTAL_SHELL_NEXT_ENABLED` | Serve the new Home |
| `LASTSIETCH_PORTAL_RUNTIME_DIR` | Where runtime state such as `feature_flags.json` is written |
| `LASTSIETCH_PORTAL_ALERT_POLL_KEY` | Shared key for the internal endpoint the Discord bot polls for market alerts |
| `PORTAL_ANNOUNCE_FILE` | Path to the JSON file behind the announcement banner |
| `PORTAL_SHOW_GEAR_DEMO` | Development only: the 3D gear viewer demo |
| `LASTSIETCH_EVENT_DISCORD_ENABLED` | Let the bot mirror portal events as Discord scheduled events |
| `LASTSIETCH_LANDSRAAD_CLAIM_ENABLED`, `LASTSIETCH_LANDSRAAD_CLAIM_ACCOUNTS` | The Landsraad reward claim panel, and who may use it (account ids, or `*`). The game host has its own gate file; both must agree |
| `LASTSIETCH_TRANSFER_RELINK_ENABLED`, `_APPLY`, `_INTERVAL`, `_MAX` | Re-point portal links from an account that left through a Funcom character transfer to the player's new account. `APPLY=0` logs only; `MAX` caps changes per tick |
| `LASTSIETCH_TELEMETRY_DB_PATH` | Read the telemetry SQLite directly on a single box |
| `LASTSIETCH_SIETCH_*` | The Seven Nights community event (nights flag file, award and role keys, contest ids, disabled shop items). Dark unless you run it |
| `LASTSIETCH_GAMES_OFFLINE_FILE`, `LASTSIETCH_RELAY_URL_GAMES` | Only for a relay that also serves other games. Leave empty |

Variables added in v0.8.1:

| Variable | What it does |
|---|---|
| `LASTSIETCH_MAP_INSTANCES_FILE` | The map instance config (below). Default `/etc/lastsietch/map-instances.json`; a missing file means the built-in defaults |
| `LASTSIETCH_PORTAL_REQUESTS_ENABLED` | The portal side of player requests to staff. Off by default |

Set the Coriolis anchor (`LASTSIETCH_CORIOLIS_ANCHOR`) to any past reset on
your server, or the spice-field cycle countdown will be wrong.

### Map instances

Which worlds your server runs, their player-facing names, partitions, dimensions, pod suffixes, and whether each is
PvP, closed or kept off public surfaces, live in one JSON file. The API server, the Discord bot and the game-host
jobs under `ops/` read it; the portal gets the open instances from `/portal/features`, so no instance name is
compiled into the front end.

1. Start from [`docs/examples/map-instances.example.json`](examples/map-instances.example.json). The field reference
   is at the top of `admin-backend/map_instances.py`.
2. Check it: `python3 admin-backend/map_instances.py --check map-instances.json` prints one line per instance or
   names the first problem.
3. Install it at `/etc/lastsietch/map-instances.json` (or set `LASTSIETCH_MAP_INSTANCES_FILE`) on the web host and
   the game host, then restart the API server and the bot. They read the file once at start.

A missing file means the built-in defaults, which are our instances. A file that fails the check also falls back to
the defaults and logs why, so a typo cannot take a map off the portal. The game-host jobs pick their entry with
`LASTSIETCH_MAP_INSTANCE_KEY`. Closing an instance is `"closed": true` plus the move in
[08-updates.md](08-updates.md#closing-a-map-instance).

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
- The Sand Table relief and miniatures are not shipped, and its storm layer is
  off (see "New in v0.8").
- The gear stage's models, layer colours and index maps, and the weekend banner art, are not shipped (see "New in
  v0.8.1").

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
