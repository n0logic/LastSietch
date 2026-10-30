# Welcome pack grant

`grant.sh` is the file the welcome-pack watcher executes on the game host (installed as
`/opt/lastsietch-welcome-pack/grant.sh`). It passes `currency_id` as `'HouseCredit'::dune.virtualwallettype`,
the 1.5.3 enum, and keeps database credentials out of the process list. Set `LASTSIETCH_GAME_NAMESPACE` and
`LASTSIETCH_GAME_BATTLEGROUP` for your deployment. Design notes: `docs/11-welcome-package-design.md`.
