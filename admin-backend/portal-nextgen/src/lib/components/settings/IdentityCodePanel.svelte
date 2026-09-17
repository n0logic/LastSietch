<script>
  // The identity code: eight symbols from the 32-symbol alphabet (no 0/1/I/O,
  // so nothing is mistaken when it is read aloud across a comms channel), one
  // per Discord identity, rotatable. This wave ships mint, rotate, lookup and
  // display only; item transfer and Send Solari front it later.
  //
  // The code is NEVER a URL. It is not rendered as a link, the QR carries the
  // eight characters and nothing else, and lookup takes the payload rather than
  // an address: a code read off someone's screen must not be able to send a
  // scanner anywhere.
  //
  // The QR plate is ink on parchment rather than amber on sand. Chrome is amber
  // everywhere else in the portal, but a scanner needs contrast before it needs
  // house colours, so the branding here is the plate and the corner brackets.
  import { onMount } from 'svelte';
  import { api } from '$lib/api.js';
  import { qrPath, QR_MODULES } from '$lib/qr.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import Modal from '$lib/components/Modal.svelte';
  import Notice from '$lib/components/Notice.svelte';

  let status = $state('loading'); // 'loading' | 'error' | 'ready'
  let code = $state('');
  let qr = $state('');
  let rotatedAt = $state(null);
  let note = $state(null); // { tone, text }

  let confirmOpen = $state(false);
  let rotating = $state(false);

  let query = $state('');
  let looking = $state(false);
  let result = $state(null); // { tone, text }

  // The frame brackets sit outside the four-module quiet zone, which stays
  // clear: a mark inside it is what makes a QR fail to read.
  const BRACKETS = [
    'M-5.5 -2.5V-5.5H-2.5',
    `M${QR_MODULES + 2.5} -5.5H${QR_MODULES + 5.5}V-2.5`,
    `M${QR_MODULES + 5.5} ${QR_MODULES + 2.5}V${QR_MODULES + 5.5}H${QR_MODULES + 2.5}`,
    `M-2.5 ${QR_MODULES + 5.5}H-5.5V${QR_MODULES + 2.5}`,
  ].join('');

  function when(t) {
    const d = new Date(t);
    return Number.isNaN(d.getTime()) ? String(t ?? '') : d.toLocaleString();
  }

  // qrPath throws on anything that is not the payload shape, so a malformed
  // code from the server leaves the plate empty rather than half-drawn.
  function setCode(next, at) {
    code = typeof next === 'string' ? next : '';
    rotatedAt = at ?? null;
    try { qr = code ? qrPath(code) : ''; } catch (e) { qr = ''; }
  }

  async function load() {
    status = 'loading';
    try {
      const r = await api.settings.codes.mine();
      if (!r || r.ok === false || !r.code) { status = 'error'; return; }
      setCode(r.code, r.rotated_at);
      status = 'ready';
    } catch (e) {
      status = 'error';
    }
  }

  async function rotate() {
    if (rotating) return;
    rotating = true; note = null;
    try {
      const r = await api.settings.codes.rotate();
      setCode(r?.code, r?.rotated_at);
      note = { tone: 'ok', text: 'New code minted. The old one no longer finds you.' };
    } catch (e) {
      // the cooldown refusal carries its own player copy ("Try again in N minutes")
      note = e?.status === 429
        ? { tone: 'warn', text: e?.data?.message || 'You changed your code recently. Try again in a few minutes.' }
        : { tone: 'error', text: 'The rotation did not go through. Your code is unchanged.' };
    } finally {
      // the confirm closes on every outcome; the note carries the verdict (live QA 9/3:
      // a refusal left the dialog open)
      confirmOpen = false;
      rotating = false;
    }
  }

  function onQuery(e) {
    query = e.target.value.toUpperCase().replace(/[^A-Z2-9]/g, '').slice(0, 8);
    e.target.value = query;
  }

  async function onLookup(e) {
    e.preventDefault();
    if (looking) return;
    if (!/^[A-Z2-9]{8}$/.test(query)) {
      result = { tone: 'warn', text: 'A code is eight characters, A to Z and 2 to 9.' };
      return;
    }
    looking = true; result = null;
    try {
      const r = await api.settings.codes.lookup(query);
      result = r?.found
        ? { tone: 'ok', text: `${r.display_name} carries that code.` }
        : { tone: 'warn', text: 'No such code. Check the eight characters and try again.' };
    } catch (err) {
      result = err?.status === 429
        ? { tone: 'error', text: 'Too many lookups just now. Wait a moment and try again.' }
        : { tone: 'error', text: 'That lookup could not be answered right now.' };
    } finally {
      looking = false;
    }
  }

  onMount(load);
