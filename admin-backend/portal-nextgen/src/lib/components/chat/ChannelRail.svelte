<script>
  // Channel rail. Four groups in a fixed order (Sietch, Guild, Houses, Maps) off
  // the `kind` the server sends, never off the id string: an id is an opaque
  // token and parsing it here would be a second place that decides what a
  // channel is.
  //
  // A group with no channels does not render. That is the whole membership
  // story on this surface: a player who is in no guild simply has no Guild
  // group, and the rail never explains an absence it cannot see the reason for.
  //
  // Below the tablet width the rail collapses to a <select> above the pane, so
  // the message list keeps the full column on a phone.
  let { channels = [], active = '', onselect } = $props();

  const GROUPS = [
    { kind: 'sietch', label: 'Sietch' },
    { kind: 'guild', label: 'Guild' },
    { kind: 'faction', label: 'Houses' },
    { kind: 'map', label: 'Maps' },
  ];

  let grouped = $derived(
    GROUPS.map((g) => ({
      ...g,
      items: channels.filter((c) => c && c.kind === g.kind),
    })).filter((g) => g.items.length > 0)
  );

  function pips(n) {
    const v = Number(n) || 0;
    return v > 99 ? '99+' : String(v);
  }
</script>

<nav class="rail" aria-label="Chat channels">
  {#each grouped as group (group.kind)}
    <div class="group">
      <p class="kicker mono">{group.label}</p>
      <ul>
        {#each group.items as c (c.id)}
          <li>
            <button
              type="button"
              class="chan"
              class:on={c.id === active}
              class:unread={Number(c.unread) > 0 && c.id !== active}
              aria-current={c.id === active ? 'true' : undefined}
              onclick={() => onselect?.(c.id)}
            >
              <span class="label">{c.label}</span>
              {#if Number(c.unread) > 0 && c.id !== active}
                <span class="pip mono" aria-label="{c.unread} unread">{pips(c.unread)}</span>
              {/if}
            </button>
          </li>
        {/each}
      </ul>
    </div>
  {/each}
</nav>

<label class="picker">
  <span class="kicker mono">Channel</span>
  <select value={active} onchange={(e) => onselect?.(e.currentTarget.value)}>
    {#each grouped as group (group.kind)}
      <optgroup label={group.label}>
        {#each group.items as c (c.id)}
          <option value={c.id}>
            {c.label}{Number(c.unread) > 0 && c.id !== active ? ` (${pips(c.unread)})` : ''}
          </option>
        {/each}
      </optgroup>
    {/each}
  </select>
</label>

<style>
  .rail { display: flex; flex-direction: column; gap: var(--space-4); }
  .group { display: flex; flex-direction: column; gap: var(--space-2); }
  /* Group names are chrome: amber, never Ibad blue. */
  .kicker {
    color: var(--accent); text-transform: uppercase; letter-spacing: .28em;
    font-size: var(--text-xs); margin: 0;
  }
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }

  .chan {
    width: 100%; display: flex; align-items: center; justify-content: space-between;
    gap: var(--space-2); text-align: left; cursor: pointer;
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text-muted);
    background: transparent; border: 1px solid transparent;
    border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2);
    transition: color var(--motion-fast) var(--ease-out),
                background var(--motion-fast) var(--ease-out),
                border-color var(--motion-fast) var(--ease-out);
  }
  .chan:hover { color: var(--text); background: var(--metal-0); }
  .chan:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .chan.on {
    color: var(--text); background: var(--metal-0);
    border-color: color-mix(in srgb, var(--accent) 46%, var(--edge));
  }
  .label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  /* A room with unread messages reads bright and bold until it is opened. */
  .chan.unread .label { color: var(--text); font-weight: 600; }

  /* The pip is live data, so it is the one Ibad-blue thing in the rail. */
  .pip {
    flex: none; font-size: var(--text-xs); line-height: 1;
    color: var(--bg-deep); background: var(--ls-ibad);
    border-radius: 999px; padding: 2px var(--space-2);
  }

  .picker { display: none; flex-direction: column; gap: var(--space-1); }
  .picker select {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-2) var(--space-3);
    box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .picker select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  @media (max-width: 780px) {
    .rail { display: none; }
    .picker { display: flex; }
  }

  @media (prefers-reduced-motion: reduce) {
    .chan { transition: none; }
  }
</style>
