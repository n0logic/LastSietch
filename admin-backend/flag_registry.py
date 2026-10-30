"""E1: one registry of every gate on the stack, and its current state.

Read only. Nothing here writes a flag, opens SSH or touches the cluster. The
Systems toggles keep working through their own allowlists in
routers/v2_systems.py (RUNTIME_GATES, HOST_FLAGS); this module only describes.

Six kinds of gate, each resolved by the path that really decides it:

  runtime     feature_flags.enabled() on every request in the <web-host> portal
              process. The state is the SAME call the gate makes, so the board
              cannot disagree with the gate.
  boot        read from the environment once, when lastsietch-admin starts. The state
              is the value the running process holds (the config constant), not
              the environment file, which says only what someone intended.
  writer_env  an environment default inside a game-host writer script. The
              dispatcher shell inherits no profile, so the coded default is the
              value; this process has no read of the deployed copy.
  host_file   a file under /etc/lastsietch. <web-host> files are stat'ed here, read
              only. <game-host> files come from the relay's existing flag-list read
              when it covers them, and are otherwise "unknown (not probed)".
  timer       a systemd timer. <web-host> timers are read with systemctl show.
              "active" is not proof a timer will fire (AGENTS.md section 1), so a
              timer with no next elapse reads "dormant". <game-host> timers are not
              probed.
  planned     a gate a plan has named that no code reads yet.

A gate whose state cannot be read is "unknown", never "off": we could not look
is not the same answer as it is off.
"""
import os
import subprocess

import feature_flags

WEB_HOST = "<web-host>"
GAME_HOST = "<game-host>"

NOT_PROBED = "not probed: no read-only status path from the web process"

