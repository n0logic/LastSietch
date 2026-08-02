# 15. Control plane architecture

How a web application safely drives a live game server it must never be
trusted to touch directly.

This is the part of the stack we would most want another operator to copy,
more than any individual script. If you take one idea from this repository,
take this one.

## The problem

You want a web interface: an admin panel, or a player-facing portal. That
interface needs game data, and some of it needs to make changes.

The naive design gives the web application database credentials. It works
immediately, and it means any bug in your web tier, any dependency
compromise, any injection flaw, is now a direct write path into a live game
database with players in it. There is no second line.

## The shape

```
browser
  |  HTTPS
Caddy
  |  reverse proxy
admin-backend            <- serves UI and API. SQLite only. No game DB access.
  |  HTTP + X-API-Key
relay                    <- the only component that can reach the game host
  |  SSH, forced command
dispatcher               <- fixed allowlist of actions. Runs on the game host
  |  exec
action script            <- one script per action, each doing one thing
  |
dq.sh -> kubectl exec -> psql
```

Every hop narrows what is possible.

## What each layer is for

**admin-backend** holds its own state in SQLite: accounts, links, sessions,
audit. It never connects to the game database, for reads or writes. If it is
compromised, the attacker has your web app and none of your game.

**relay** exists because the game host should not accept connections from the
internet, and because the web tier should not hold an SSH key to it. It runs
somewhere with a stable egress address, authenticates callers with an API key,
and is the only thing that speaks to the game host.

**dispatcher** is the important one. It is installed as an SSH **forced
command**: the key that reaches the game host cannot run arbitrary commands,
because sshd will only ever run this one script, whatever the client asks for.
The script reads an action name from its argument, looks it up in a fixed
allowlist, and executes the matching handler. Anything not on the list is
refused and logged.

It also checks the source address, so a stolen key from an unexpected place
gets nothing.

**action scripts** each do one job. Argument validation lives here, close to
the operation. Adding a capability means writing a script and adding one
allowlist entry, which is a reviewable act rather than a widening.

## Why this is worth the extra hops

- **No credential travels.** The database password is read from the pod's own
  environment at the moment of use, on the host, by `dq.sh`. It is never in a
  config file, never in an environment variable on the web host, never in the
  relay.
- **The dangerous verbs do not exist in the reachable surface.** The web tier
  cannot express "drop this table", because there is no action for it. Its
  vocabulary is the allowlist, not SQL.
- **One audit choke point.** Every state change passes through the dispatcher,
  so logging there logs everything.
- **Compromise is bounded.** Web tier compromised: attacker gets a fixed menu.
  Relay compromised: same menu, from a different place. To get arbitrary
  database access, an attacker needs the game host itself, and at that point
  the control plane is not what failed.

## Costs, honestly

- More moving parts, and a request crosses several of them.
- Every new capability is a script plus an allowlist entry, deliberately.
- Debugging spans hosts. Log at each hop, with a correlation id, or you will
  spend evenings guessing.
- Read latency is real. We cache aggressively on the web side and mirror
  slow-changing data locally, so player-facing pages do not pay the full
  round trip.

## Single host

Our split exists because the game host is a remote dedicated box and the web
tier needed a stable egress address. If you run everything on one machine, the
relay and dispatcher stop being a network boundary.

Keep them anyway. The value is the **allowlist**, not the hop: a web
application that can only express a fixed set of operations is worth having
even when both processes are on the same host. Collapse the transport, keep
the vocabulary.

## What is in this repository right now

| | |
|---|---|
| `relay/` | The Dune-only relay, 106 routes. **Reference implementation** |
| `scripts/dune-relay-dispatch.sh` | The forced-command dispatcher. **Reference implementation** |
| `dune-telemetry/` | Standalone. Reads the game database directly, read-only. Usable today |
| `scripts/dq.sh` | The database access wrapper everything else uses. Usable today |

**Read this before trying to run the relay or dispatcher.** They route to
around 54 individual action scripts that have not been published yet: those
land with the player portal, since that is what most of them serve. What is
here now is the shape, the auth model, the forced-command setup, and the
allowlist pattern, which is the part worth copying. Treat them as a worked
example to build your own against, not as a drop-in service.

Telemetry has no such dependency and runs on its own.

## Setting up the forced command

On the game host, in the authorized_keys entry for the relay's key:

```
command="/root/dune-relay-dispatch.sh",no-agent-forwarding,no-port-forwarding,no-pty,no-X11-forwarding ssh-ed25519 AAAA... relay@your-web-host
```

The client's requested command arrives in `SSH_ORIGINAL_COMMAND` and is
**input, not instruction**. Parse it, match it against the allowlist, and never
pass it to a shell. That last point is the whole design: the moment you
`eval "$SSH_ORIGINAL_COMMAND"`, you have rebuilt an unrestricted shell with
extra steps.
