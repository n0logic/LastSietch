<script>
  // The chat moderation queue: what players reported, and who is currently
  // muted.
  //
  // TWO ways in, and the second one is not a role. An admin reaches this page
  // through useAuthGate's server-granted `admin` role. A guild leader reaches it
  // because the channels payload says can_moderate on a guild channel they can
  // see, which is the same helper the backend uses to decide the write. Both
  // gates only hide controls: portal_chat_mod.py answers `forbidden` to everyone
  // else, and a leader's queue is filtered to their own guild channel server
  // side, not here. Nothing on this page grants anything.
  //
  // Open reports lead because an open report is the only row anyone has to do
  // something about. "All" is a second read, not a client-side filter of the
  // first: a resolved report is history and the queue should not carry a month
  // of it around to draw four rows.
  //
  // Every report row prints the message it is about. A queue that showed only
  // "reported for spam" would make a moderator open a channel and scroll for the
  // thing they are judging, and the two most common actions here are one click
  // away from a mistake. The body renders as TEXT, never markup: it is player
  // input on a page whose readers hold the delete button.
  import { untrack } from 'svelte';
  import { base } from '$app/paths';
  import { api } from '$lib/api.js';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import MuteDialog from '$lib/components/chat/MuteDialog.svelte';

  const admin = useAuthGate({ role: 'admin' });

  const UTC_FMT = new Intl.DateTimeFormat(undefined, {
    timeZone: 'UTC', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  });

  function parseUtc(s) {
    if (typeof s !== 'string' || !s.trim()) return null;
    let v = s.trim().replace(' ', 'T');
    if (!/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(v)) v += 'Z';
    const t = Date.parse(v);
    return Number.isNaN(t) ? null : t;
  }
  function stamp(s) {
    const t = parseUtc(s);
    return t == null ? '' : `${UTC_FMT.format(t)} UTC`;
  }

  // --- channels: the leader half of the gate, and the id -> label map --------
  let chanStatus = $state('loading'); // 'loading' | 'ready' | 'error'
  let dark = $state(false);
  let labels = $state({});
  let leader = $state(false);

  // --- the two lists --------------------------------------------------------
  let reportState = $state('open'); // 'open' | 'all'
  let reports = $state([]);
  let reportsStatus = $state('loading');
  let mutes = $state([]);
  let mutesStatus = $state('loading');

  let busyId = $state(null);
  let note = $state(null); // { tone, text }
  let muteFor = $state(null); // the report row whose delete_and_mute is open
  let muteError = $state(''); // a refusal that belongs INSIDE the mute dialog

  // Every refusal from portal_chat_mod.py is a 200 carrying
  // {ok:false, error, message}, which sendCsrfJSON turns into a throw with the
  // envelope on err.data. The envelope's `message` is deliberately NOT used:
  // _TEXT in portal_chat.py only has copy for the composer's own tokens, so a
  // `forbidden` here would render as "That message could not be sent." The
  // queue's refusals are the queue's own sentences.
  const REFUSALS = {
    forbidden: 'That is not yours to act on.',
    report_not_found: 'That report is already gone. The queue has been re-read.',
    mute_not_found: 'That mute has already been lifted. The list has been re-read.',
    not_found: 'That message is already gone.',
    unknown_player: 'No player answers to that name any more.',
    bad_request: 'That could not be applied. Check the mute length and the reason.',
    chat_disabled: 'Chat is being fitted. Back soon.',
  };
  function refusal(err, fallback) {
    return REFUSALS[err?.data?.error] || fallback;
  }

  let allowed = $derived(admin.allowed || leader);
  let gateReason = $derived(
    admin.loading || (admin.authed && chanStatus === 'loading') ? 'loading'
      : !admin.authed ? 'anon'
      : allowed ? 'ok'
      : 'missing_role'
  );

  function channelLabel(id) {
    const key = String(id || '');
    if (!key) return 'every channel';
    return labels[key] || key;
  }

  async function loadChannels() {
    chanStatus = 'loading';
    try {
      const r = await api.chat.channels();
      if (r?.ok === false) {
        dark = r?.error === 'chat_disabled';
        leader = false;
        chanStatus = dark ? 'ready' : 'error';
        return;
      }
      const rows = Array.isArray(r?.channels) ? r.channels : [];
      const map = {};
      for (const c of rows) if (c?.id) map[String(c.id)] = String(c.label || c.id);
      labels = map;
      leader = rows.some((c) => c?.kind === 'guild' && c?.can_moderate === true);
      chanStatus = 'ready';
    } catch (e) {
      leader = false;
      chanStatus = 'error';
    }
  }

  async function loadReports() {
    reportsStatus = 'loading';
    try {
      const r = await api.chat.moderation.reports(reportState);
      if (r?.ok === false) { reports = []; reportsStatus = 'error'; return; }
      reports = Array.isArray(r?.reports) ? r.reports : [];
      reportsStatus = 'ready';
    } catch (e) {
      reports = [];
      reportsStatus = 'error';
    }
  }

  async function loadMutes() {
    mutesStatus = 'loading';
    try {
      const r = await api.chat.moderation.mutes();
      if (r?.ok === false) { mutes = []; mutesStatus = 'error'; return; }
      mutes = Array.isArray(r?.mutes) ? r.mutes : [];
      mutesStatus = 'ready';
    } catch (e) {
      mutes = [];
      mutesStatus = 'error';
    }
  }

  // The channels read is the leader half of the gate, so it runs for any signed
  // in viewer, once. The queue reads wait for a yes from either half.
  let channelsAsked = false;
  $effect(() => {
    if (!admin.authed || channelsAsked) return;
    channelsAsked = true;
    untrack(loadChannels);
  });

  let queueAsked = false;
  $effect(() => {
    if (!allowed || queueAsked) return;
    queueAsked = true;
    untrack(() => { loadReports(); loadMutes(); });
  });

  function switchState(next) {
    if (reportState === next) return;
    reportState = next;
    loadReports();
  }

  // --- the three resolve actions -------------------------------------------
  // dismiss: the report was not worth acting on. delete: the message goes, the
  // author stays. delete_and_mute: both, in ONE write, so a moderator cannot end
  // up with a deleted message and a mute that failed.
  async function resolve(report, action, extra = {}) {
    if (busyId != null) return;
    busyId = report?.id ?? null;
    note = null;
    muteError = '';
    try {
      await api.chat.moderation.resolve(report?.id, { action, ...extra });
      muteFor = null;
      note = { tone: 'ok', text: 'Report resolved.' };
      await loadReports();
      if (action === 'delete_and_mute') await loadMutes();
    } catch (err) {
      const text = refusal(err, 'That report could not be resolved right now.');
      // A refusal drawn on the page behind the mute dialog's scrim is a refusal
      // nobody reads. Keep the dialog open and put the sentence inside it.
      if (action === 'delete_and_mute' && muteFor) muteError = text;
      else note = { tone: 'error', text };
      if (err?.data?.error === 'report_not_found') await loadReports();
    } finally {
      busyId = null;
    }
  }

  // MuteDialog hands the payload back rather than writing it: the mute has to
  // ride along with the delete on the resolve call, or the two halves can part.
  function onMuteSubmit(payload) {
    const report = muteFor;
    if (!report) return;
    resolve(report, 'delete_and_mute', {
      minutes: payload.minutes,
      reason: payload.reason,
    });
  }

  async function lift(mute) {
    if (busyId != null) return;
    busyId = `mute-${mute?.id}`;
    note = null;
    try {
      await api.chat.unmute(mute?.id);
      note = { tone: 'ok', text: 'Mute lifted.' };
      await loadMutes();
    } catch (err) {
      note = { tone: 'error', text: refusal(err, 'That mute could not be lifted right now.') };
      if (err?.data?.error === 'mute_not_found') await loadMutes();
    } finally {
      busyId = null;
    }
  }

  let reportsPhase = $derived(
    reportsStatus === 'ready' && reports.length === 0 ? 'empty' : reportsStatus
  );
  let mutesPhase = $derived(
    mutesStatus === 'ready' && mutes.length === 0 ? 'empty' : mutesStatus
  );