# Fields: name, kind, host, where, scope, default, restart, owner, feature.
# runtime adds gate_default (the literal the gate itself passes); boot adds const
# (the config.py constant) when there is one; host_file adds path, on_when
# ("present" for an enable file, "absent" for a kill file) and relay (the name in
# the relay's flag-list) when the relay reports it.
ENTRIES = (
    # --- runtime: portal process, request time ----------------------------- #
    {"name": "LASTSIETCH_STORAGE_PAWN_MOVE_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "portal: storage", "scope": "portal, every player",
     "feature": "moving items into and out of the bank and the backpack"},
    {"name": "LASTSIETCH_AUGMENT_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "portal: augments", "scope": "portal, every player",
     "feature": "augment reroll and swap"},
    {"name": "LASTSIETCH_AUGMENT_TARGETED_UPGRADE_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "portal: augments", "scope": "portal, every player",
     "feature": "the transplant swap that keeps the consumed augment's rolls"},
    {"name": "LASTSIETCH_ITEM_TRANSFER_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "portal: storage", "scope": "portal, every player",
     "feature": "player to player item transfer (the <game-host> file item-transfer-enabled must also be on)"},
    {"name": "LASTSIETCH_KARUM_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "portal: economy", "scope": "portal, every player",
     "feature": "the Karum: listing, buying and every open order"},
    {"name": "LASTSIETCH_KARUM_WTB_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "portal: economy", "scope": "portal, every player",
     "feature": "Karum wanted orders"},
    {"name": "LASTSIETCH_BLUEPRINT_PUBLISH_ENABLED", "kind": "runtime", "gate_default": "1",
     "owner": "portal: Solido", "scope": "portal, every player",
     "feature": "publishing a blueprint to the Solido market"},
    {"name": "LASTSIETCH_BLUEPRINT_IMPORT_ENABLED", "kind": "runtime", "gate_default": "1",
     "owner": "portal: Solido", "scope": "portal, every player",
     "feature": "importing a blueprint from the Solido market"},
    {"name": "LASTSIETCH_MARKET_SELL_BACKPACK_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "portal: economy", "scope": "portal, every player",
     "feature": "listing on the Exchange from the backpack (the <game-host> file is the authority)"},
    {"name": "LASTSIETCH_REFINERY_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "portal: economy", "scope": "portal, every player",
     "feature": "the Ingot Refinery (the <game-host> file is the authority)"},
    {"name": "LASTSIETCH_CHAT_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "portal: community", "scope": "portal, every player",
     "feature": "portal chat and its moderation queue (retention keeps running)"},
    {"name": "LASTSIETCH_WEEKEND_REMIND_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "events", "scope": "portal, opted-in players",
     "feature": "the weekend reminder opt-in and its inbox notice an hour before each weekend event"},
    {"name": "LASTSIETCH_REPORTS_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "portal: community", "scope": "portal, every player",
     "feature": "player reports"},
    {"name": "LASTSIETCH_AMTAL_LEDGER_ENABLED", "kind": "runtime", "gate_default": False,
     "owner": "Amtal", "scope": "portal, every player",
     "feature": "the Amtal ledger page"},
    {"name": "LASTSIETCH_AMTAL_PVP_ENABLED", "kind": "runtime", "gate_default": False,
     "owner": "Amtal", "scope": "portal, every player",
     "feature": "the Amtal PvP board"},
    {"name": "LASTSIETCH_SIETCH_NIGHTS_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "events: Seven Nights", "scope": "portal and the <web-host> tick",
     "feature": "Seven Nights earning (the tick also needs /etc/lastsietch/sietch-nights-live)"},
    {"name": "LASTSIETCH_SIETCH_SHOP_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "events: Seven Nights", "scope": "portal, every player",
     "feature": "the Seven Nights shop (the <game-host> writer also needs sietch-shop-enabled)"},
    {"name": "LASTSIETCH_COSMETIC_TITLES_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "portal: community", "scope": "portal, every player",
     "feature": "cosmetic titles"},
    {"name": "LASTSIETCH_PORTAL_SHELL_NEXT_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "portal: shell", "scope": "portal, every player",
     "feature": "the redesigned portal Home (owner preview: ?shell=next)"},
    {"name": "LASTSIETCH_ADMIN_SHELL_NEXT_ENABLED", "kind": "runtime", "gate_default": "0",
     "owner": "admin: shell", "scope": "admin panel, every admin",
     "feature": "the admin rail shell, banners and accent pass (owner preview: ?shell=next)"},
    {"name": "LASTSIETCH_STORAGE_MOVE_ENABLED", "kind": "runtime", "gate_default": "0",
     "forced_off": True,
     "owner": "portal: storage", "scope": "portal, every player",
     "feature": "container storage moves. /portal/features forces this closed: no native backend exists"},

    # --- boot: portal process, read once at start --------------------------- #
    {"name": "LASTSIETCH_REPAIR_ALL_ENABLED", "kind": "boot", "const": "REPAIR_ALL_ENABLED",
     "default": "on", "owner": "portal: repair", "scope": "portal, every player",
     "feature": "the once-a-day Repair and Refurbish Everything tier"},
    {"name": "LASTSIETCH_REPAIR_VEHICLE_ENABLED", "kind": "boot", "const": "REPAIR_VEHICLE_ENABLED",
     "default": "on", "owner": "portal: repair", "scope": "portal, every player",
     "feature": "the per-vehicle Refurbish Vehicle tier"},
    {"name": "LASTSIETCH_REPAIR_BOX_ENABLED", "kind": "boot", "const": "REPAIR_BOX_ENABLED",
     "default": "on", "owner": "portal: repair", "scope": "portal, every player",
     "feature": "the per-container Repair box tier"},
    {"name": "LASTSIETCH_EXPORT_ENABLED", "kind": "boot", "const": "EXPORT_ENABLED",
     "default": "off", "owner": "portal: account", "scope": "portal, every player",
     "feature": "Download my data"},
    {"name": "LASTSIETCH_MULTIACCOUNT_ENABLED", "kind": "boot", "const": "MULTIACCOUNT_ENABLED",
     "default": "off", "owner": "portal: account", "scope": "portal, every player",
     "feature": "linking and switching between several game accounts"},
    {"name": "LASTSIETCH_LANDSRAAD_CLAIM_ENABLED", "kind": "boot", "const": "LANDSRAAD_CLAIM_ENABLED",
     "default": "off", "owner": "portal: Landsraad", "scope": "portal, every player",
     "feature": "the Landsraad claim door in the portal (the <game-host> file decides who may claim)"},
    {"name": "LASTSIETCH_PORTAL_PROFILES_ENABLED", "kind": "boot", "const": "PORTAL_PROFILES_ENABLED",
     "default": "off", "owner": "portal: account", "scope": "portal, every player",
     "feature": "portal profiles"},
    {"name": "LASTSIETCH_PORTAL_AUTH_ENABLED", "kind": "boot", "const": "PORTAL_AUTH_ENABLED",
     "default": "off", "owner": "portal: account", "scope": "portal, every player",
     "feature": "portal profile sign-in"},
    {"name": "LASTSIETCH_PORTAL_GAME_AUTH_ENABLED", "kind": "boot", "const": "PORTAL_GAME_AUTH_ENABLED",
     "default": "off", "owner": "portal: account", "scope": "portal, every player",
     "feature": "portal sign-in with the game account"},
    {"name": "LASTSIETCH_MARKET_WATCH_ENABLED", "kind": "boot", "const": "MARKET_WATCH_ENABLED",
     "default": "off", "owner": "portal: economy", "scope": "portal, every player",
     "feature": "the market price-alert watcher loop"},
    {"name": "LASTSIETCH_EVENT_DISCORD_ENABLED", "kind": "boot", "const": "EVENT_DISCORD_ENABLED",
     "default": "off", "owner": "events", "scope": "Discord #events",
     "feature": "event sync to Discord (Cielago also needs CIELAGO_EVENTS_SYNC_ENABLED)"},
    {"name": "LASTSIETCH_EVENT_REMINDERS_ENABLED", "kind": "boot", "const": "EVENT_REMINDERS_ENABLED",
     "default": "on", "owner": "events", "scope": "portal, players with a reminder",
     "feature": "the T-1h event reminder loop"},
    {"name": "LASTSIETCH_TRANSFER_RELINK_ENABLED", "kind": "boot", "const": "TRANSFER_RELINK_ENABLED",
     "default": "off", "owner": "portal: account", "scope": "portal, every player",
     "feature": "account relink on transfer"},
    {"name": "LASTSIETCH_BASE_VAULT_ENABLED", "kind": "boot",
     "default": "off", "owner": "admin: bases", "scope": "Base Vault, every player",
     "feature": "the Base Vault (read from the process environment on each call)"},

    # --- writer_env: coded defaults in <game-host> writers --------------------- #
    {"name": "LASTSIETCH_GIFTS_ENABLED", "kind": "writer_env", "host": GAME_HOST,
     "where": "scripts/dune-gift-op.sh coded default", "default": "on",
     "restart": "redeploy the writer", "owner": "portal: economy", "scope": "fleet",
     "feature": "Solari gifting writer"},
    {"name": "LASTSIETCH_REWARD_ENABLED", "kind": "writer_env", "host": GAME_HOST,
     "where": "scripts/dune-reward-op.sh, set to 1 when /etc/lastsietch/reward-enabled exists",
     "default": "off", "restart": "no, follows reward-enabled", "owner": "portal: rewards",
     "scope": "fleet", "feature": "the login-rewards writer (the file is the switch)"},
    {"name": "CIELAGO_EVENTS_SYNC_ENABLED", "kind": "writer_env", "host": WEB_HOST,
     "where": "Cielago bot environment (/opt/cielago/.env), another process",
     "default": "off", "restart": "restart cielago", "owner": "events",
     "scope": "Discord #events", "feature": "Cielago's side of the event sync"},

    # --- host files, <game-host>, reported by the relay flag-list -------------- #
    {"name": "karum-enabled", "kind": "host_file", "host": GAME_HOST, "relay": "karum-enabled",
     "on_when": "present", "owner": "portal: economy", "scope": "fleet",
     "feature": "the Karum writer: every listing, fill and payout"},
    {"name": "karum-wtb-enabled", "kind": "host_file", "host": GAME_HOST, "relay": "karum-wtb-enabled",
     "on_when": "present", "owner": "portal: economy", "scope": "fleet",
     "feature": "the Karum wanted-order writer"},
    {"name": "augment-enabled", "kind": "host_file", "host": GAME_HOST, "relay": "augment-enabled",
     "on_when": "present", "owner": "portal: augments", "scope": "fleet",
     "feature": "the augment reroll and swap writer"},
    {"name": "reward-enabled", "kind": "host_file", "host": GAME_HOST, "relay": "reward-enabled",
     "on_when": "present", "owner": "portal: rewards", "scope": "fleet",
     "feature": "the login-rewards writer"},
    {"name": "servercmd-enabled", "kind": "host_file", "host": GAME_HOST, "relay": "servercmd-enabled",
     "on_when": "present", "owner": "admin: live actions", "scope": "fleet",
     "feature": "this panel's own live actions on the game host"},
    {"name": "market-sell-container-enabled", "kind": "host_file", "host": GAME_HOST,
     "relay": "market-sell-container-enabled", "on_when": "present",
     "owner": "portal: economy", "scope": "fleet",
     "feature": "selling to the Exchange from a container"},
    {"name": "storage-pawn-move-enabled", "kind": "host_file", "host": GAME_HOST,
     "relay": "storage-pawn-move-enabled", "on_when": "present",
     "owner": "portal: storage", "scope": "fleet",
     "feature": "the bank and backpack move writer"},
    {"name": "market-sell-backpack-enabled", "kind": "host_file", "host": GAME_HOST,
     "relay": "market-sell-backpack-enabled", "on_when": "present",
     "owner": "portal: economy", "scope": "fleet",
     "feature": "selling to the Exchange from the backpack"},
    {"name": "item-transfer-enabled", "kind": "host_file", "host": GAME_HOST,
     "relay": "item-transfer-enabled", "on_when": "present",
     "owner": "portal: storage", "scope": "fleet",
     "feature": "the CHOAM bank item transfer writer"},
    {"name": "landsraad-claim-enabled", "kind": "host_file", "host": GAME_HOST,
     "relay": "landsraad-claim-enabled", "on_when": "present",
     "owner": "portal: Landsraad", "scope": "fleet",
     "feature": "the Landsraad claim writer (its contents decide who may claim)"},
    {"name": "refinery-enabled", "kind": "host_file", "host": GAME_HOST, "relay": "refinery-enabled",
     "on_when": "present", "owner": "portal: economy", "scope": "fleet",
     "feature": "the Ingot Refinery writer"},

    # --- host files, <game-host>, no read path from here ------------------------ #
    {"name": "event-effects-enabled (<game-host>)", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/event-effects-enabled", "on_when": "present",
     "owner": "events", "scope": "fleet",
     "feature": "the effect worker: launch refuses and the assert timer idles without it"},
    {"name": "event-effects-restart-enabled", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/event-effects-restart-enabled", "on_when": "present",
     "owner": "events", "scope": "fleet (restarts every game pod)",
     "feature": "restart effects (xp, crafting). Created only after the L19 rehearsal passes"},
    {"name": "sietch-shop-enabled", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/sietch-shop-enabled", "on_when": "present",
     "owner": "events: Seven Nights", "scope": "fleet",
     "feature": "the Seven Nights shop writer in dune-reward-op.sh"},
    {"name": "landsraad-claim-swatches-enabled", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/landsraad-claim-swatches-enabled", "on_when": "present",
     "owner": "portal: Landsraad", "scope": "fleet",
     "feature": "swatch unlocks in the Landsraad claim writer"},
    {"name": "welcome-research-sweep-enabled", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/welcome-research-sweep-enabled", "on_when": "present",
     "owner": "portal: welcome pack", "scope": "fleet",
     "feature": "the welcome-pack research sweep"},
    {"name": "amtal-arrival-live", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/amtal-arrival-live", "on_when": "present",
     "owner": "Amtal", "scope": "Amtal (Survival_1 dim 2)",
     "feature": "the Amtal safe-arrival watcher acting rather than dry running"},
    {"name": "amtal-arrival-disabled", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/amtal-arrival-disabled", "on_when": "absent",
     "owner": "Amtal", "scope": "Amtal (Survival_1 dim 2)",
     "feature": "kill switch for the Amtal safe-arrival watcher"},
    {"name": "amtal-pvp-flag-disabled", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/amtal-pvp-flag-disabled", "on_when": "absent",
     "owner": "Amtal", "scope": "Amtal (Survival_1 dim 2)",
     "feature": "kill switch for the Amtal PvP flag watcher"},
    {"name": "amtal-streak-payouts-live", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/amtal-streak-payouts-live", "on_when": "present",
     "owner": "Amtal", "scope": "Amtal",
     "feature": "kill-streak bounty payouts acting rather than dry running"},
    {"name": "amtal-streak-payouts-disabled", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/amtal-streak-payouts-disabled", "on_when": "absent",
     "owner": "Amtal", "scope": "Amtal",
     "feature": "kill switch for kill-streak bounty payouts"},
    {"name": "kulon-close-notice-live", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/kulon-close-notice-live", "on_when": "present",
     "owner": "ops", "scope": "fleet broadcast",
     "feature": "the Kulon close notice broadcasting rather than dry running"},
    {"name": "kulon-close-notice-disabled", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/kulon-close-notice-disabled", "on_when": "absent",
     "owner": "ops", "scope": "fleet broadcast",
     "feature": "kill switch for the Kulon close notice"},
    {"name": "base-vault-admin.enabled (<game-host>)", "kind": "host_file", "host": GAME_HOST,
     "path": "/etc/lastsietch/base-vault-admin.enabled", "on_when": "present",
     "owner": "admin: bases", "scope": "Base Vault",
     "feature": "the Base Vault admin helper (set on both hosts)"},

    # --- host files, <web-host>, stat'ed here --------------------------------- #
    {"name": "event-effects-enabled (<web-host>)", "kind": "host_file", "host": WEB_HOST,
     "path": "/etc/lastsietch/event-effects-enabled", "on_when": "present",
     "owner": "events", "scope": "fleet",
     "feature": "the weekend effects runner: published events go missed without it"},
    {"name": "event-effects-notices-enabled", "kind": "host_file", "host": WEB_HOST,
     "path": "/etc/lastsietch/event-effects-notices-enabled", "on_when": "present",
     "owner": "events", "scope": "in-game broadcast and #announcements",
     "feature": "start notices for live effects (the effect still runs without it)"},
    {"name": "sietch-nights-live", "kind": "host_file", "host": WEB_HOST,
     "path": "/etc/lastsietch/sietch-nights-live", "on_when": "present",
     "owner": "events: Seven Nights", "scope": "every linked player",
     "feature": "the Seven Nights tick crediting (also needs LASTSIETCH_SIETCH_NIGHTS_ENABLED)"},
    {"name": "social-publisher-disabled", "kind": "host_file", "host": WEB_HOST,
     "path": "/etc/lastsietch/social-publisher-disabled", "on_when": "absent",
     "owner": "community", "scope": "public social accounts",
     "feature": "kill switch for the social publisher"},
    {"name": "amtal-title-roles-disabled", "kind": "host_file", "host": WEB_HOST,
     "path": "/etc/lastsietch/amtal-title-roles-disabled", "on_when": "absent",
     "owner": "Amtal", "scope": "Discord roles",
     "feature": "kill switch for mirroring Amtal titles onto Discord roles"},
    {"name": "portal-host-enabled", "kind": "host_file", "host": WEB_HOST,
     "path": "/etc/lastsietch/portal-host-enabled", "on_when": "present",
     "owner": "portal: host", "scope": "every portal visitor",
     "feature": "old portal addresses redirecting to the portal host (Caddy reads it per request)"},
    {"name": "base-vault-admin.enabled (<web-host>)", "kind": "host_file", "host": WEB_HOST,
     "path": "/etc/lastsietch/base-vault-admin.enabled", "on_when": "present",
     "owner": "admin: bases", "scope": "Base Vault",
     "feature": "the Base Vault admin helper (set on both hosts)"},
    {"name": "games-offline", "kind": "host_file", "host": WEB_HOST,
     "path": "/etc/lastsietch/games-offline", "on_when": "present",
     "owner": "ops", "scope": "the website game list",
     "feature": "games listed as dark (a list file: present means some game is marked offline)"},

    # --- timers, <web-host> ---------------------------------------------------- #
    {"name": "lastsietch-dune-hotfix-watch.timer", "kind": "timer", "host": WEB_HOST,
     "owner": "ops: updates", "scope": "notify only",
     "feature": "polls Steam for a new Funcom server build every 10 min"},
    {"name": "lastsietch-return-pack-sweep.timer", "kind": "timer", "host": WEB_HOST,
     "owner": "portal: welcome pack", "scope": "returning players",
     "feature": "returning-player pack sweep"},
    {"name": "lastsietch-return-pack-watch.timer", "kind": "timer", "host": WEB_HOST,
     "owner": "portal: welcome pack", "scope": "returning players",
     "feature": "returning-player pack watch"},
    {"name": "lastsietch-wb-returning-player-sweep.timer", "kind": "timer", "host": WEB_HOST,
     "owner": "portal: welcome pack", "scope": "returning players",
     "feature": "welcome-back returning-player sweep"},
    {"name": "lastsietch-landsraad-weekly-guard.timer", "kind": "timer", "host": WEB_HOST,
     "owner": "portal: Landsraad", "scope": "fleet",
     "feature": "weekly Landsraad guard"},
    {"name": "lastsietch-dd-marker-refresh.timer", "kind": "timer", "host": WEB_HOST,
     "owner": "portal: map", "scope": "Deep Desert map",
     "feature": "Deep Desert marker refresh"},
    {"name": "lastsietch-event-effects.timer", "kind": "timer", "host": WEB_HOST,
     "owner": "events", "scope": "fleet",
     "feature": "the weekend effects runner, every minute"},
    {"name": "lastsietch-sietch-nights.timer", "kind": "timer", "host": WEB_HOST,
     "owner": "events: Seven Nights", "scope": "every linked player",
     "feature": "the Seven Nights tick"},
    {"name": "lastsietch-sietch-deliver.timer", "kind": "timer", "host": WEB_HOST,
     "owner": "events: Seven Nights", "scope": "every linked player",
     "feature": "Seven Nights deliveries"},
    {"name": "social-pub.timer", "kind": "timer", "host": WEB_HOST,
     "owner": "community", "scope": "public social accounts",
     "feature": "the social publisher, every 5 min"},

    # --- timers, <game-host> ------------------------------------------------------ #
    {"name": "lastsietch-event-effect-assert.timer", "kind": "timer", "host": GAME_HOST,
     "owner": "events", "scope": "fleet",
     "feature": "re-asserts open live effect windows every 5 min; dead-man end"},
    {"name": "lastsietch-bg-watchdog.timer", "kind": "timer", "host": GAME_HOST,
     "owner": "ops", "scope": "fleet",
     "feature": "recovers a BattleGroup left at spec.stop=true"},
    {"name": "lastsietch-welcome-pack-watcher.timer", "kind": "timer", "host": GAME_HOST,
     "owner": "portal: welcome pack", "scope": "new players",
     "feature": "welcome-pack grants, every 60 s"},
    {"name": "lastsietch-amtal-arrival.timer", "kind": "timer", "host": GAME_HOST,
     "owner": "Amtal", "scope": "Amtal (Survival_1 dim 2)",
     "feature": "the Amtal safe-arrival watcher, every 3 s"},
    {"name": "lastsietch-amtal-streak-payouts.timer", "kind": "timer", "host": GAME_HOST,
     "owner": "Amtal", "scope": "Amtal",
     "feature": "kill-streak bounty payouts, every 60 s"},
    {"name": "lastsietch-kulon-close-notice.timer", "kind": "timer", "host": GAME_HOST,
     "owner": "ops", "scope": "fleet broadcast",
     "feature": "the Kulon close notice"},

    # --- planned: named by the portal plan (S2), no code reads them yet -------- #
    {"name": "LASTSIETCH_PORTAL_SELF_RELINK_ENABLED", "kind": "planned",
     "owner": "portal: account", "scope": "portal, every player",
     "feature": "portal self-relink (portal plan A2)"},
    {"name": "LASTSIETCH_PORTAL_VAULT_LINE_ENABLED", "kind": "planned",
     "owner": "portal: bases", "scope": "portal, every player",
     "feature": "the portal Base Vault line"},
    {"name": "LASTSIETCH_PORTAL_REQUESTS_ENABLED", "kind": "planned",
     "owner": "admin: requests", "scope": "portal, every player",
     "feature": "player requests to staff (admin F7)"},
    {"name": "LASTSIETCH_AMTAL_WEEKLY_ENABLED", "kind": "planned",
     "owner": "Amtal", "scope": "portal, every player",
     "feature": "the Amtal weekly"},
    {"name": "LASTSIETCH_PORTAL_POLLS_ENABLED", "kind": "planned",
     "owner": "portal: community", "scope": "portal, every player",
     "feature": "portal polls"},
    {"name": "LASTSIETCH_POP_BANDS_ENABLED", "kind": "planned",
     "owner": "portal: community", "scope": "portal, every player",
     "feature": "population bands"},
)

