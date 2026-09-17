<script>
  // One solo player raising a signal on the Seeker Wall. Amber (a call, not live
  // telemetry). A Message button reveals an inline DM composer addressed to this
  // seeker by character name (the server resolves the account).
  import RecruitingBeacon from './RecruitingBeacon.svelte';
  import MessageComposer from './MessageComposer.svelte';

  let { seeker, canMessage = false } = $props();

  let name = $derived(seeker?.char_name || 'A lone survivor');
  let updated = $derived(seeker?.updated_at || '');
  let composing = $state(false);
</script>

<article class="seeker">
  <div class="head">
    <div class="titles">
      <h3 class="name">{name}</h3>
      {#if seeker?.role}<span class="role mono">{seeker.role}</span>{/if}
    </div>
    <RecruitingBeacon label="Seeking" />
  </div>

  {#if seeker?.note}<p class="note">{seeker.note}</p>{/if}

  {#if seeker?.playstyle || seeker?.timezone}
    <div class="tags">
      {#if seeker?.playstyle}<span class="tag mono">{seeker.playstyle}</span>{/if}
      {#if seeker?.timezone}<span class="tag mono">{seeker.timezone}</span>{/if}
    </div>
  {/if}

  <div class="foot">
    {#if updated}<span class="when mono">raised {updated}</span>{/if}
    {#if canMessage}
      <button class="btn" type="button" onclick={() => (composing = !composing)}>{composing ? 'Close' : 'Message'}</button>
    {/if}
  </div>

  {#if composing && canMessage}
    <div class="dm">
      <MessageComposer recipientCharName={name} locked={true} onSent={() => (composing = false)} />
    </div>
  {/if}
</article>

<style>
  .seeker {
    display: flex; flex-direction: column; gap: var(--space-2);
    padding: var(--space-3) var(--space-4);
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
  .titles { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
  .name { font-size: var(--text-base); letter-spacing: .03em; color: var(--text); overflow: hidden; text-overflow: ellipsis; }
  .role { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .08em; text-transform: uppercase; }
  .note { margin: 0; color: var(--text-muted); font-size: var(--text-sm); line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
  .tags { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .tag { font-size: var(--text-xs); color: var(--text-muted); padding: var(--space-1) var(--space-2); background: var(--bg-elevated); border: 1px solid var(--edge); border-radius: var(--radius-sm); }
  .foot { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
  .when { font-size: var(--text-xs); color: var(--text-muted); }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer;
  }
  .btn:hover { border-color: var(--accent); }
  .dm { margin-top: var(--space-2); padding-top: var(--space-3); border-top: 1px solid var(--edge); }
</style>