</script>

<svelte:head>
  <title>Chat moderation | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | Chat"
    title="Moderation"
    sub="What players reported, and who is muted. An admin sees every channel; a guild leader sees their own guild channel."
  >
    <p class="back"><a href={`${base}/chat`}>Back to chat</a></p>
  </PageHeader>

  {#if gateReason === 'loading'}
    <p class="skeleton">loading</p>
  {:else if gateReason === 'anon'}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to reach the chat moderation queue."
    />
  {:else if dark}
    <SealedPanel
      status="empty"
      emptyText="Chat is being fitted. Back soon."
    />
  {:else if chanStatus === 'error' && !admin.allowed}
    <SealedPanel
      status="error"
      errorText="Your channels could not be read, so this page cannot tell whether you may moderate. Try again in a moment."
    />
  {:else if gateReason === 'missing_role'}
    <SealedPanel
      status="empty" width="prose"
      emptyText="This queue is for admins and guild leaders. Nothing here is yours to act on."
    />
  {:else}
    {#if note}<Notice tone={note.tone} text={note.text} />{/if}

    <section class="block">
      <div class="blockhead">
        <h2>Reports</h2>
        <div class="filters" role="group" aria-label="Report state">
          <button class="chip" type="button" class:on={reportState === 'open'}
            aria-pressed={reportState === 'open'} onclick={() => switchState('open')}>Open</button>
          <button class="chip" type="button" class:on={reportState === 'all'}
            aria-pressed={reportState === 'all'} onclick={() => switchState('all')}>All</button>
        </div>
      </div>

      <SealedPanel
        status={reportsPhase}
        loadingText="loading reports"
        errorText="The report queue could not be read right now. Try again in a moment."
        emptyText={reportState === 'open'
          ? 'No open reports. Nothing is waiting on you.'
          : 'No reports have been filed yet.'}
      >
        <ul class="rows">
          {#each reports as r (r.id)}
            {@const msg = r.message}
            <li class="row">
              <div class="meta mono">
                <span class="chan">{channelLabel(r.channel)}</span>
                <span class="dot" aria-hidden="true">/</span>
                <span class="state" data-state={r.state}>{r.state}</span>
                <span class="dot" aria-hidden="true">/</span>
                <span class="when">{stamp(r.created_utc)}</span>
              </div>

              <!-- The router nests the message under `message`, and keeps its
                   body even once it is deleted: a moderator deciding whether to
                   mute has to read what was actually said. Null means retention
                   has taken the row, and then there is nothing left to judge. -->
              {#if msg}
                <blockquote class="msg">
                  <p class="who mono">
                    {msg.char_name || 'unknown'}
                    <span class="when">{stamp(msg.created_utc)}</span>
                    {#if msg.deleted}<span class="gone">already removed</span>{/if}
                  </p>
                  <p class="body">{msg.body}</p>
                </blockquote>
              {:else}
                <p class="reported">The message this report was filed against is past retention and gone.</p>
              {/if}

              <p class="reported">
                Reported by {r.reporter_char_name || 'a player'}{#if r.reason}: {r.reason}{/if}
              </p>

              {#if r.state === 'open'}
                <div class="acts">
                  <button class="btn" type="button" disabled={busyId != null}
                    onclick={() => resolve(r, 'dismiss')}>Dismiss</button>
                  <button class="btn" type="button" disabled={busyId != null || !msg}
                    onclick={() => resolve(r, 'delete')}>Delete message</button>
                  <!-- No author to silence once the message is gone, and the
                       dialog would open with an empty name and a dead button. -->
                  {#if msg}
                    <button class="btn danger" type="button" disabled={busyId != null}
                      onclick={() => { muteError = ''; muteFor = r; }}>Delete and mute&hellip;</button>
                  {/if}
                </div>
              {:else}
                <p class="resolved mono">
                  {r.resolved_utc ? `${r.state} on ${stamp(r.resolved_utc)}` : r.state}
                </p>
              {/if}
            </li>
          {/each}
        </ul>
      </SealedPanel>
    </section>

    <section class="block">
      <div class="blockhead"><h2>Mutes</h2></div>
      <SealedPanel
        status={mutesPhase}
        loadingText="loading mutes"
        errorText="The mute list could not be read right now. Try again in a moment."
        emptyText="Nobody is muted."
      >
        <ul class="rows">
          {#each mutes as m (m.id)}
            <li class="row mute">
              <div class="muteline">
                <span class="name">{m.char_name || 'unknown'}</span>
                <span class="meta mono">
                  {channelLabel(m.channel)}
                  <span class="dot" aria-hidden="true">/</span>
                  {#if m.until_utc}until {stamp(m.until_utc)}{:else}until lifted{/if}
                  <span class="dot" aria-hidden="true">/</span>
                  by {m.by_kind || 'admin'}
                </span>
                <button class="btn" type="button" disabled={busyId != null}
                  onclick={() => lift(m)}>Lift</button>
              </div>
              {#if m.reason}<p class="reported">{m.reason}</p>{/if}
            </li>
          {/each}
        </ul>
      </SealedPanel>
    </section>
  {/if}
</div>

{#if muteFor}
  <MuteDialog
    charName={muteFor.message?.char_name || ''}
    channel={muteFor.channel || ''}
    channelLabel={channelLabel(muteFor.channel)}
    busy={busyId != null}
    error={muteError}
    onSubmit={onMuteSubmit}
    onClose={() => { muteFor = null; muteError = ''; }}
  />
{/if}

<style>
  .page { padding-bottom: var(--space-8); }
  .back { margin: 0; font-size: var(--text-sm); }
  .block { margin-top: var(--space-6); }
  .blockhead {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: var(--space-3); margin-bottom: var(--space-3); flex-wrap: wrap;
  }
  .blockhead h2 {
    margin: 0; font-size: var(--text-lg); text-transform: uppercase; letter-spacing: .12em;
  }
  .filters { display: inline-flex; gap: var(--space-1); }
  .chip {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3);
    background: transparent; color: var(--text-muted);
    border: 1px solid var(--edge); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .chip:hover { border-color: var(--accent); }
  .chip.on { color: var(--accent-text); border-color: var(--accent); }

  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-3); width: 100%; }
  .row {
    display: flex; flex-direction: column; gap: var(--space-2);
    padding: var(--space-3);
    background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .meta { font-size: var(--text-xs); color: var(--text-muted); display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: baseline; }
  .dot { opacity: .5; }
  .state[data-state='open'] { color: var(--accent-text); }
  .msg {
    margin: 0; padding: var(--space-2) var(--space-3);
    background: var(--metal-0); border-left: 2px solid var(--edge); border-radius: var(--radius-sm);
  }
  .who { margin: 0 0 var(--space-1); font-size: var(--text-xs); color: var(--text-muted); }
  .gone { color: var(--ls-red); text-transform: uppercase; letter-spacing: .1em; }
  /* Player text. Newlines are kept because a message is what it was typed as;
     nothing else in it is interpreted. */
  .body { margin: 0; font-size: var(--text-sm); color: var(--text); white-space: pre-wrap; overflow-wrap: anywhere; }
  .reported { margin: 0; font-size: var(--text-sm); color: var(--text-muted); overflow-wrap: anywhere; }
  .resolved { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }
  .acts { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .mute .muteline { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2) var(--space-3); }
  .mute .name { font-size: var(--text-sm); color: var(--text); }
  .mute .btn { margin-left: auto; }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase; white-space: nowrap;
    padding: var(--space-1) var(--space-3);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent-text); }
  .btn.danger:hover:not(:disabled) { border-color: var(--ls-red); color: var(--ls-red); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
</style>