_KIND_DEFAULTS = {
    "runtime": {"host": WEB_HOST, "where": "portal process, feature_flags (override file, then env, then coded default)",
                "restart": "no, next request"},
    "boot": {"host": WEB_HOST, "where": "portal process environment, read once at start",
             "restart": "restart lastsietch-admin"},
    "writer_env": {"restart": "redeploy"},
    "host_file": {"restart": "no, read on every run"},
    "timer": {"where": "systemd", "default": "enabled", "restart": "no"},
    "planned": {"host": WEB_HOST, "where": "no code reads it yet", "default": "off",
                "restart": "n/a"},
}


def entries() -> list:
    """Every entry with its kind's defaults filled in. A copy: callers can add
    state to the rows without touching the table."""
    out = []
    for entry in ENTRIES:
        row = dict(_KIND_DEFAULTS.get(entry["kind"], {}))
        row.update(entry)
        if row["kind"] == "runtime":
            coded = row["gate_default"]
            on = coded if isinstance(coded, bool) else str(coded).strip() == "1"
            row.setdefault("default", _word(on))
        if row["kind"] == "host_file":
            row.setdefault("path", "/etc/lastsietch/" + row["name"])
            row.setdefault("where", row["path"])
            row.setdefault("default", "absent")
        out.append(row)
    return out


