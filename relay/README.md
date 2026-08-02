# relay

The only component that talks to the game host. Everything above it goes
through here; nothing above it holds a key to the game box.

Read [docs/15-control-plane-architecture.md](../docs/15-control-plane-architecture.md)
first for why this exists at all.

## Status

The action scripts this relay routes to are now published too, in `scripts/`,
so the whole chain works: relay to SSH forced command to dispatcher allowlist
to action script to `dq.sh` to psql.

What is not here yet is the web front end that calls it. Drive it from your own
interface or from curl until the player portal lands.

Several routes write to a live game database. Read `docs/13-safe-database-writes.md`
before wiring any of them to a button.

## What is here

106 routes: 99 under `/dune`, 6 under `/server`, 1 health check.

This is the Dune-only relay. Our production one also carries Conan Exiles,
Enshrouded, and some hypervisor and storage management, which are not in scope for
this repository; those 45 routes were removed. The split was clean, along a
prefix boundary, with only the shared auth dependency crossing it.

## Configuration

| Variable | Meaning |
|---|---|
| `DUNE_SSH_HOST` | Game host address. **Required, no default**, so a misconfigured relay fails loudly rather than reaching somewhere unexpected |
| `RELAY_API_KEY` | Shared secret required on every request as `X-API-Key` |

Bind to localhost and reverse-proxy to it. Do not expose it directly.

## Auth

Every route depends on `verify_key`, which checks `X-API-Key`. That is
deliberately simple, because it is not the security boundary: the dispatcher's
allowlist is. A stolen relay key gets an attacker the fixed set of actions the
dispatcher permits, and nothing else.

If you expose this to anything less trusted than your own admin backend, put
real authentication in front of it.

## Running it

```bash
pip install -r requirements.txt
DUNE_SSH_HOST=<your-game-host> RELAY_API_KEY=<secret> \
  uvicorn app:app --host 127.0.0.1 --port 8077
```

A systemd unit is included. Adjust paths and user to your layout.
