<script>
  // CHOAM bank currency console. Solari balance reads AMBER (it is owned currency,
  // not a live-streaming value, so never Ibad blue). Three writes: withdraw
  // (credit -> coin, capped), deposit a specific amount (coin -> credit), and
  // sweep (bank every owned coin). All three are offline-gated; when the write
  // surface is locked the arrows disable and the gate banner explains why.
  import { storage, withdraw, deposit } from '$lib/storage.svelte.js';
  import { openSendSolari } from '$lib/sendSolari.svelte.js';
  import EliHint from '$lib/components/guild/EliHint.svelte';

  let amount = $state('');
  let busy = $state(false);

  let amountNum = $derived(Math.floor(Number(amount)) || 0);
  let overCap = $derived(amountNum > storage.withdrawCap);
  let locked = $derived(!storage.offlineOk);
  let solari = $derived(storage.bank ? Number(storage.bank.solari) || 0 : 0);

  async function doWithdraw() {
    if (busy || locked) return;
    busy = true;
    const ok = await withdraw(amountNum);
    if (ok) amount = '';
    busy = false;
  }
  async function doDepositAmount() {
    if (busy || locked) return;
    busy = true;
    const ok = await deposit('amount', amountNum);
    if (ok) amount = '';
    busy = false;
  }
  async function doSweep() {
    if (busy || locked) return;
    busy = true;
    await deposit('sweep');
    busy = false;
  }
</script>

<div class="bank">
  <p class="kicker mono">CHOAM Bank</p>
  <EliHint
    text="Your Solari on account at the CHOAM Exchange."
    detail="Withdraw turns credit into a coin stack you carry; deposit banks coins back. You must be logged out of the game to move Solari."
  />

  <div class="balance">
    <img class="crest" src="/admin/static/img/dune-icons/T_UI_IconResourceSolarisCoin_D.png" alt="Solari" />
    <div class="figure">
      <span class="amt mono">{solari.toLocaleString()}</span>
    </div>
  </div>

  <div class="controls" class:locked>
    <label class="fld">
      <span>Amount</span>
      <input
        type="number" min="1" step="1" inputmode="numeric"
        bind:value={amount} placeholder="0"
        disabled={locked}
        aria-label="Solari amount"
        class:err={overCap}
      />
    </label>
    <div class="arrows">
      <button
        class="btn" type="button" onclick={doWithdraw}
        disabled={busy || locked || amountNum <= 0 || overCap}
        title="Withdraw credit into a coin stack (capped {storage.withdrawCap.toLocaleString()})"
      >&rarr; Withdraw</button>
      <button
        class="btn" type="button" onclick={doDepositAmount}
        disabled={busy || locked || amountNum <= 0}
        title="Deposit this many coins back to credit"
      >&larr; Deposit</button>
    </div>
    <button
      class="btn sweep" type="button" onclick={doSweep}
      disabled={busy || locked}
      title="Bank every Solari coin you carry"
    >Deposit all coins</button>
  </div>

  {#if overCap}
    <p class="warn mono" role="alert">Over the {storage.withdrawCap.toLocaleString()} Solari per-transfer cap.</p>
  {/if}

  <!-- Deliberately outside the offline-gated .controls block: a gift posts through
       adjust_player_virtual_currency_balance (DB-authoritative), the same reason
       GiftDialog on /guilds has never needed the offline gate. -->
  <button class="btn send" type="button" onclick={openSendSolari} title="Send Solari to another player">
    Send Solari
  </button>
</div>

<style>
  .bank { display: flex; flex-direction: column; gap: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }

  .balance {
    display: flex; align-items: center; gap: var(--space-3);
    padding: var(--space-3) var(--space-4);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--accent) 8%, var(--metal-0));
    box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .crest { width: 34px; height: 34px; object-fit: contain; opacity: .9; filter: drop-shadow(0 0 4px var(--accent-glow)); }
  .figure { display: flex; align-items: baseline; gap: var(--space-2); }
  /* AMBER, not Ibad: owned balance is not a live-streaming value. */
  .amt { font-size: var(--text-2xl); font-weight: 700; color: var(--accent-text); letter-spacing: .01em; }

  .controls { display: flex; flex-direction: column; gap: var(--space-2); }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; text-transform: uppercase; }
  .fld input {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
    text-transform: none; letter-spacing: normal;
  }
  .fld input.err { border-color: var(--ls-red); }
  .fld input:disabled { opacity: .5; cursor: not-allowed; }
  .fld input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .arrows { display: flex; gap: var(--space-2); }
  .arrows .btn { flex: 1 1 0; }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm);
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge);
    box-shadow: inset 0 1px 0 var(--metal-hi); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), filter var(--motion-fast) var(--ease-out);
  }
  .btn:hover:not(:disabled) { border-color: var(--accent); }
  .btn.sweep { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); box-shadow: 0 0 10px var(--accent-glow); }
  .btn.sweep:hover:not(:disabled) { filter: brightness(1.1); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn.send { width: 100%; }

  .warn { margin: 0; font-size: var(--text-xs); color: var(--ls-red); }
</style>