def _word(value: bool) -> str:
    return "on" if value else "off"


def _runtime_state(row: dict) -> dict:
    if row.get("forced_off"):
        return {"state": "off", "source": "forced closed in /portal/features"}
    name = row["name"]
    if isinstance(feature_flags.overrides().get(name), bool):
        source = "override file"
    elif feature_flags.env_value(name) is not None:
        source = "process env"
    else:
        source = "coded default"
    return {"state": _word(feature_flags.enabled(name, row["gate_default"])), "source": source}


def _boot_state(row: dict, config_module) -> dict:
    const = row.get("const")
    if const and config_module is not None and hasattr(config_module, const):
        return {"state": _word(bool(getattr(config_module, const))),
                "source": "config." + const + " as booted"}
    env = feature_flags.env_value(row["name"])
    if env is None:
        return {"state": row["default"], "source": "unset in the process env, coded default"}
    return {"state": _word(env), "source": "process env as booted"}


def _host_file_state(row: dict, host_states, stat) -> dict:
    if row["host"] == GAME_HOST:
        relay = row.get("relay")
        if not relay:
            return {"state": "unknown", "source": NOT_PROBED}
        if host_states is None:
            return {"state": "unknown", "source": "the relay flag-list did not answer"}
        value = host_states.get(relay)
        if value not in ("on", "off"):
            return {"state": "unknown", "source": "the relay flag-list did not report it"}
        return {"state": value, "source": "relay flag-list (fresh test -f on <game-host>)"}

    try:
        stat(row["path"])
        present = True
    except FileNotFoundError:
        present = False
    except OSError as exc:
        return {"state": "unknown", "source": "stat failed here: " + type(exc).__name__}
    on = present if row["on_when"] == "present" else not present
    return {"state": _word(on),
            "source": "stat on <web-host>: file " + ("present" if present else "absent")}


