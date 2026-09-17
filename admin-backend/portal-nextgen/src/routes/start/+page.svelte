<script>
  import { onDestroy, untrack } from 'svelte';
  import { base } from '$app/paths';
  import { auth } from '$lib/auth.svelte.js';
  import { api } from '$lib/api.js';
  import { welcomeReading } from '$lib/start.js';
  import PageHeader from '$lib/components/PageHeader.svelte';

  const joiningUrl = import.meta.env.VITE_REVIEW_PREVIEW === 'true'
    ? 'http://localhost:4174/dune/#connect' : 'https://lastsietch.com/dune/#connect';

  let packages = $state(null);
  let readStatus = $state('idle');
  let loadedFor = '';
  let requestId = 0;
  let linked = $derived(auth.status === 'authed' && auth.linked.length > 0);
  let accountKey = $derived(linked ? `${auth.discordHandle}:${auth.accounts.find((a) => a.active)?.account_id ?? auth.linked[0]?.character_name ?? ''}` : '');
  let reading = $derived(welcomeReading(packages));
  let lastRead = $derived(packages?.read_at && Number.isFinite(Date.parse(packages.read_at))
    ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(packages.read_at)) : null);

  async function loadDeliveries() {
    const token = ++requestId;
    packages = null;
    readStatus = 'loading';
    try {
      const response = await api.deliveries.list();
      if (token !== requestId) return;
      packages = response;
      readStatus = response?.available === true && Array.isArray(response.packages) ? 'ready' : 'error';
    } catch (_) {
      if (token === requestId) readStatus = 'error';
    }
  }

  $effect(() => {
    const key = accountKey;
    if (key && key !== loadedFor) {
      loadedFor = key;
      untrack(loadDeliveries);
    } else if (!key) {
      loadedFor = '';
      requestId += 1;
      packages = null;
      readStatus = 'idle';
    }
  });
  onDestroy(() => { requestId += 1; });
</script>

<svelte:head><title>First session | Last Sietch</title><meta name="description" content="Join Last Sietch, connect your portal account, find your welcome package and meet the community." /></svelte:head>

