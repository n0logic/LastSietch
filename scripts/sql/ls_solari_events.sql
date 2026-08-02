-- Permanent archive of Solari balance changes.
--
-- 🔴 WHY THIS EXISTS. Funcom already logs every Solari change: their
-- dune.adjust_player_virtual_currency_balance() calls log_event_solaris(), which writes a
-- category='solaris' row into dune.event_log carrying the delta, the resulting balance and
-- the acting fls_id. A complete currency audit trail exists BY DESIGN.
--
-- And it is useless for anything older than about nine days, because event_log rotates.
-- Measured 2026-07-27: the whole table spanned exactly 9 days (07-18 -> 07-27).
--
-- That cost us a real investigation. Three accounts hold
-- ~1e17 Solari between them, which is 99.99998% of the money on the server. Their last
-- activity was 2026-07-06, TWELVE DAYS before the surviving log window opens. The rows that
-- would have shown exactly where that money came from existed, and had aged out by the time
-- anybody looked. See.
--
-- This table is the fix: copy the rows out before they rotate, and keep them. Cheap
-- (~460 rows/day, ~170k/year) and the next currency anomaly becomes answerable instead of
-- archaeological.
--
-- READ-ONLY against every Funcom table. The snapshot job only ever SELECTs from event_log and
-- INSERTs here.
--
-- ══════════════════════════════════════════════════════════════════════════════════════════
-- 🔴 TWO THINGS YOU MUST KNOW BEFORE QUERYING THIS TABLE (both learned 2026-07-27, LT-2)
-- ══════════════════════════════════════════════════════════════════════════════════════════
--
-- 1. **`solaris_delta` IS NOT SIGN-RELIABLE. The three logging procs disagree.**
--
--      adjust_player_virtual_currency_balance    delta is SIGNED   (-2415 means -2415)
--      dune_exchange_modify_user_solari_balance  delta is UNSIGNED MAGNITUDE
--      dune_exchange_retrieve_solaris_from_item  delta is positive (always an inflow)
--
--    So an exchange PURCHASE of 230,000 logs `solaris_delta = +230000` while the balance FALLS
--    by 230,000. Proven live: event_log 18 -> 19, bal 7600921396 -> 7600691396, delta logged
--    +230000. This is the same sign-naivety already recorded against that proc as a latent
--    vector; it extends to its logging.
--
--    ⚠️ **Any query that reasons about DIRECTION from `solaris_delta` alone is wrong.** Derive
--    the sign from the balance instead. The canonical pattern:
--
--      SELECT fls_id, event_log_id,
--             solaris_balance - lag(solaris_balance) OVER w AS signed_delta
--        FROM dune.ls_solari_events
--      WINDOW w AS (PARTITION BY fls_id ORDER BY event_log_id);
--
--    ⚠️ And partition by fls_id ONLY when the account has a single wallet. `fls_id` is the
--    ACCOUNT; the wallet is per CHARACTER (`player_virtual_currency_balances.player_controller_id`)
--    and `meta` carries no controller id, so a multi-character account interleaves two series
--    that cannot be separated from the log alone. 6 of 40 logged accounts were multi-wallet.
--
-- 2. **`character_transfer_import` WRITES THE WALLET AND LOGS NOTHING.** So this archive is not
--    the complete trail an earlier version of this comment claimed. Of the procs that write
--    `player_virtual_currency_balances`, three log and `character_transfer_import` does not.
--    🔴 That is precisely the mechanism by which two of the three quadrillion-Solari accounts got
--    their money (`account_removal_log` reason `incoming char transfer`), so **the exact event
--    this archive was built to catch is the one event it cannot see.** A transfer-in of an absurd
--    balance would still arrive silently. Detecting that needs balance-level snapshotting or an
--    `account_removal_log` watch, not this table.
--    (`get_player_virtual_currency_balances` also writes -- it creates a missing row -- which is
--    another write-capable Funcom `get_*`; see the search_path/write-capable-getter rule.)
--
-- ✅ What the archive IS good for, and it is a lot: 4,090 of 4,093 consecutive logged balance
-- transitions reconcile exactly (`|Δbalance| = |logged delta|`), and the 3 that do not are
-- multi-wallet interleaving. So **no unlogged writer touches a wallet during play** -- which is
-- what closed LT-2 (the currency leg is online-safe: proc-mediated, delta-based, no client-held
-- primary key, and empirically never clobbered).
--
-- OWNER MUST BE dune. Funcom's pre-update pg_dump aborts if any object in the dune schema is
-- owned by another role, mid-update and inside a window. See
-- scripts/tests/test_schema_ownership.py, which enforces this repo-wide.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS dune.ls_solari_events (
    id              bigserial PRIMARY KEY,

    -- The source row's own id in dune.event_log. UNIQUE, and it is what makes the snapshot
    -- job idempotent: a re-run inserts nothing rather than duplicating history.
    event_log_id    bigint      NOT NULL UNIQUE,

    event_time      timestamptz NOT NULL,
    partition_id    bigint,
    function_name   text,                  -- which proc moved the money
    message         text,                  -- 'update_solaris'

    -- Lifted out of the meta jsonb so the common queries need no json operators. fls_id is
    -- the account key (dune.accounts."user"), which is also what cheater_tracking uses.
    fls_id          text,
    solaris_delta   bigint,
    solaris_balance bigint,

    meta            jsonb,                 -- kept whole; the columns above are a convenience
    captured_at     timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE dune.ls_solari_events OWNER TO dune;

-- The three questions this table gets asked: what happened to this player, what happened
-- around this time, and what were the biggest moves.
CREATE INDEX IF NOT EXISTS idx_hse_fls_time
    ON dune.ls_solari_events (fls_id, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_hse_time
    ON dune.ls_solari_events (event_time DESC);
-- Partial: only the moves worth investigating. A million routine 250-Solari trades are not
-- what anybody scans for, and this keeps the anomaly hunt cheap.
CREATE INDEX IF NOT EXISTS idx_hse_big_moves
    ON dune.ls_solari_events (abs(solaris_delta) DESC, event_time DESC)
    WHERE abs(solaris_delta) >= 1000000;
