<script>
  // One guild in the Signal Board directory. The recruiting call wears amber
  // (RecruitingBeacon). NOTE: /portal/guilds/data does NOT carry a per-guild online
  // census (too heavy to fan across the whole directory per poll), so the directory
  // shows only the total member count, never an Ibad-blue live online number. Live
  // presence is available only for the viewer's OWN open guild (via /presence). A
  // linked player not already in THIS guild can raise a join request (LIVE).
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import RecruitingBeacon from './RecruitingBeacon.svelte';
  import RequestToJoinButton from './RequestToJoinButton.svelte';

  let { guild, canRequest = false } = $props();

  let name = $derived(guild?.name || 'Unnamed sietch');
  let faction = $derived(guild?.faction || '');
  let memberCount = $derived(Number(guild?.member_count) || 0);
  let isMine = $derived(guild?.is_mine === true);
  let rec = $derived(guild?.recruiting || null);
  let recruiting = $derived(rec?.open === true || rec?.open === 1);
  let npFriendly = $derived(rec?.new_player_friendly === true || rec?.new_player_friendly === 1);
  let blurb = $derived(rec?.message || guild?.description || '');
  // Render-side XSS guard (defense-in-depth; dev-1 also validates the scheme
  // server-side): only treat discord_url as a clickable link when it is an explicit
  // https:// URL. Anything else (javascript:, data:, http:, junk) is omitted.
  let safeDiscord = $derived(
    typeof rec?.discord_url === 'string' && /^https:\/\//i.test(rec.discord_url.trim())
      ? rec.discord_url.trim()
      : ''
  );
</script>

<CarvedSlab elevation={2} hot={recruiting}>
  <div class="head">
    <div class="titles">
      <h3 class="gname">{name}</h3>
      {#if faction}<span class="faction mono">{faction}</span>{/if}
    </div>
    {#if recruiting}<RecruitingBeacon />{/if}
  </div>

  <div class="census">
    <span class="total mono">{memberCount} {memberCount === 1 ? 'member' : 'members'}</span>
    {#if isMine}<span class="mine mono">your sietch</span>{/if}
  </div>

  {#if blurb}<p class="blurb">{blurb}</p>{/if}

  {#if recruiting && (rec?.playstyle || rec?.timezone || rec?.language || npFriendly)}
    <div class="tags">
      {#if rec?.playstyle}<span class="tag mono">{rec.playstyle}</span>{/if}
      {#if rec?.timezone}<span class="tag mono">{rec.timezone}</span>{/if}
      {#if rec?.language}<span class="tag mono">{rec.language}</span>{/if}
      {#if npFriendly}<span class="tag mono np">new-player friendly</span>{/if}
    </div>
  {/if}

  <div class="foot">
    {#if safeDiscord}
      <a class="discord" href={safeDiscord} target="_blank" rel="noopener noreferrer">Discord</a>
    {/if}
    {#if canRequest && !isMine}
      <RequestToJoinButton guildId={guild.guild_id} guildName={name} />
    {/if}
  </div>
</CarvedSlab>

<style>
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
  .titles { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
  .gname {
    font-size: var(--text-lg); letter-spacing: .04em; text-transform: uppercase;
    color: var(--text); overflow: hidden; text-overflow: ellipsis;
  }
  .faction { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .1em; text-transform: uppercase; }
  .census { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-3); margin-top: var(--space-3); }
  .total { font-size: var(--text-xs); color: var(--text-muted); }
  .mine { font-size: var(--text-xs); color: var(--accent-text); letter-spacing: .08em; text-transform: uppercase; }
  .blurb { color: var(--text-muted); font-size: var(--text-sm); line-height: 1.45; margin: var(--space-3) 0 0; }
  .tags { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-3); }
  .tag {
    font-size: var(--text-xs); letter-spacing: .06em;
    color: var(--text-muted); padding: var(--space-1) var(--space-2);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .tag.np { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent) 40%, var(--edge)); }
  .foot { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; margin-top: var(--space-4); }
  .discord {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; color: var(--accent-text); text-decoration: none;
    padding: var(--space-1) var(--space-3); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .discord:hover { border-color: var(--accent); }
</style>