</script>

{#if status === 'ready'}
  <CarvedSlab>
    <p class="kicker mono">Your identity code | the sign you carry</p>

    <div class="plate">
      <div class="who">
        <p class="code mono">{code}</p>
        <p class="explain">Give this to another player and they can find you without a name, a spelling or a Discord handle. It is a payload, not a link: nothing scans through to a page.</p>
        {#if rotatedAt}<p class="rotated mono">Last rotated {when(rotatedAt)}</p>{/if}
        <button class="btn" type="button" onclick={() => (confirmOpen = true)}>Rotate</button>
        {#if note}<Notice tone={note.tone} text={note.text} />{/if}
      </div>

      <div class="qr">
        <svg viewBox="-6 -6 33 33" role="img" aria-label={`Identity code ${code} as a QR code`}>
          <rect class="quiet" x="-4" y="-4" width={QR_MODULES + 8} height={QR_MODULES + 8} />
          <path class="modules" d={qr} />
          <path class="brackets" d={BRACKETS} aria-hidden="true" />
        </svg>
      </div>
    </div>

    <form class="lookup" onsubmit={onLookup}>
      <label class="field">
        <span class="lbl mono">Look up a code</span>
        <input
          class="mono"
          type="text"
          autocomplete="off"
          autocapitalize="characters"
          spellcheck="false"
          maxlength="8"
          placeholder="ABCD2345"
          value={query}
          oninput={onQuery}
          aria-label="Identity code to look up"
        />
      </label>
      <button class="btn" type="submit" disabled={looking}>{looking ? 'Looking' : 'Look up'}</button>
    </form>
    {#if result}<Notice tone={result.tone} text={result.text} />{/if}
  </CarvedSlab>
{:else}
  <SealedPanel
    slab={true}
    {status}
    loadingText="minting your sign"
    action="none"
    errorText="Your identity code could not be read right now. Try again shortly."
  />
{/if}

{#if confirmOpen}
  <Modal title="Rotate your code" size="sm" onClose={() => (confirmOpen = false)}>
    <p class="warn">
      Rotating mints a new code and retires <span class="mono">{code}</span>. Anyone still holding
      the old one will not find you with it, so give out the new one. This cannot be undone.
    </p>
    <div class="rowbtns">
      <button class="btn danger" type="button" onclick={rotate} disabled={rotating}>
        {rotating ? 'Rotating' : 'Rotate'}
      </button>
      <button class="btn" type="button" onclick={() => (confirmOpen = false)} disabled={rotating}>Cancel</button>
    </div>
  </Modal>
{/if}

<style>
  .plate {
    display: flex; flex-wrap: wrap; gap: var(--space-5);
    align-items: flex-start; margin-top: var(--space-3);
  }
  .who { flex: 1 1 18rem; min-width: 0; }
  .code {
    margin: 0; font-size: var(--text-2xl); letter-spacing: .34em;
    color: var(--accent-text); word-break: break-all;
  }
  .explain { margin: var(--space-2) 0 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; max-width: 46ch; }
  .rotated { margin: var(--space-2) 0 0; font-size: var(--text-xs); color: var(--text-muted); }

  .qr { flex: 0 0 auto; }
  .qr svg { width: 168px; height: 168px; display: block; }
  .qr .quiet { fill: var(--ls-bleach-2); }
  .qr .modules { fill: var(--ls-bleach-ink); shape-rendering: crispEdges; }
  .qr .brackets { fill: none; stroke: var(--accent); stroke-width: 1; }

  .lookup { display: flex; flex-wrap: wrap; align-items: flex-end; gap: var(--space-2); margin-top: var(--space-5); }
  .field { display: flex; flex-direction: column; gap: var(--space-1); flex: 1 1 12rem; }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; }
  .lookup input {
    font-size: var(--text-sm); letter-spacing: .2em; text-transform: uppercase;
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-2);
  }

  .warn { margin: 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; }
  .rowbtns { display: flex; gap: var(--space-2); }

  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; cursor: pointer;
    padding: var(--space-2) var(--space-4); border-radius: var(--radius-sm);
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge);
  }
  .who .btn { margin-top: var(--space-3); }
  .btn:hover:not(:disabled) { border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn.danger { color: var(--bg-deep); background: var(--ls-red); border-color: var(--ls-red); }
</style>