def systemctl_timer(unit: str) -> dict:
    proc = subprocess.run(
        ["systemctl", "show", "-p",
         "LoadState,ActiveState,LastTriggerUSec,NextElapseUSecRealtime,NextElapseUSecMonotonic",
         unit],
        capture_output=True, text=True, timeout=5)
    out = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key] = value.strip()
    return out


def timer_state(props: dict) -> dict:
    """systemctl show properties -> state. A timer that is active but has no next
    elapse on either clock is armed-looking and will never fire (the 09-17 hold
    left 12 of 23 timers that way): that is "dormant", not "on"."""
    load = props.get("LoadState", "")
    active = props.get("ActiveState", "")
    if not load:
        return {"state": "unknown", "source": "systemctl gave no answer"}
    if load == "not-found":
        return {"state": "unknown", "source": "not installed on this host"}
    last = props.get("LastTriggerUSec") or ""
    last_note = "last fired " + last if last and last != "n/a" else "never fired since boot"
    if active != "active":
        return {"state": "off", "source": "timer " + (active or "unknown") + ", " + last_note}
    realtime = props.get("NextElapseUSecRealtime") or ""
    monotonic = props.get("NextElapseUSecMonotonic") or ""
    has_next = (realtime not in ("", "n/a", "0")
                or monotonic not in ("", "infinity", "0", "n/a"))
    if not has_next:
        return {"state": "dormant", "source": "active but no next elapse: it will not fire, " + last_note}
    return {"state": "on", "source": last_note}


