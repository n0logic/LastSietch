# <game-host> journald retention

Live at `/etc/systemd/journald.conf.d/50-lastsietch-retention.conf` since 2026-10-20 (`systemctl restart systemd-journald`; no game service touched).

Why: `lastsietch-rmq-capture-admin` writes about 128k journal lines an hour, which rotated the default 3.9 GB cap in about 2 days and took the ssh/auth history with it (the journal reached back only to 10/18). The disk has 2.6 TB free, so the cap is now 60 GB or 90 days.
