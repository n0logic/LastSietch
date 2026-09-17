import { createReadStream, existsSync, readFileSync } from 'node:fs';
import { resolve, extname, sep } from 'node:path';

const stamp = (minutes = 0) => new Date(Date.now() + minutes * 60000).toISOString();
const catalog = [
  { template_id: 'PlastaniumDust', name: 'Plastanium Dust', tier: 6, icon: 'T_UI_IconItemUnknownS_D', is_gradeable: false },
  { template_id: 'DuraluminumBar', name: 'Duraluminum', tier: 5, icon: 'T_UI_IconItemUnknownS_D', is_gradeable: false },
  { template_id: 'Perforator', name: 'Perforator', tier: 6, icon: 'T_UI_IconItemUnknownS_D', is_gradeable: true },
];
const listings = Array.from({ length: 6 }, (_, i) => ({ listing_id: i + 1, ...catalog[i % 3],
  display_name: catalog[i % 3].name, stack_size: i % 3 === 2 ? 1 : 120 + i * 40,
  quality_level: i % 3 === 2 ? 3 : 0, price: 38400 + i * 7600,
  seller_name: ['Sahar', 'Tarek', 'Ishara'][i % 3], status: 'active', created_at: stamp(-60 - i * 23) }));

export function reviewPreview() {
  const docs = { '': { reach_pins: ['/maps/hagga?inst=habbanya', '/storage', '/karum'], reach_recent: ['/rewards'] }, 'char:9001': { activity_seen: stamp(-1440) } };
  return {
    name: 'lastsietch-review-preview',
    configureServer(server) {
      server.middlewares.use(async (request, response, next) => {
        const url = new URL(request.url, 'http://localhost');
        if (url.searchParams.has('preview')) {
          const value = url.searchParams.get('preview');
          if (['habbanya', 'amtal', 'anon', 'unlinked', 'legacy'].includes(value)) response.setHeader('Set-Cookie', `ls-preview=${value}; Path=/; SameSite=Lax`);
        }
        if (['normal', 'unavailable', 'no-reset'].includes(url.searchParams.get('clock'))) {
          const prior = response.getHeader('Set-Cookie');
          response.setHeader('Set-Cookie', [...(prior ? [prior] : []), `ls-clock-preview=${url.searchParams.get('clock')}; Path=/; SameSite=Lax`]);
        }
        const mode = request.headers.cookie?.match(/ls-preview=(habbanya|amtal|anon|unlinked|legacy)/)?.[1] || 'habbanya';
        const anonymous = mode === 'anon';
        const unlinked = mode === 'unlinked';
        const p = url.pathname;
        const json = (value, status = 200) => { response.statusCode = status; response.setHeader('Content-Type', 'application/json'); response.setHeader('Cache-Control', 'no-store'); response.end(JSON.stringify(value)); };
        if (p === '/portal/auth/status') return json({ ok: true, enabled: mode !== 'legacy', game_enabled: mode !== 'legacy', authenticated: !anonymous,
          recent: !anonymous && !unlinked, csrf: 'preview-only', security: anonymous ? null : { username: 'sahar', password: true, discord_linked: false,
            passkeys: [{ id: 'preview-key', label: 'Sahar\'s phone', created_at: Date.now() / 1000 }],
            sessions: [{ id: 'preview-session', method: 'password', current: true, created_at: Date.now()/1000, last_seen: Date.now()/1000, device: 'Preview browser' }] } });
        if (p.startsWith('/portal/auth/')) return json({ ok: false, error: 'sign_in_failed' }, 400);
        if (p.startsWith('/admin/static/')) {
          const root = resolve('..', 'static');
          const file = resolve(root, decodeURIComponent(p.slice('/admin/static/'.length)));
          if (!file.startsWith(root + sep) || !existsSync(file)) { response.statusCode = 404; response.end(); return; }
          response.setHeader('Content-Type', { '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp', '.svg': 'image/svg+xml' }[extname(file)] || 'application/octet-stream');
          createReadStream(file).pipe(response); return;
        }
        if (!p.startsWith('/portal/') && !p.startsWith('/api/') && !p.startsWith('/assets/')) return next();
        if (request.method !== 'GET') {
          if (p === '/portal/settings/prefs' && request.method === 'PUT') {
            let body = ''; for await (const chunk of request) { body += chunk; if (body.length > 12000) return json({ ok: false }, 413); }
            try { const data = JSON.parse(body); docs[data.scope || ''] = { ...(docs[data.scope || ''] || {}), ...data.prefs }; return json({ ok: true, prefs: docs[data.scope || ''], updated_utc: stamp() }); }
            catch { return json({ ok: false }, 400); }
          }
          return json({ ok: false, status: 'deferred', error: 'preview_only', message: 'This is a local preview. No game action was performed.' }, 409);
        }
        if (p === '/portal/me') return json({ authenticated: !anonymous, discord_handle: 'preview-player', linked: anonymous || unlinked ? [] : [{ character_name: 'Sahar' }], accounts: anonymous || unlinked ? [] : [{ account_id: 9001, character_name: 'Sahar', active: true }], roles: [] });
        if (p === '/portal/messages') return json({ ok: true, messages: [] });
        if (p === '/portal/deliveries/v2') {
          if (anonymous || unlinked) return json({ ok: false, error: 'unauthenticated' }, 401);
          if (mode === 'legacy') return json({ available: true, read_at: stamp(), packages: [{ kind: 'welcome', label: 'Welcome Package', granted_at: '2026-05-25T00:31:25Z', state: 'delivered', items: [], legs: [
            { key: 'backpack', label: 'Your backpack', state: 'delivered', at: '2026-05-25T00:31:25Z' },
            { key: 'research', label: 'Base Construction research', state: 'delivered' },
            { key: 'scrip', label: '100,000 House Scrip', state: 'delivered' },
            { key: 'intel', label: '100 Intel points', state: 'delivered' },
          ] }] });
          return json({ available: true, read_at: stamp(), packages: [{ kind: 'welcome', label: 'Welcome Package', granted_at: stamp(-120), state: 'partial', legs: [
            { key: 'backpack', label: 'Your backpack', state: 'delivered', at: stamp(-120) },
            { key: 'exchange', label: 'CHOAM Exchange, Completed tab', state: 'waiting', at: null, note: 'Press Take item at any tradepost terminal.' },
          ], items: [
            { name: 'Solari', icon: 'T_UI_IconResourceSolarisCoin_D', quantity: 25000, where: 'backpack', state: 'delivered' },
            { name: 'Spice Melange', icon: 'T_UI_IconResourceRefinedSpiceR_D', quantity: 100, where: 'backpack', state: 'delivered' },
            { name: 'Granite Stone', icon: 'T_UI_IconResourceRhyoliteR_D', quantity: 250, where: 'backpack', state: 'delivered' },
            { name: 'Scout Ornithopter Engine Mk6', icon: 'T_UI_IconVehCOLEngineR_D', quantity: 1, where: 'exchange', state: 'waiting' },
            { name: 'Base Reconstruction Tool', icon: 'T_UI_IconToolBaseBackup_D', quantity: 1, where: 'exchange', state: 'waiting' },
          ] }] });
        }
        if (p === '/portal/characters') return json({ characters: anonymous || unlinked ? [] : [{ controller_id: 9001, char_name: 'Sahar', lvl: 148, selected: true, online: true }] });
        if (p === '/portal/settings/prefs') return json({ ok: true, identity: docs[''], scopes: { 'char:9001': docs['char:9001'] }, updated_utc: stamp() });
        if (p === '/portal/announcement') return json({ active: false });
        if (p === '/portal/features') return json({ ok: true, features: { chat: true, refinery: true, storage_move: false, market_sell_backpack: true, augment: true, profile_login: mode !== 'legacy', game_login: mode !== 'legacy' } });
        if (p === '/portal/refinery/v2/catalog') {
          if (anonymous) return json({ ok: false, error: 'unauthenticated' }, 401);
          const rates = JSON.parse(readFileSync(resolve('../../scripts/refinery-rates.json'), 'utf8'));
          const names = ['Copper', 'Iron', 'Steel', 'Aluminum', 'Duraluminum', 'Plastanium'];
          return json({ ok: true, enabled: true, holdings_read: true, online: false, offline_ok: true,
            character_name: 'Sahar', rate_version: rates.rate_version,
            caps: { max_batches_per_request: rates.max_batches_per_request, window_days: rates.window_days },
            recipes: rates.recipes.map(recipe => ({ ...recipe,
              input_name: `${names[recipe.tier - 1]} Ingot`, output_name: `Spice-infused ${names[recipe.tier - 1]} Dust`,
              spice_name: 'Spice Melange', held_input: { bank: 125, backpack: 0, toolbar: 0, total: 125 },
              held_spice: { bank: 30, backpack: 0, toolbar: 0, total: 30 }, held_output: 0,
              max_batches_affordable: 5, weekly_dust_cap: rates.weekly_dust_cap,
              weekly_dust_used: 0, weekly_dust_left: rates.weekly_dust_cap })) });
        }
        if (p === '/portal/home/v2') return anonymous ? json({ ok: false }, 401) : json({ ok: true,
          character: { name: 'Sahar', online: true, current_map: 'Hagga Basin', current_partition: mode === 'amtal' ? 33 : 1 },
          wallet: { bank_solari: 2418900 }, orders: { market_open: 3, market_filled_today: 2, karum_open: 1 },
          guild: { name: 'The Ninth Door', invite_count: 0, online_members: 3, members: [{ name: 'Tarek', online: true, map: 'Deep Desert' }, { name: 'Ishara', online: true, map: 'Kulon' }, { name: 'Sahar', online: true, map: mode === 'amtal' ? null : 'Habbanya' }, { name: 'Ashwalker', online: false, last_seen_ago_s: 8400 }] },
          rewards: { enabled: true, claimable_total: 22000, cycle: Array.from({ length: 7 }, (_, i) => ({ cycle_day: i + 1, amount: [10000,12000,15000,18000,22000,24000,28000][i], state: i < 4 ? 'claimed' : i === 4 ? 'claimable' : 'upcoming' })), weekly_claimable: false, monthly_claimable: false },
          deliveries: { count: 2, pending_count: 1, latest: { label: 'Return Package', state: 'partial' } }, mailbox: { unread: 2 }, landsraad: { my_contribution: 12400 }, caps: { transfer_daily_remaining: 3 } });
        if (p === '/api/dune/status') return json({ available: true, maps: [{ name: 'Hagga Basin', players: 14 }], online_players: 31 });
        if (p === '/portal/server/time') {
          const clockMode = request.headers.cookie?.match(/ls-clock-preview=(normal|unavailable|no-reset)/)?.[1] || 'normal';
          if (clockMode === 'unavailable') return json({ available: false }, 503);
          if (clockMode === 'no-reset') return json({ available: true, server_now_utc: stamp(), time_zone: 'America/New_York', coriolis: { available: false } });
          const period = 14 * 86400000;
          const anchor = Date.parse('2026-06-16T05:00:00Z');
          const start = anchor + Math.floor((Date.now() - anchor) / period) * period;
          return json({ available: true, server_now_utc: stamp(), time_zone: 'America/New_York',
            coriolis: { available: true, cycle_start_utc: new Date(start).toISOString(), next_cycle_utc: new Date(start + period).toISOString() } });
        }
        if (p === '/portal/server/overview') return json({ clock: { utc: stamp(), local_hour: 18 }, status: { up: true, title: 'Last Sietch', online_players: 31, maps: [{ name: 'Hagga Basin', players: 14 }] }, world: null });
        if (p.endsWith('/data') && p.startsWith('/portal/maps/')) return json({ has_spice: p.includes('deep-desert'), has_storms: true, instances: p.includes('hagga') ? [{ key:'habbanya', label:'Habbanya', dim:0, part:1 }, { key:'kulon', label:'Kulon', dim:1, part:32 }, { key:'amtal', label:'Amtal', dim:2, part:33 }] : [{ key:'pve', label:'PvE', dim:0 },{ key:'pvp', label:'PvP', dim:1 }] });
        if (p.endsWith('/live') && p.startsWith('/portal/maps/')) return json({ sandstorm: { dimensions: Object.fromEntries([0,1,2].map((dim) => [dim, { next_eta_utc: stamp(23 + dim * 7), mean_interval_min: 48, confidence: .84 }])) }, spice: { dimensions: {} }, worms: { dimensions: {} } });
        if (p.endsWith('/players')) return json({ available: true, map_players: p.includes('deep-desert') ? 9 : 14 });
        if (p === '/api/dune/positions') return json({ available: true, players: Array.from({length:14},(_,i)=>({p:i<9?1:32})) });
        if (p === '/portal/landsraad/standings') return json({ available: true, decided: 16, contested: 9, rails: [{ name:'Atreides', crest:'atreides', score:9 },{ name:'Harkonnen', crest:'harkonnen', score:7 }], term: { end_utc: stamp(2400) } });
        if (p === '/portal/events') return json({ ok: true, events: [{ id: 901, title:'War of Assassins', banner:'war-of-assassins', kind:'faction_war', starts_utc:stamp(3200), ends_utc:stamp(3380), status:'published', description:'Gather your sietch for the next contest on the sand.' }] });
        if (p === '/portal/activity') return json({ ok: true, as_of:stamp(), unavailable: [], has_more:false, items: [{kind:'sale',t:stamp(-42),summary:'Sold Duraluminum',detail:'240 sold · 76,800 Solari',href:'/exchange?tpl=DuraluminumBar'},{kind:'delivery',t:stamp(-80),summary:'Return Package: bank supplies landed',detail:'In your CHOAM bank.',href:'/mailbox#deliveries'},{kind:'alert',t:stamp(-125),summary:'Price alert: Plastanium Dust',detail:'Matched 320 Solari · target 350 Solari',href:'/exchange?tpl=PlastaniumDust'},{kind:'reward',t:stamp(-260),summary:'Claimed 18,000 Solari from the daily reward',href:'/rewards'}] });
        if (p === '/portal/messages/unread-count') return json({ unread:2 });
        if (p === '/portal/market/v2/alerts/count') return json({ ok:true, count:1, alert_count:1 });
        if (p === '/portal/karum/catalog') return json({ ok:true, items:catalog });
        if (p === '/portal/karum') return json({ ok:true, bank_solari:2418900, online:true, offline_ok:false, flags:{karum_enabled:true,karum_wtb_enabled:true}, listings, my_listings:[], my_purchases:[], requests:[], my_requests:[], my_fills:[] });
        if (p === '/portal/karum/search') {
          const q = (url.searchParams.get('q') || '').toLowerCase();
          const template = url.searchParams.get('template');
          const wanted = url.searchParams.get('side') === 'wanted';
          const rows = listings.filter((row) => row.display_name.toLowerCase().includes(q) && (!template || row.template_id === template));
          if (url.searchParams.get('sort') === 'cheap') rows.sort((a, b) => a.price - b.price);
          if (url.searchParams.get('sort') === 'dear') rows.sort((a, b) => b.price - a.price);
          return json({ ok: true, rows: rows.map((row) => wanted ? { ...row, listing_id: undefined, request_id: row.listing_id, requester_name: row.seller_name, quality_mode: 'exact', funded: true } : row), more: false });
        }
        if (p === '/portal/karum/sellable') return json({ok:true,items:[],container_id:1});
        if (p === '/portal/market/v2') return json({ok:true,bank_solari:2418900,watches:[],alerts:[],alert_count:0,tabs:[]});
        if (p === '/portal/market/v2/search') return json({ok:true,rows:catalog.map((row,i)=>({...row,min_price:320+i*80,listing_count:3,quantity:240})),more:false});
        if (p === '/portal/market/v2/item') { const row=catalog.find(item=>item.template_id===url.searchParams.get('tpl'))||catalog[0];return json({ok:true,...row,bank_solari:2418900,ladder:[{order_id:71,price:320,qty:120,quality:3,buyable:true},{order_id:72,price:370,qty:200,quality:4,buyable:true}],bot_tiers:[]}); }
        if (p === '/portal/market/v2/history') return json({ok:true,calibrating:true,points:[]});
        if (p === '/portal/market/v2/my-orders') return json({ok:true,available:true,active:[],completed:[],history:[]});
        if (p === '/portal/market/v2/flips') return json({ok:true,rows:[]});
        if (p === '/portal/market/v2/bot-limits') return json({ok:true,available:false,scopes:[]});
        return json({ok:false,available:false,error:'preview_unavailable',message:'This page is not populated in the local review preview.'},404);
      });
    },
  };
}
