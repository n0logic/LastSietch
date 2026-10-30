# Amtal PvP rules flag re-apply

Amtal (Hagga partition 33) runs security zones ON with the PvP override table
(`-ini:game:[/Script/DuneSandbox.DataTablesSubsystem]:m_SecurityZonesDataTable=.../DT_SecurityZones_PvPOverride`)
plus the runtime cvar `SecurityZones.UsePvPOverrideTable 1`. The cvar is what makes players' clients use the
PvP table too (open desert PvP, abilities can target players). It has to be sent at runtime and is lost on every
Amtal restart. Owner decision 2026-09-29: keep this setup; safe zones on
Amtal are Riftwatch and Helius Gate (tradeposts are PvP; their banner flip-flops because of the missing
TownNoClimb row).

`amtal-pvp-flag.sh` runs every minute (`lastsietch-amtal-pvp-flag.timer`). When Amtal's server_id changes and the new
container has logged "Server farm is READY", it sends the set to that server_id only, reads it back, and posts
to the bot-logs channel. Kill switch: `touch /etc/lastsietch/amtal-pvp-flag-disabled`.

Install (<game-host>, root):
```
install -D -m755 amtal-pvp-flag.sh /opt/lastsietch-amtal-pvp-flag/amtal-pvp-flag.sh
install -m644 systemd/lastsietch-amtal-pvp-flag.{service,timer} /etc/systemd/system/
mkdir -p /var/lib/lastsietch-amtal-pvp-flag   # seed "applied" with the current server_id if the flag is already set
systemctl daemon-reload && systemctl enable --now lastsietch-amtal-pvp-flag.timer
```
