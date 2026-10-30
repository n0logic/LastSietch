# 25 - Host topology: what runs where

The question we get most after "how do I install it": which piece runs on which machine, and what talks to what.
This doc answers both, for the split we run and for a single box.

## The request chain

```text
browser
  |  HTTPS
Caddy                     TLS, access log, security headers, X-Forwarded-For overwrite
  |  reverse proxy, loopback
admin-backend             portal + admin panel. Own state in SQLite. No game database access
  |  HTTP + X-API-Key, loopback
relay                     the only component that can reach the game host
  |  SSH, forced command
dispatcher                fixed allowlist of actions, on the game host
  |  exec
action script             one job each, arguments validated here
  |
dq.sh -> kubectl exec -> psql
```

Every hop narrows what is possible. [15-control-plane-architecture.md](15-control-plane-architecture.md) explains
why each layer exists. Reads that must be fresh (positions, chat, server status) come from the telemetry service on
the game host, read-only, outside this chain.

## Our split

| Host | Runs | Why there |
|---|---|---|
| **Game host** (dedicated box) | k3s and Funcom's battlegroup; the dispatcher, action scripts and `dq.sh`; telemetry collector and API; battlegroup watchdog and pod watcher; update orchestrator and hotfix watcher; database backups; the PvP map jobs ([22](22-amtal-pvp-sietch.md)); the events effect worker ([23](23-events-runner.md)); Base Vault capture ([24](24-base-vault.md)) | Anything that touches the game database or the cluster has to run here, and nothing here should accept connections from the internet |
| **Web host** (small VPS) | Caddy; admin-backend and the built portal; the relay; the Discord bot; the events runner; the Base Vault archive store and lab | A stable public address, no game credentials, and a compromise that ends at the relay's fixed menu |

The game host accepts SSH from the web host's address only, and that key can run nothing but the dispatcher. The
relay and admin-backend listen on loopback; only Caddy faces the internet.

**Never expose** Postgres, FileBrowser or the battlegroup director's admin UI publicly. Reach them over an SSH
tunnel ([10](10-bgd-admin-ui-access.md)).

## Single box

Everything runs on one machine as well. Our split exists because the game host is a remote dedicated box and the web
tier needed a stable egress address.

- **Keep the relay and the forced-command hop**, even to `localhost`. The value is the allowlist, not the network
  boundary. Collapse the transport, keep the vocabulary.
- **Ports.** admin-backend and the telemetry API both default to `8078`. Move one
  ([20](20-player-portal.md#single-box-mode)).
- **Memory.** The game pods take most of the box. Budget the web tier's few hundred MB before the per-map limits
  ([06](06-memory-tuning.md)).
- **Telemetry.** With the collector on the same host, admin-backend can read its SQLite directly instead of through
  the relay.

## Caddy

A minimal site block for the portal:

```caddy
(access_log) {
	log {
		output file /var/log/caddy/access.log {
			roll_size 100MiB
			roll_keep 20
		}
		format json
	}
}

portal.<your-domain> {
	import access_log
	reverse_proxy 127.0.0.1:8078 {
		header_up X-Forwarded-For {remote_host}
	}
	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "strict-origin-when-cross-origin"
	}
}
```

Two things in it are deliberate:

- **`header_up X-Forwarded-For {remote_host}`.** Caddy appends to whatever `X-Forwarded-For` the client sent, so
  the leftmost entry is client-chosen. Every rate-limit bucket and audit address behind the proxy keys on that
  value. Overwrite it with the peer Caddy actually saw. admin-backend also reads the rightmost hop, so either half
  alone is correct.
- **An access log on every site,** in one rolled JSON file, so any host can answer "who hit what". Caddy redacts
  cookies and authorization headers by default.

Change Caddy by editing a copy, `caddy validate`, then **reload**. Never restart it: a reload keeps in-flight
requests and TLS sessions, a restart drops them.

## Ops on each host

| Concern | Game host | Web host |
|---|---|---|
| Logs | Cap journald, or the game's log volume fills the disk | journald for the services, Caddy's own log |
| Backups | Database dump with integrity gates ([12](12-operations.md)) | `admin.db` and the other SQLite files with `VACUUM INTO`, never a file copy ([20](20-player-portal.md#operating)); the Base Vault store |
| Watchers | Battlegroup watchdog (a stopped battlegroup never recovers itself), pod watcher, disk and IO watch | Timers for the runner and the bot jobs |
| Kill switches | Flag files under `/etc/lastsietch/`, checked by the action scripts | Feature flags in the admin Systems panel; both sides must be on for a write to land |

## Related

- [15-control-plane-architecture.md](15-control-plane-architecture.md): why the chain is shaped this way
- [20-player-portal.md](20-player-portal.md): installing the web tier
- [01-install.md](01-install.md): installing the game host
