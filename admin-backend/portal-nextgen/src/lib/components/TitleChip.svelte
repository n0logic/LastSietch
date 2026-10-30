<script>
  // A cosmetic title beside a player's name (COSMETICS-MAP-ICONS-PLAN P0).
  // `title` is {key, name} exactly as the server sent it; the server only sends
  // one for a title the player owns and wears, so this component never decides
  // ownership. Ember, not purple (owner 2026-10-29): purple is spice on this
  // portal. A title with badge art shows it; the rest are text only.
  let { title = null } = $props();

  const BADGES = {
    nightwalker: '/img/sietch-nights/badge-night-walker-256.webp?v=20261029a',
  };

  let badge = $derived(title?.key ? BADGES[title.key] || null : null);
</script>

{#if title?.name}
  <span class="title-chip mono">
    {#if badge}<img class="badge" src={badge} alt="" width="14" height="14" loading="lazy" decoding="async" />{/if}
    <span class="label">{title.name}</span>
  </span>
{/if}

<style>
  .title-chip {
    --title-ember: #ff9a5c;
    --title-ember-edge: rgba(255, 154, 92, .45);
    display: inline-flex; align-items: center; gap: 4px; vertical-align: middle;
    margin-left: var(--space-2); padding: 1px 7px 1px 4px; max-width: 100%;
    font-size: 10px; line-height: 1.5; letter-spacing: .1em; text-transform: uppercase;
    white-space: nowrap; color: var(--title-ember);
    border: 1px solid var(--title-ember-edge); border-radius: 999px;
    background: rgba(255, 120, 40, .08);
  }
  .title-chip:not(:has(.badge)) { padding-left: 7px; }
  :global([data-theme='day']) .title-chip {
    --title-ember: #8f3a0c;
    --title-ember-edge: rgba(143, 58, 12, .45);
  }
  .badge { width: 14px; height: 14px; flex: none; border-radius: 50%; object-fit: cover; }
  .label { overflow: hidden; text-overflow: ellipsis; }
</style>
