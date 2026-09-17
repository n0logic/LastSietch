<script>
  // Send Solari: three ways to NAME a recipient. Two of them resolve to a
  // character name string; the third resolves to an identity CODE. Either way
  // the value hands off to GiftDialog -- the same POST /portal/gifts/send path
  // the Guilds page already uses -- and the server does the resolving. No
  // account id or controller id ever crosses the wire from here; that is
  // deliberate (see docs/dune-research/v2-portal/SEND-SOLARI-DESIGN-2026-08-26.md
  // section 2) and is why this path has no IDOR surface. Do not "improve" it by
  // sending ids.
  //
  // The code is a payload, never a URL. It goes into the lookup call and into
  // the gift BODY, and nowhere else: not a link, not a query string this
  // component builds, not a page address. The confirm step is advisory only --
  // the server re-resolves the code on the write, so a code rotated between
  // Look up and Gift comes back code_unknown instead of falling through to the
  // name the sender was shown.
  //
  // The dialog chrome, focus trap, inert background and Escape handling come
  // from the shared Modal primitive, which also fixes this dialog's own trap
  // bug: the hand-rolled selector here omitted input/select/textarea, so the
  // GiftDialog amount field could tab out of the panel. Rows expand an inline
  // GiftDialog in place, same pattern PlayerDirectory uses for MessageComposer.
  import { onMount } from 'svelte';
  import { auth } from '$lib/auth.svelte.js';
  import { api } from '$lib/api.js';
  import { refreshOverview } from '$lib/storage.svelte.js';
  import Modal from '$lib/components/Modal.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import GiftDialog from './guild/GiftDialog.svelte';

  let { onClose } = $props();

  let mode = $state(auth.multiaccountEnabled ? 'accounts' : 'guild'); // 'accounts' | 'guild' | 'code'
  let openFor = $state(null); // character_name whose inline GiftDialog is expanded

  // My accounts: every OTHER game account linked to this Discord (auth.accounts,
  // NOT auth.characters -- that field is every character on the ACTIVE account,
  // exactly one on this server, and was the wrong source an earlier pass used).
  // You cannot send to yourself, so the active account is excluded.
  let alts = $derived(Array.isArray(auth.accounts) ? auth.accounts.filter((a) => !a.active) : []);

  // Guild member: not loaded on Storage today, so fetch it ourselves, lazily,
  // once when the dialog opens.
  let guildStatus = $state('loading'); // 'loading' | 'ready' | 'error'
  let inGuild = $state(false);
  let guildMembers = $state([]); // [{ character_name }] -- own guild, self excluded

  // Send code tab. `resolved` is the CONFIRMED recipient and is the only thing
  // that opens the gift form: no confirmed resolution, no Send. Any code
  // refusal, from the lookup or from the write, clears it again so a stale name
  // can never sit above a live button.
  const CODE_RE = /^[A-Z2-9]{8}$/;
  let code = $state('');
  let looking = $state(false);
  let note = $state(null);     // { tone, text } for the Notice line
  let resolved = $state(null); // { name, dailyRemaining, pairRemaining }

  let codeValid = $derived(CODE_RE.test(code));

  async function loadGuild() {
    guildStatus = 'loading';
    try {
      const r = await api.guilds.data();
      const guilds = Array.isArray(r) ? r : (r?.guilds ?? []);
      const mine = guilds.find((g) => g?.is_mine === true) || null;
      inGuild = !!mine;
      const rows = Array.isArray(mine?.members) ? mine.members : [];
      guildMembers = rows.filter((m) => !m?.is_self && m?.character_name);
      guildStatus = 'ready';
    } catch (e) {
      inGuild = false; guildMembers = []; guildStatus = 'error';
    }
  }
  onMount(loadGuild);

  function pickMode(m) {
    mode = m;
    openFor = null; // don't carry an expanded row across tabs
  }
  function toggle(name) {
    openFor = openFor === name ? null : name;
  }
  // A gift moves Solari OUT of the sender's own bank via the relay/gift-op path,
  // not storageWithdraw/Deposit, so ChoamBankCard's balance would otherwise sit
  // stale until the next unrelated write. Bring it back in sync.
  function onGiftSent() {
    refreshOverview();
  }

  // Typing a different code retires the confirmation it no longer matches.
  function onCode(e) {
    code = e.target.value.toUpperCase().replace(/[^A-Z2-9]/g, '').slice(0, 8);
    e.target.value = code;
    resolved = null;
    note = null;
  }

  async function onLookup(e) {
    e.preventDefault();
    if (looking) return;
    resolved = null;
    if (!codeValid) {
      note = { tone: 'warn', text: 'A code is eight characters, A to Z and 2 to 9.' };
      return;
    }
    looking = true; note = null;
    try {
      const r = await api.settings.codes.lookup(code);
      if (r?.found) {
        resolved = {
          name: r.display_name,
          // null stays null: an unknown allowance is never printed as zero.
          dailyRemaining: r.daily_remaining ?? null,
          pairRemaining: r.pair_remaining ?? null,
        };
      } else {
        note = { tone: 'warn', text: 'No such code. Check the eight characters and try again.' };
      }
    } catch (err) {
      note = err?.status === 429
        ? { tone: 'error', text: err?.data?.message || 'Too many lookups just now. Wait a moment and try again.' }
        : { tone: 'error', text: err?.data?.message || 'That lookup could not be answered right now.' };
    } finally {
      looking = false;
    }
  }

  // The write re-resolves the code, so a refusal there retires the confirmation
  // the same way a failed lookup does.
  function onCodeRefused(refusal) {
    resolved = null;
    note = { tone: 'error', text: refusal?.message || 'That code could not be used.' };
  }