def _timer_state(row: dict, systemctl) -> dict:
    if row["host"] == GAME_HOST:
        return {"state": "unknown", "source": NOT_PROBED}
    try:
        return timer_state(systemctl(row["name"]))
    except Exception as exc:  # noqa: BLE001
        return {"state": "unknown", "source": "systemctl failed: " + type(exc).__name__}


def snapshot(config_module=None, host_states=None, systemctl=None, stat=None, skip=()) -> list:
    """Every entry plus {state, source}. `host_states` is the relay flag-list as
    {name: on|off}, or None when the relay did not answer. Names in `skip` are
    dropped before their state is read. `systemctl` and `stat` are injectable
    for tests; the defaults are read only."""
    systemctl = systemctl or systemctl_timer
    stat = stat or os.stat
    rows = []
    for row in entries():
        if row["name"] in skip:
            continue
        kind = row["kind"]
        if kind == "runtime":
            row.update(_runtime_state(row))
        elif kind == "boot":
            row.update(_boot_state(row, config_module))
        elif kind == "host_file":
            row.update(_host_file_state(row, host_states, stat))
        elif kind == "timer":
            row.update(_timer_state(row, systemctl))
        elif kind == "writer_env":
            row.update({"state": "unknown", "source": NOT_PROBED + "; coded default " + row["default"]})
        else:
            row.update({"state": "planned", "source": "no code reads it yet"})
        rows.append(row)
    return rows
