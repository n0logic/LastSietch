# Amtal arrival whisper

Whispers every player arriving on **Amtal** (partition **33**, browser name
`Amtal-Full-PvP`) a one-line PvP warning, **every time they arrive**, base owners
included (Medic, 2026-10-17).

**Why:** Amtal is full PvP in the open world, but the in-game zone banner out
there still renders **PvE**. The banner is drawn client-side and the server does
not agree with it, so a player who trusts it can be killed by a rule their HUD
said did not apply. Every other Amtal surface carries the same warning; this one
reaches the player who never read Discord.

Status: **LIVE on <game-host>** (timer enabled). The every-login behaviour and
the current copy went live 2026-10-17 20:22Z. Pre-change backup:

## Files

| File | Deploys to |
|---|---|
| `watcher.sh` | `/opt/lastsietch-amtal-arrival-whisper/watcher.sh` |
| `systemd/amtal-arrival-whisper.service` | `/etc/systemd/system/` |
| `systemd/amtal-arrival-whisper.timer` | `/etc/systemd/system/` |
| `systemd/enable.conf` | `/etc/systemd/system/amtal-arrival-whisper.service.d/enable.conf` |

State: `/var/lib/amtal-arrival-whisper/warned.tsv` (`controller_id<TAB>epoch`),
holding only the players warned during their current stay on Amtal.
Herald output and errors: `/var/log/amtal-arrival-whisper.log`. Sweep log: journal.

## How it works

One sweep per invocation, fired every 60s by the timer — the same shape as
`lastsietch-welcome-pack-watcher`. Per sweep:

1. Query online players standing in partition 33.
2. Drop state rows for anyone not in that result (logged off or left the
   partition). Their next arrival is a new one and gets warned again. This only
   runs after a successful query, so a DB blip cannot re-warn everyone.
3. Skip anyone still in the state file (already warned this stay).
4. Whisper the rest via the Cielago herald, then record them.

A failed or partial whisper is **not** recorded, so it retries next sweep.
Amtal keeps a logged-out character in the world for about 5 minutes
(`logoff_persistence_end_time`), so a relog inside that window is not a new
arrival.

### The query

```sql
SELECT eps.player_controller_id, a.funcom_id
  FROM dune.encrypted_player_state eps
  JOIN dune.actors act ON act.id = eps.player_pawn_id
  JOIN dune.accounts a ON a.id = eps.account_id
 WHERE act.partition_id = 33
   AND eps.online_status <> 'Offline'
   AND eps.character_state IS DISTINCT FROM 'Deleted';
```

Run through `sudo /root/dq.sh -t -A -F'|'`, which pipes straight to `psql` in the
DB pod. Read-only.

### Controller id to FuncomId — resolved, not guessed

The herald routes whispers by **FuncomId** (`Display#tag`), so the sweep resolves
it in the same query: `eps.account_id` joins `dune.accounts.funcom_id`. That is
the identical path `resolve_funcom_id()` uses in the deployed welcome-pack
watcher. The controller id is only the dedup key; it is never sent to the herald.
The display name in the whisper is the FuncomId up to the `#`.

Verified live 2026-08-27 against a populated partition (partition 1, so no
Amtal player was touched):

```
$ sudo /root/dq.sh -t -A -F'|' -c "<query above, partition_id = 1>"
130028|Solidan#89098
```

One pipe, no padding, no trailing whitespace — which is what the read loop parses.

## Deploy

Nothing below runs anything against players until step 5.

```bash
# 1. copy
scp watcher.sh <game-host>:/tmp/amtal-watcher.sh
ssh <game-host> 'sudo mkdir -p /opt/lastsietch-amtal-arrival-whisper /var/lib/amtal-arrival-whisper \
  && sudo install -m 0755 /tmp/amtal-watcher.sh /opt/lastsietch-amtal-arrival-whisper/watcher.sh'

# 2. units
scp systemd/amtal-arrival-whisper.{service,timer} <game-host>:/tmp/
ssh <game-host> 'sudo install -m 0644 /tmp/amtal-arrival-whisper.service /tmp/amtal-arrival-whisper.timer \
  /etc/systemd/system/ \
  && sudo mkdir -p /etc/systemd/system/amtal-arrival-whisper.service.d'
scp systemd/enable.conf <game-host>:/tmp/
ssh <game-host> 'sudo install -m 0644 /tmp/enable.conf \
  /etc/systemd/system/amtal-arrival-whisper.service.d/enable.conf && sudo systemctl daemon-reload'

# 3. DRY RUN against Amtal. Sends nothing; logs who it would whisper.
ssh <game-host> 'sudo /opt/lastsietch-amtal-arrival-whisper/watcher.sh --dry-run'

# 4. dry run recorded whoever it "warned" — clear the state so the real
#    first sweep does not skip them.
ssh <game-host> 'sudo truncate -s 0 /var/lib/amtal-arrival-whisper/warned.tsv'

# 5. GOES LIVE. One sweep by hand first, watch it, then arm the timer.
ssh <game-host> 'sudo systemctl start amtal-arrival-whisper.service && \
  sudo journalctl -u amtal-arrival-whisper -n 40 --no-pager'
ssh <game-host> 'sudo systemctl enable --now amtal-arrival-whisper.timer'
```

## Kill switch

Any one of these stops delivery. First is fastest.

```bash
ssh <game-host> 'sudo touch /opt/lastsietch-amtal-arrival-whisper/DISABLED'      # instant, no reload
ssh <game-host> 'sudo systemctl disable --now amtal-arrival-whisper.timer'
# or set Environment=AMTAL_WHISPER_ENABLED=0 in enable.conf + systemctl daemon-reload
```

The watcher only ever SELECTs and whispers. It writes nothing to the game DB and
touches no pods, so disabling it is the whole rollback.

## Knobs

| Env | Default | Meaning |
|---|---|---|
| `AMTAL_WHISPER_ENABLED` | `1` | `0` disables delivery |
| `AMTAL_PARTITION` | `33` | Partition to watch; override to rehearse elsewhere |
| `AMTAL_DQ` / `AMTAL_HERALD` / `AMTAL_STATE_DIR` / `AMTAL_LOG_FILE` | on-host paths | Off-host testing only |

## Whisper copy

Medic's wording, verbatim (2026-10-17). Medic asked for this statement alone, with
nothing added:

```
Welcome to Amtal! 🏜️ Sandworms aren't the only thing hunting you out here. Stay safe in Helius Gate and Riftwatch — everywhere else, it's every Sleeper for themselves.
```

Edit `AMTAL_WHISPER_PARTS` in `watcher.sh`. Nothing else needs changing. A `%s`
in a part is replaced by the recipient's display name.

## Open checks

1. **Emoji rendering in game chat is unconfirmed.** If 🏜️ shows as a box, drop it.
2. **Whisper length limit is still unconfirmed**, but this line (162 chars) is
   shorter than parts that are known to deliver.
3. **Whispers only render to an online client.** A player still on the loading
   screen may miss it. If that happens, warn on the second consecutive sighting.

## Rehearsal (2026-10-17, every-login change)

Off-host with a stub DQ:
- A present player who was already warned is skipped.
- A new player is whispered.
- A gone player's row is dropped.
- A repeat sweep sends nothing (control).
- After logging off, the player's row drops; logging back in re-warns them.
- An empty partition clears the state.
- A failed query leaves the state untouched.

On-host dry run with a throwaway state dir named the real player on Amtal. The
first live sweep pruned the state to that one player, who was already warned,
so nothing was sent.
