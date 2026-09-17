<script>
  // "Download my data", relocated from the Character page. Still gated on the
  // server flag (auth.exportEnabled mirrors /portal/me.export_enabled): when the
  // flag is dark the control is not rendered at all and the panel says so
  // plainly, because a button that answers "not available yet" reads as broken.
  // downloadMyData() branches on Content-Disposition, so a deferred or failed
  // export never looks like a saved file.
  import { auth } from '$lib/auth.svelte.js';
  import { downloadMyData } from '$lib/api.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import Notice from '$lib/components/Notice.svelte';

  let exporting = $state(false);
  let note = $state(null); // { tone, text }

  async function onExport() {
    if (exporting) return;
    exporting = true; note = null;
    const r = await downloadMyData();
    exporting = false;
    if (r.ok) note = { tone: 'ok', text: `Downloaded ${r.filename}` };
    else if (r.deferred) note = { tone: 'info', text: r.message || 'Not available yet.' };
    else note = { tone: 'error', text: r.message || 'Export failed, please try again.' };
  }
</script>

<CarvedSlab>
  <p class="kicker mono">Your data | keepsake copy</p>

  <p class="explain">Your character lives on the server and is backed up every six hours. If the server goes down for maintenance, your progress is safe.</p>

  {#if auth.exportEnabled}
    <button class="btn" type="button" onclick={onExport} disabled={exporting}>
      {exporting ? 'Preparing your file...' : 'Download my character data'}
    </button>
    <p class="note">A snapshot of the selected character as of now: level, faction, specializations, storage and journey progress. It is a copy to keep, not a self-restore.</p>
    {#if note}<Notice tone={note.tone} text={note.text} />{/if}
  {:else}
    <SealedPanel
      status="empty" action="none"
      emptyText="Personal exports are not switched on yet. When they are, you can take your keepsake copy from here."
    />
  {/if}
</CarvedSlab>

<style>
  .explain { margin: var(--space-3) 0 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; }
  .note { margin: var(--space-2) 0 0; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.5; }
  .btn {
    margin-top: var(--space-3); font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase; cursor: pointer;
    padding: var(--space-2) var(--space-4); border-radius: var(--radius-sm);
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge);
  }
  .btn:hover:not(:disabled) { border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
</style>
