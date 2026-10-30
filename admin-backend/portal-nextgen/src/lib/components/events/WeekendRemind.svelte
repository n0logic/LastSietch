<script>
  // Standing weekend reminder opt-in (player portal plan B3). One switch on the
  // account prefs doc (`weekend_remind`), so it follows the player to any browser.
  //
  // Hidden until BOTH are true: the server reports the feature on
  // (LASTSIETCH_WEEKEND_REMIND_ENABLED, via /portal/features) and the viewer is a signed-in,
  // linked player. A switch that cannot save would be a promise nobody keeps.
  //
  // Honest copy: delivery is the portal inbox about an hour before the event
  // starts. It does not say Discord, because no Discord message is sent.
  import { onMount } from 'svelte';
  import { api } from '$lib/api.js';
  import { auth } from '$lib/auth.svelte.js';
  import { getPref, prefs, setPref } from '$lib/prefs.svelte.js';

  let enabled = $state(false);
  onMount(async () => {
    try {
      const data = await api.server.features();
      enabled = data?.features?.weekend_remind === true;
    } catch (e) {
      enabled = false;
    }
  });

  let show = $derived(enabled && auth.status === 'authed' && !!auth.discordHandle && prefs.status === 'ready');
  let on = $derived(getPref('weekend_remind') === true);
</script>

{#if show}
  <div class="wr">
    <button
      type="button" class="switch" class:on role="switch" aria-checked={on}
      aria-labelledby="wr-label" aria-describedby="wr-help"
      onclick={() => setPref('weekend_remind', !on)}
    ><span class="knob" aria-hidden="true"></span></button>
    <div class="text">
      <p id="wr-label" class="label">Remind me before every weekend event</p>
      <p id="wr-help" class="help">
        You get a notice in your inbox about an hour before each weekend bonus starts.
        On XP and crafting weekends, all maps restart for about 20 minutes at the start
        and again at the end. Turn it off here any time.
      </p>
    </div>
  </div>
{/if}

<style>
  .wr { margin-top: var(--space-6); display: flex; gap: var(--space-3); align-items: flex-start; padding: var(--space-3) var(--space-4); background: var(--panel); border: 1px solid var(--edge); border-radius: var(--radius-sm); }
  .text { min-width: 0; }
  .label { margin: 0; font-size: var(--text-sm); color: var(--text); }
  .help { margin: var(--space-1) 0 0; font-size: var(--text-xs); color: var(--text-muted); }
  .switch {
    flex: none; position: relative; width: 2.75rem; height: 1.5rem; padding: 0;
    background: var(--metal-1); border: 1px solid var(--edge); border-radius: 999px; cursor: pointer;
    transition: background var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
  }
  .knob {
    position: absolute; top: 2px; left: 2px; width: calc(1.5rem - 6px); height: calc(1.5rem - 6px);
    background: var(--text-muted); border-radius: 50%;
    transition: transform var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .switch.on { background: var(--accent); border-color: var(--accent); }
  .switch.on .knob { transform: translateX(1.25rem); background: var(--bg-deep); }
  .switch:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  @media (prefers-reduced-motion: reduce) { .switch, .knob { transition: none; } }
</style>