<div class="page">
  <a class="back" href={`${base}/`}>← Back to Home</a>
  <PageHeader kicker="Last Sietch | arrival guide" title="Your first session"
    sub="Find the server, get your bearings and make the portal useful to you. Start wherever you are in the journey." />
  <div class="intro">
    <div><h2>The game comes first.</h2><p>You join Last Sietch through Dune: Awakening's in-game server browser. Connecting Discord links the companion portal; it does not join the game or create a character for you.</p></div>
    <img src="/img/v3/plates/habbanya-dawn-clear-small.jpg" alt="" width="640" height="360" />
  </div>

  <ol class="journey">
    <li>
      <span class="step-number" aria-hidden="true">01</span><div>
        <p class="step-label">In the game</p><h2>Find your sietch</h2>
        <p>Open the PC game's server browser: <strong>Experimental</strong> tab, <strong>North America</strong>, search <strong>Last Sietch</strong>. Expand the row and choose your world.</p>
        <ul class="worlds"><li><strong>Habbanya</strong><span>PvE</span></li><li><strong>Kulon</strong><span>PvP with safe zones</span></li><li><strong>Amtal-Full-PvP</strong><span>Full PvP outside tradeposts</span></li></ul>
        <p class="note">A Custom Character warning is expected. Read it before confirming. Amtal's PvE zone banner is misleading; its open world is PvP. Deep Desert has separate PvE and PvP choices.</p>
        <a href={joiningUrl}>Joining guide with screenshots <span aria-hidden="true">↗</span></a>
      </div>
    </li>
    <li>
      <span class="step-number" aria-hidden="true">02</span><div>
        <p class="step-label">In the companion</p><h2>Link your game account</h2>
        <p>Sign in and follow the available game-account verification steps. If you already use the portal through Discord, sign in with that same Discord account to keep your existing profile and history.</p>
        <div class="account-reading" role="status">
          {#if auth.status === 'loading'}Reading your portal connection
          {:else if linked}A game account is linked to your profile. Use Settings to check the active account and character.
          {:else if auth.status === 'authed'}Discord is connected; no linked game account is shown yet.
          {:else}Sign in to read your own account and delivery status.{/if}
        </div>
        {#if linked}<a href={`${base}/settings`}>Check your linked account <span aria-hidden="true">↗</span></a>
        {:else}<a href={auth.status === 'authed' ? '/settings' : '/login'} data-sveltekit-reload>{auth.status === 'authed' ? 'Continue account linking' : 'Sign in'} <span aria-hidden="true">↗</span></a>{/if}
      </div>
    </li>
    <li>
      <span class="step-number" aria-hidden="true">03</span><div>
        <p class="step-label">Your welcome package</p><h2>Know where each part lands</h2>
        <p>Starter equipment and supplies are sent during your first session. Anything that does not fit in your bag waits at a tradepost's <strong>CHOAM Exchange</strong>, in the <strong>Completed</strong> tab.</p>
        <p class="note">The game labels these held items <strong>CANCELED</strong>. Select <strong>Take item</strong> to collect them. Intel Points, Base Construction research and House Scrip are applied after logout.</p>
        {#if linked}
          <div class="package-reading" role="status">
            {#if readStatus === 'loading'}Reading your recorded deliveries
            {:else}<strong>{reading.label}</strong><p>{reading.detail}</p>{#if lastRead}<small>Last read: {lastRead}</small>{/if}{/if}
          </div>
          <div class="step-actions"><a href={`${base}/mailbox#deliveries`}>Open your delivery details <span aria-hidden="true">↗</span></a>{#if readStatus === 'error'}<button type="button" onclick={loadDeliveries}>Try the read again</button>{/if}</div>
        {:else}<p class="note">Link your game account to see its recorded package status here. You do not claim the welcome package from this guide.</p><a href={`${base}/help#howto-deliveries`}>How package delivery works <span aria-hidden="true">↗</span></a>{/if}
      </div>
    </li>
    <li>
      <span class="step-number" aria-hidden="true">04</span><div>
        <p class="step-label">Make yourself at home</p><h2>Find your people and your next outing</h2>
        <p>Browse guilds, talk in portal chat or check the event calendar. You can also compare player trades and explore community base blueprints when you are ready.</p>
        <div class="destinations"><a href={`${base}/guilds`}>Find a guild</a><a href={`${base}/chat`}>Portal chat</a><a href={`${base}/events`}>Event calendar</a><a href={`${base}/karum`}>Player trades</a><a href={`${base}/bases`}>Base blueprints</a></div>
        <p class="note">Need a hand? <a href="https://discord.gg/your-invite" target="_blank" rel="noopener noreferrer">Join the community Discord</a> and ask in <a href="https://discord.com/channels/<guild-id>/<channel-id>" target="_blank" rel="noopener noreferrer">help-and-feedback</a>. Include what you tried and a screenshot when useful. Never share passwords or login codes.</p>
      </div>
    </li>
  </ol>
  <p class="footnote">This is guidance, not an automatic checklist. Opening a page does not mean you joined the game, received a package or completed an action. <a href={`${base}/help`}>Help and changelog</a></p>
</div>

<style>
  .page { max-width: 1120px; padding: 28px 24px 64px; margin: auto; }
  .back { display: inline-block; margin-bottom: 20px; font-size: 13px; }
  .intro { display: grid; grid-template-columns: minmax(0,1.35fr) minmax(0,.85fr); gap: 24px; align-items: center; margin: 8px 0 32px; padding: 24px; background: var(--panel); border-top: 2px solid var(--accent-soft); border-radius: 5px; }
  .intro img { width: 100%; height: 150px; object-fit: cover; border-radius: 4px; }
  h2 { font: 700 27px/1.2 var(--font-display); color: var(--text); margin: 0 0 12px; }
  p { font-size: 15px; line-height: 1.65; color: var(--text-muted); margin: 0 0 12px; max-width: 65ch; }
  strong { color: var(--text); font-weight: 500; }
  .journey { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 24px; }
  .journey > li { display: grid; grid-template-columns: 28px minmax(0,1fr); gap: 14px; border-top: 1px solid var(--edge); padding-top: 24px; }
  .step-number { font: 400 13px var(--font-mono); color: var(--accent-text); padding-top: 4px; }
  .step-label { font-size: 12px; margin-bottom: 7px; color: var(--accent-text); }
  .worlds { list-style: none; padding: 0; margin: 16px 0; display: grid; gap: 8px; }
  .worlds li { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 6px 12px; font-size: 13px; }
  .worlds span { color: var(--text-muted); }
  .note { font-size: 13px; line-height: 1.6; }
  .account-reading, .package-reading { padding: 12px; border-left: 2px solid var(--accent-soft); background: var(--bg-elevated); margin: 16px 0; font-size: 13px; color: var(--text-muted); }
  .package-reading p { font-size: 13px; margin: 6px 0; }
  .package-reading small { font-size: 11px; }
  .journey a { font-size: 14px; }
  .step-actions { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
  .step-actions button { color: var(--accent-text); background: transparent; border: 1px solid var(--edge); padding: 8px 12px; border-radius: 4px; font: inherit; font-size: 13px; cursor: pointer; min-height: 44px; }
  .destinations { display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 16px 0; }
  .destinations a { min-height: 36px; display: inline-flex; align-items: center; }
  .footnote { border-top: 1px solid var(--edge); margin-top: 32px; padding-top: 20px; font-size: 12px; }
  @media (max-width: 759px) { .page { padding: 20px 16px 48px; } .intro, .journey { grid-template-columns: minmax(0,1fr); } .intro { padding: 20px; gap: 16px; } .intro img { height: 120px; } .journey > li { grid-template-columns: 24px minmax(0,1fr); gap: 10px; } }
</style>