</script>

<Modal title="Send Solari" size="md" {onClose}>
  <div class="tabs" role="tablist" aria-label="Recipient">
    {#if auth.multiaccountEnabled}
      <button
        role="tab" aria-selected={mode === 'accounts'} class:on={mode === 'accounts'}
        type="button" onclick={() => pickMode('accounts')}
      >My accounts</button>
    {/if}
    <button
      role="tab" aria-selected={mode === 'guild'} class:on={mode === 'guild'}
      type="button" onclick={() => pickMode('guild')}
    >Guild member</button>
    <button
      role="tab" aria-selected={mode === 'code'} class:on={mode === 'code'}
      type="button" onclick={() => pickMode('code')}
    >Send code</button>
  </div>

  {#if mode === 'accounts'}
    {#if alts.length === 0}
      <p class="sealed">You have only one linked account. Link another from the top bar to send Solari between your own accounts.</p>
    {:else}
      <ul class="rows">
        {#each alts as a (a.account_id)}
          <li class="row">
            <div class="line">
              <span class="name">{a.character_name}</span>
              <button class="btn" type="button" onclick={() => toggle(a.character_name)}>
                {openFor === a.character_name ? 'Close' : 'Send'}
              </button>
            </div>
            {#if openFor === a.character_name}
              <div class="expand">
                <GiftDialog recipientCharName={a.character_name} locked={true} onSent={onGiftSent} />
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {:else if mode === 'guild'}
    {#if guildStatus === 'loading'}
      <p class="skeleton">loading</p>
    {:else if guildStatus === 'error'}
      <p class="sealed">Your guild roster could not be read right now. Try again in a moment.</p>
    {:else if !inGuild}
      <p class="sealed">You have not sworn to a guild yet.</p>
    {:else if guildMembers.length === 0}
      <p class="sealed">No other members in your guild yet.</p>
    {:else}
      <ul class="rows">
        {#each guildMembers as m (m.character_name)}
          <li class="row">
            <div class="line">
              <span class="name">{m.character_name}</span>
              <button class="btn" type="button" onclick={() => toggle(m.character_name)}>
                {openFor === m.character_name ? 'Close' : 'Send'}
              </button>
            </div>
            {#if openFor === m.character_name}
              <div class="expand">
                <GiftDialog recipientCharName={m.character_name} locked={true} onSent={onGiftSent} />
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {:else}
    <div class="codetab">
      <p class="explain">A send code is the eight characters another player reads off their own Settings page. It finds them without a name or a spelling. Look one up first: nothing can be sent until the code answers with a player.</p>
      <form class="lookup" onsubmit={onLookup}>
        <label class="fld"><span class="lbl mono">Send code</span>
          <input
            class="mono"
            type="text"
            autocomplete="off"
            autocapitalize="characters"
            spellcheck="false"
            maxlength="8"
            placeholder="ABCD2345"
            value={code}
            oninput={onCode}
            aria-label="Recipient identity code"
          />
        </label>
        <button class="btn" type="submit" disabled={looking || !codeValid}>{looking ? 'Looking' : 'Look up'}</button>
      </form>
      {#if note}<Notice tone={note.tone} text={note.text} />{/if}
      {#if resolved}
        <div class="confirm">
          <p class="who">Sending to {resolved.name}</p>
          {#if resolved.dailyRemaining != null && resolved.pairRemaining != null}
            <p class="allow mono">{resolved.dailyRemaining} of your sends left today, {resolved.pairRemaining} left to this player.</p>
          {:else}
            <p class="allow mono">Your remaining allowance could not be read; the server still enforces it.</p>
          {/if}
        </div>
        <div class="expand">
          <GiftDialog recipientCode={code} locked={true} onSent={onGiftSent} onRefused={onCodeRefused} />
        </div>
      {/if}
    </div>
  {/if}
</Modal>

<style>
  .tabs {
    display: inline-flex; gap: var(--space-1); overflow-x: auto; scrollbar-width: none;
    max-width: 100%; border-bottom: 1px solid var(--edge); padding-bottom: 2px;
  }
  .tabs::-webkit-scrollbar { display: none; }
  .tabs button {
    flex: 0 0 auto; background: transparent; color: var(--text-muted); border: 0;
    border-bottom: 2px solid transparent;
    padding: var(--space-2) var(--space-3); font-size: var(--text-xs); cursor: pointer;
    font-family: var(--font-mono); letter-spacing: .1em; text-transform: uppercase; white-space: nowrap;
    transition: color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
  }
  .tabs button:hover { color: var(--text); }
  .tabs button.on { color: var(--accent-bright); border-bottom-color: var(--accent); font-weight: 700; }

  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .row {
    display: flex; flex-direction: column; gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .line { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); }
  .name { font-size: var(--text-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .btn {
    flex: 0 0 auto;
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out);
  }
  .btn:hover { border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .expand { padding-top: var(--space-2); border-top: 1px solid var(--edge); }

  .codetab { display: flex; flex-direction: column; gap: var(--space-3); }
  .explain { margin: 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.45; }
  .lookup { display: flex; align-items: flex-end; gap: var(--space-2); flex-wrap: wrap; }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); flex: 1 1 10rem; min-width: 0; }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .1em; }
  .fld input {
    font-size: var(--text-base); color: var(--text); letter-spacing: .18em;
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .fld input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .confirm {
    display: flex; flex-direction: column; gap: var(--space-1);
    padding: var(--space-2) var(--space-3);
    background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .who { margin: 0; font-size: var(--text-sm); color: var(--text); }
  .allow { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }

  .sealed { margin: 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.45; }
</style>
