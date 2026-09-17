// Help page content: what changed in each release, and how the portal works.
//
// PLAIN DATA. No Svelte, no store, no DOM: the page imports this, filters it
// through visible() and renders it, and the suite imports the same module under
// node and executes visible() for real. A helper that reached for a rune here
// would make both of those impossible.
//
// EVERY FACT IN A how-to WAS READ OUT OF THE CODE THAT IMPLEMENTS IT, and the
// file it came from is named in the comment above the entry. A number typed here
// from memory is a support ticket waiting to happen: the page would keep saying
// the old rule over the new behaviour and nothing would ever contradict it.
//
// Versions are the ones package.json actually shipped
// (git log --date=short -- admin-backend/portal-nextgen/package.json). Lines are
// player-facing only: what a player can see or do, never how it was built.

// The keys GET /portal/features answers with. An entry may name ONE of these;
// naming anything else is a typo that would hide the entry forever, so the suite
// pins every `feature` against this tuple.
export const FEATURE_KEYS = ['chat', 'refinery', 'storage_move', 'market_sell_backpack', 'augment', 'profile_login', 'game_login', 'reports'];

// Newest first. The page marks the entry whose version equals the running build.
export const WHATS_NEW = [
  {
    version: '2.21.1',
    date: '2026-09-10',
    title: 'More flexible password length',
    items: [
      { text: 'Passwords and passphrases now accept 8 to 128 characters when creating a profile or changing a saved password. Spaces and Unicode remain supported, and common passwords are still refused.', feature: 'profile_login' },
    ],
  },
  {
    version: '2.21.0',
    date: '2026-09-10',
    title: 'More ways to sign in',
    items: [
      { text: 'Save a password or passkey on your existing profile, then sign in without opening Discord. Existing linked accounts, characters, history and shared allowances stay together.', feature: 'profile_login' },
      { text: 'Settings now includes Sign-in and security: manage your saved methods, optionally connect or disconnect Discord, and sign out other browser sessions. Security changes require a recent sign-in.', feature: 'profile_login' },
      { text: 'New players can verify an online character through a private Cielago code and save their first password or passkey without Discord. Game verification does not recover or merge existing profiles.', feature: 'game_login' },
    ],
  },
  {
    version: '2.20.0',
    date: '2026-09-10',
    title: 'A public record of the Sietch',
    items: [
      { text: 'Reports brings the Daily Dispatch and Sunday Chronicle into a public archive. Open an edition without signing in, filter daily or weekly issues, and share its permanent link.', feature: 'reports' },
      { text: 'Each published edition preserves its reporting window and figures. Explore recorded activity, community highlights, the standing testing-station board and links to Events and the Exchange.', feature: 'reports' },
      { text: 'Report headings, body text and labels use the portal’s shared type scale for consistent reading on desktop and mobile.', feature: 'reports' },
      { text: 'Playtime is labelled as an estimate across all maps. Record ties are kept distinct from new highs, and unavailable data is not presented as an all-clear.', feature: 'reports' },
    ],
  },
  {
    version: '2.18.0',
    date: '2026-09-08',
    title: 'Server time and the next Coriolis reset',
    items: [
      { text: 'The shared header now shows Last Sietch time in Eastern time and the next scheduled Coriolis reset.' },
      { text: 'Home, Desert and Maps include the full reset date, time and countdown. Players in another time zone also see the reset in their own local time.' },
      { text: 'The clock synchronizes with the portal server. Unavailable or interrupted readings are labelled, and a scheduled reset is kept distinct from a local sandstorm or a confirmed completed reset.' },
    ],
  },
  {
    version: '2.17.1',
    date: '2026-09-08',
    title: 'Package details and BRT recovery',
    items: [
      { text: 'Mailbox opens recorded package contents by default, with readable item names and quantities grouped by backpack, CHOAM bank or Exchange destination.' },
      { text: 'Long item names wrap on phones. Waiting and pending items keep their own delivery status, and missing quantities are marked as not recorded.' },
      { text: 'Older deliveries without an item breakdown now say so. Their contents are not filled in from the current welcome-pack recipe.' },
      { text: 'In-game Base Reconstruction Tool backup and restore have been verified again on Habbanya after the September 8 server restart. Help now includes recovery checks and what to do if a restore does not appear.' },
      { text: 'Players confirmed Cielago private-message delivery and faction chat across Habbanya and Kulon after the restart, and reported working faction chat on Overland too.' },
    ],
  },
  {
    version: '2.17.0',
    date: '2026-09-07',
    title: 'Find your sietch, then get settled',
    items: [
      { text: 'The public Dune page puts joining instructions and a copy-server-name button first, with clear Habbanya, Kulon and Amtal choices before the live stats.' },
      { text: 'First session is a new public guide in Home and Search. It explains joining the game, linking Discord, finding the welcome package and meeting the community.' },
      { text: 'Linked players can see their recorded welcome-package status in the guide. An unavailable read is not treated as an empty history, and opening the guide does not claim a package or mark tasks complete.' },
      { text: 'The joining guide explains the Custom Character warning, Exchange items labelled CANCELED, and the parts of the welcome package that arrive after logout. Deep Desert copy now names both PvE and PvP choices.' },
    ],
  },
  {
    version: '2.16.1',
    date: '2026-09-07',
    title: 'More dust from your spice',
    items: [
      { text: 'Refinery inventory tables now scroll inside their panels on phones instead of widening the page.' },
      { text: 'Every Ingot Refinery batch now turns 25 matching ingots into 10 Spice-infused dust. It takes 1 Melange for Copper, 2 for Iron, 3 for Steel, 4 for Aluminum, 5 for Duraluminum and 6 for Plastanium.', feature: 'refinery' },
      { text: 'A new rates table in Storage > Workshop shows exactly what each batch spends and produces, using the current published recipes.', feature: 'refinery' },
      { text: 'Melange market pricing is unchanged. The 250-dust allowance per tier over a rolling seven days is unchanged and still shared across linked accounts. Previous use still counts; refining now happens in batches of 10 dust.', feature: 'refinery' },
    ],
  },
  {
    version: '2.16.0',
    date: '2026-09-07',
    title: 'Fremkit: your desert, within reach',
    items: [
      { text: 'A labelled navigation rail, larger text, carved surfaces and distinct artwork for Habbanya, Kulon and Amtal. Phones keep a compact dashboard and use still images by default.' },
      { text: 'The Reach searches pages, maps, items and your guild. Press / or use Search. Pin destinations for later; pinned shortcuts and saved Karum searches follow your account.' },
      { text: 'Home brings rewards and pending deliveries forward. World pulse lives in Desert, and full package details remain in Mailbox.' },
      { text: 'Karum item details compare quantities, exact grades, total prices and per-unit prices. Open the Exchange for its price ladder or set an Exchange price watch.' },
      { text: 'Activity brings sales, rewards, packages and fired price alerts together for your selected account. A partial read is labelled, and your previous visit is kept when a source is unavailable.' },
      { text: 'The Ingot Refinery is open in Storage > Workshop. Refine while logged out using your bank, backpack and toolbar; the dust goes to your bank. Each tier has a rolling seven-day allowance shared by your linked accounts.', feature: 'refinery' },
      { text: 'Moves between base storage containers remain paused until changes can be reflected reliably in the active game world. Use the game to move those items for now.' },
    ],
  },
  {
    version: '2.15.0',
    date: '2026-09-06',
    title: 'The portal has a new address',
    items: [
      { text: 'The companion now lives at portal.lastsietch.com. Every old address under lastsietch.com/portal, including the V2 address and every deep link, forwards to the new one.' },
      { text: 'You will need to connect Discord once more on the new address. Nothing else about your account changes.' },
      { text: 'If the companion is installed on your phone or desktop as an app, remove it and install it again from portal.lastsietch.com so it opens at the new address.' },
      { text: 'The release notes on this page are now the Server and Portal Changelog: a fixed box you can scroll, which will list server-side changes as well as portal releases from now on.' },
    ],
  },
  {
    version: '2.14.2',
    date: '2026-09-06',
    title: 'List rows keep their names',
    items: [
      { text: 'In the Storage list view, rows inside the narrow bank and backpack panels no longer lose the item name behind the action buttons; the actions drop under the name instead.' },
    ],
  },
  {
    version: '2.14.1',
    date: '2026-09-06',
    title: 'Help moves beside the gear',
    items: [
      { text: 'Help is now the question mark beside the gear at the top of every page, instead of a Home menu entry. The footer link and the version number still open it.' },
    ],
  },
  {
    version: '2.14.0',
    date: '2026-09-05',
    title: 'Deliveries and Help',
    items: [
      { text: 'Deliveries: the packages the server sent you, what was in each one, and where every part landed. A summary on Home, the full list on the Mailbox page.' },
      { text: 'This page. What changed in each release, and how the portal works.' },
    ],
  },
  {
    version: '2.13.0',
    date: '2026-09-05',
    title: 'Chat links, slash commands and Karum labels',
    items: [
      { text: 'Links to lastsietch.com, Discord, YouTube and the two Dune Awakening wikis stay clickable in chat. Any other host shows as its name instead.', feature: 'chat' },
      { text: 'Slash commands in chat, with /help to list them.', feature: 'chat' },
      { text: 'Row menus in chat, on storage tiles and on Exchange listings close when you click away from them.' },
      { text: 'Switching boards on the Karum no longer shows a wanted order as a sale card while it loads.' },
      { text: 'Schematics are labelled as schematics in the Karum picker and on both boards, and a search carrying the word schematic narrows to them.' },
    ],
  },
  {
    version: '2.12.0',
    date: '2026-09-04',
    title: 'Sietch chat',
    items: [
      { text: 'Chat rooms in the portal: the sietch, your guild, your house and every map.', feature: 'chat' },
    ],
  },
  {
    version: '2.11.0',
    date: '2026-09-04',
    title: 'Storage views and saved layouts',
    items: [
      { text: 'Storage has a Grid view and a List view, and your pick follows you to any browser you sign in from.' },
      { text: 'Settings gained a Layout panel: your storage view, and the map layers each of your characters has saved.' },
      { text: 'Map layers and the Board or Holo table choice save per character.' },
    ],
  },
  {
    version: '2.10.0',
    date: '2026-09-04',
    title: 'The Workshop, and the Rules of the Sietch',
    items: [
      { text: 'Storage gained a Workshop page: reroll and swap the augments on your gear while you are logged out.' },
      { text: 'The Ingot Refinery: hand base ingots and Spice Melange to CHOAM and take back Spice-infused dust.', feature: 'refinery' },
      { text: 'All seven Rules of the Sietch are on the Server Rules page, the same seven pinned in the Discord.' },
    ],
  },
  {
    version: '2.8.1',
    date: '2026-09-04',
    title: 'Vehicles on the boards',
    items: [
      { text: 'Vehicles carry their own icons on the maps, and the feed names the subtype.' },
    ],
  },
  {
    version: '2.7.0',
    date: '2026-09-03',
    title: 'Events, and a page per guild',
    items: [
      { text: 'An Events page under Sietch: what is coming up, what has just been, and a reminder you can set for yourself.' },
      { text: 'Every guild has its own page, reached from the Signal Board.' },
    ],
  },
  {
    version: '2.6.0',
    date: '2026-09-03',
    title: 'The new Home',
    items: [
      { text: 'Home answers first: one line for where you stand, then the instruments row with your wallet, the storm, your orders and the Landsraad.' },
    ],
  },
  {
    version: '2.5.0',
    date: '2026-09-03',
    title: 'Earlier releases, 2.1 to 2.5',
    items: [
      { text: 'The nav became five labelled groups, Settings got a page of its own, and a phone got its own tab bar.' },
      { text: 'Identity codes: eight characters another player can use to find you without knowing your character name.' },
      { text: 'The Desert hub, the Economy hub and the Server Rules page.' },
      { text: 'Solido base listings got their own page, with a preview and a share link.' },
      { text: 'Item transfers from your bank and Solari gifts can address a player by identity code.' },
    ],
  },
];

export const HOW_TO = [
  {
    // reports/+page.svelte, ReportArticle.svelte and routers/portal_reports.py.
    id: 'reports',
    title: 'Read and share a Sietch report',
    since: '2.20.0',
    feature: 'reports',
    body: [
      'Open Reports in the Sietch navigation group. Choose a Daily Dispatch or Sunday Chronicle, then open a dated edition for its full figures and recorded-activity chart. No sign-in is needed.',
      'The Daily Dispatch covers the day ending at 9:00 AM Eastern. The Sunday Chronicle covers the week ending Sunday at 6:00 PM Eastern. The reporting window appears at the top of each edition.',
      'In Discord, Events and Exchange open those portal pages. Full Report opens the exact dated edition. The archive starts with editions published after Reports launches.',
      'Copy report link shares that exact edition. Published reports preserve their original reporting window and figures. About these figures explains estimates, missing observations and the scope of recorded activity.',
    ],
  },
  {
    // portal_credentials.py, routers/portal_signin.py and SecurityPanel.svelte.
    id: 'sign-in-security',
    title: 'Passwords, passkeys and recovery',
    since: '2.21.0',
    feature: 'profile_login',
    body: [
      'Already linked through Discord? Sign in with that same Discord account, then open Settings > Sign-in and security. Add a password or passkey to the existing profile. Your linked accounts, characters, history, identity code and allowances stay together.',
      'Choose a portal username of 3 to 32 letters, numbers, dots, underscores or hyphens, starting with a letter or number. Passwords use 8 to 128 characters; spaces, Unicode and password-manager paste are supported. Common passwords are refused. No email is required.',
      'A passkey uses your device lock, fingerprint, face or security key. You can save up to eight passkeys and give each a label. Keep another saved method you can reach if a device is lost.',
      'Confirm a saved sign-in method before changing credentials, connecting or disconnecting Discord, unlinking a game account or ending other sessions. Confirmation lasts ten minutes. Adding a passkey or changing a password signs out other sessions.',
      'Discord is optional once another method is saved. Disconnecting it stops Discord delivery and Discord-based staff access while preserving your portal history and shared allowances. You cannot remove your last sign-in method.',
      'If you lose access, try another saved method. A game code cannot recover an existing profile or unlock its other accounts. Contact an admin if no saved method works; profiles are not merged automatically. Never share a password or verification code.',
    ],
  },
  {
    // portal_signin.game_action, portal_credentials game tickets and GameVerification.svelte.
    id: 'game-verification',
    title: 'Create a profile with a game code',
    since: '2.21.0',
    feature: 'game_login',
    body: [
      'Open Sign in and choose New here? Verify in game. Stay in game and enter your exact online character name. Cielago sends a private six-digit code to that character; enter it in the same browser within five minutes. Five incorrect guesses end the attempt, and repeated requests are limited.',
      'After verification, choose a portal username and save your first password or passkey within ten minutes. This finishes creating the profile. You can add more sign-in methods later in Settings.',
      'An account already linked to the portal must use its existing profile and saved sign-in method. To link another game account, sign in again, then use the game verification control in Settings. Game verification never combines profiles or resets shared allowances.',
      'If no private message arrives, check that the character is online and the exact name is correct. Start again when chat is available, or use Discord. Never post a verification code in community chat.',
    ],
  },
  {
    // ServerSchedule.svelte, server-time.svelte.js and GET /portal/server/time.
    id: 'server-time',
    title: 'Server time and Coriolis resets',
    since: '2.18.0',
    body: [
      'Last Sietch time is Eastern time. The clock shows EDT or EST as appropriate, including daylight-saving changes, and synchronizes with the portal server rather than relying on your device clock.',
      'The shared header gives quick access to the next scheduled Coriolis reset. Home, Desert and Maps show the full date, time and countdown, plus your local reset time when your time zone differs.',
      'This is the Deep Desert cycle reset, not a local sandstorm forecast. The schedule follows the current configured cycle; reaching its time does not confirm that the reset has completed. Server announcements may change the plan.',
      'If time or schedule synchronization is unavailable, the portal says so. An interrupted connection keeps the last synchronized reading with a warning until it can refresh.',
    ],
  },
  {
    // docs/dune-research/BRT-RECOVERY-2026-09-08.md: player-confirmed recovery test.
    id: 'brt-recovery',
    title: 'Base Reconstruction Tool recovery',
    since: '2.17.1',
    body: [
      'The in-game Base Reconstruction Tool completed a backup and restore test on Habbanya after the September 8 server restart. The structure, door, claim console and ownership were confirmed restored.',
      'Before moving a valuable base after this recovery, try a small disposable base and check both backup and restore. Make sure you have a free backup slot, and confirm the restored base is visible and usable before continuing.',
      'Older stored backups, container contents and Deep Desert use were not covered by this test. If a restore reports success but no base appears, stop retrying and contact server staff before recycling the backup. Keep the approximate time, map and exact error message.',
    ],
  },
  {
    // src/routes/start/+page.svelte and website/dune/index.html: first-session paths.
    id: 'first-session',
    title: 'Your first session',
    since: '2.17.0',
    body: [
      'Open First session from Home or Search. The guide is public, so you can read the joining instructions before connecting Discord. Joining the game and linking the portal are separate steps.',
      'In the PC game, open the Experimental tab, choose North America and search Last Sietch. Expand the row and choose your sietch. The public Dune page includes a copy-name button and screenshots.',
      'Once linked, the guide reads the selected account\'s welcome-package record. It does not grant or claim anything, and it does not infer completed actions from pages you opened. Mailbox keeps the full delivery details.',
    ],
  },
  {
    // src/lib/reach/Reach.svelte: search, keyboard navigation and saved pins.
    id: 'reach-search',
    title: 'Search and pinned shortcuts',
    since: '2.16.0',
    body: [
      'Use Search at the top of the portal, or press / when you are not typing in another field. Search pages and maps, plus items and your guild when signed in.',
      'Use the arrow keys and Enter to open a result. Search takes you to the relevant page so you can review any action there.',
      'Open Search with an empty field to see pinned shortcuts and Recent. Use the pin beside a destination to keep it close. Signed-in shortcuts follow your account.',
    ],
  },
  {
    // src/routes/activity/+page.svelte: source status, filters and visit markers.
    id: 'activity-history',
    title: 'Since your last visit',
    since: '2.16.0',
    body: [
      'Open Activity from Home to see recorded sales, rewards, deliveries and price alerts for your selected account. Filter by activity type, or show older entries by switching off Since last visit.',
      'A partial history is labelled. If a source could not be read or the history is truncated, your previous visit is kept so missing entries are not treated as viewed.',
      'Opening Activity does not clear your price-alert badge. Follow an entry to its page for details or the next action.',
    ],
  },
  {
    // src/lib/components/karum/KarumDetail.svelte and KarumBoard.svelte.
    id: 'karum-comparison',
    title: 'Compare items and save a search',
    since: '2.16.0',
    body: [
      'On a Karum listing, open Details and comparison to check the exact grade, stack quantity, total price and price per unit. Each comparison row is a separate stack.',
      'Review purchase opens the usual confirmation. Closing details keeps your board filters. Save search keeps your item filter, trade direction, sort and wanted-grade choice for later.',
      'Compare on the Exchange opens that item in the CHOAM price ladder. An Exchange price watch tracks that market by item; it does not watch Karum listings or a specific grade.',
    ],
  },
  {
    // routers/portal.py: storage-move gate; owner hold reaffirmed 2026-09-06.
    id: 'storage-move-hold',
    title: 'Why storage moves are paused',
    since: '2.16.0',
    body: [
      'Moves between base storage containers are temporarily disabled. The portal cannot yet guarantee that those moves will appear correctly while the game world is active.',
      'Logging out alone does not make these moves available. Use the game to move items between your storage boxes for now.',
      'The Ingot Refinery is a separate service using your personal bank, backpack and toolbar while you are logged out. It does not take inputs from base storage boxes.',
    ],
  },
  {
    // src/lib/chat.svelte.js COMMANDS (usage and help verbatim), WHISPER_SUBJECT,
    // PRICE_RUNGS and LOCAL_RE; admin-backend/portal_chat_commands.py for the
    // roll bounds (ROLL_MAX_DICE 10, ROLL_MIN_SIDES 2, ROLL_MAX_SIDES 1000) and
    // for the rule that the SERVER rolls, never the browser.
    id: 'chat-commands',
    title: 'Chat commands',
    feature: 'chat',
    since: '2.12.0',
    body: [
      'A message that starts with a slash is a command. Type /help in any room to have the list printed straight back to you.',
      'The dice are rolled on the server, so nobody can pick their own number. You may roll 1 to 10 dice with 2 to 1000 sides each.',
      'A whisper is a mailbox message, not a room message. It reaches the player whether or not they are reading chat, and it arrives under the subject "Whisper from chat".',
      '/price answers with the three cheapest rungs of the ladder for that item, and only you see the answer.',
    ],
    steps: [
      '/me <action>  Say what you are doing.',
      '/roll [NdM]  Roll dice. 1d20 by default.',
      '/w <name> <message>  Whisper one player through the mailbox.',
      '/price <item>  The cheapest live listings for an item.',
      '/help  List these commands.',
    ],
  },
  {
    // src/routes/storage/+page.svelte (the Grid / List toggle writes the
    // storage_view preference), src/lib/components/settings/LayoutPanel.svelte
    // (the same switch under Settings), src/lib/prefs.svelte.js header (the
    // server owns one document per scope, so the choice is not browser-local).
    id: 'storage-view',
    title: 'Grid or list in Storage',
    since: '2.11.0',
    body: [
      'Storage draws your containers as a grid of tiles or as a list of rows. The switch sits in the page header, beside the Workshop link.',
      'The pick is saved on your account rather than in the browser, so it follows you to any browser you sign in from. The same switch lives under Settings, in the Layout panel, with everything else that changes how a page is arranged.',
    ],
  },
  {
    // scripts/refinery-rates.json (input_per_batch 25, spice_per_batch 1 to 6 by
    // tier, output_per_batch 10, weekly_dust_cap 250, window_days 7,
    // max_batches_per_request 50, six tiers);
    // src/lib/components/refinery/RefineryPanel.svelte (where the inputs come
    // from and where the dust lands, the per-tier allowance);
    // src/lib/components/refinery/RefineryRow.svelte (the week counter, the
    // confirm, and that the result is not visible in game until you relog).
    id: 'ingot-refinery',
    title: 'The Ingot Refinery',
    feature: 'refinery',
    since: '2.10.0',
    body: [
      'CHOAM will turn base refined ingots into the matching Spice-infused metal dust while you are logged out. It is a service, not a bench recipe: nothing in the game turns an ingot into a dust.',
      'One batch takes 25 ingots of that tier and returns 10 dust. Melange per batch is 1 for Copper, 2 for Iron, 3 for Steel, 4 for Aluminum, 5 for Duraluminum and 6 for Plastanium. The rates table in the Workshop reads the current published recipes.',
      'Inputs are drawn from your CHOAM bank, your backpack and your toolbar together; the dust always lands in your bank.',
      'Each of the six tiers has its own allowance of 250 dust over a rolling seven days, shared by all game accounts linked to your portal profile. Spending one tier leaves the other five untouched. A full allowance covers 25 batches at the current rate.',
      'Previous use still counts after a rate change. If fewer than 10 dust remain in a tier, wait until older use leaves the rolling seven-day window before refining another whole batch. Market pricing for Melange is unchanged.',
      'The trade is confirmed before it runs and cannot be undone, and the result does not appear in game until your next login.',
      'Check the rate shown in Workshop before confirming. If you are buying the inputs, compare their cost with buying the dust directly on the Exchange.',
    ],
    steps: [
      'Open Storage, then Workshop, then the Refinery tab.',
      'Pick a tier and set how many batches you want.',
      'Tick the confirm and press through. The dust is waiting in your bank the next time you log in.',
    ],
  },
  {
    // src/routes/karum/+page.svelte (the page sub), KarumSellDialog.svelte (one
    // bank or backpack stack at a fixed ask), KarumRequestDialog.svelte and
    // src/lib/components/karum/grade.js (Any or Base to G5, exact or better).
    id: 'karum',
    title: 'The Karum',
    since: '2.1.0',
    body: [
      'The Karum is a merchant quarter outside the CHOAM economy. You list what you have or post what you need, and the trade waits for another player. Nothing expires, and the CHOAM bot cannot take it.',
      'A sale is one stack out of your bank or your backpack at a fixed ask. A wanted order is the other direction: you name the item, the quantity and the price you will pay, and another player fills it.',
      'For an item that has grades you can ask for any grade, or for one grade exactly, or for that grade and better. Grades run from Base to G5.',
    ],
  },
  {
    // src/routes/exchange/+page.svelte (the page sub);
    // src/lib/components/storage/SellDialog.svelte and
    // src/routes/storage/+page.svelte (allowMarketSell: the bank grid is
    // unconditional, the backpack grid rides storage.marketBackpackEnabled).
    id: 'exchange',
    title: 'The CHOAM Exchange',
    since: '2.1.0',
    body: [
      'The Exchange page carries every open listing on the sietch market, the price ladder behind each item, and the flips where a player ask sits under what CHOAM will pay. You can watch a price, buy off the ladder, and manage your own listings.',
      'Listing goes through Storage. Open the item in your CHOAM bank, choose Sell, and set the price and the run: 1, 3, 7 or 14 days. The listing fee comes out of your bank.',
    ],
  },
  {
    // src/lib/components/storage/SellDialog.svelte + routes/storage/+page.svelte
    // (allowMarketSell on the backpack grid is gated on marketBackpackEnabled).
    id: 'exchange-from-backpack',
    title: 'Listing from your backpack',
    feature: 'market_sell_backpack',
    since: '2.1.0',
    body: [
      'Selling is normally offered from your CHOAM bank alone. While this is open you can also list a tradeable stack straight out of your character backpack, on the same form and with the same fee.',
      'Both are pawn-side stores that rebuild from the database when you log in. Base containers and vehicles are never offered, because their contents live in the running world instead.',
    ],
  },
  {
    // src/lib/components/storage/TransferDialog.svelte (bank items, recipient by
    // character name or identity code, the offline gate, the daily and per-player
    // counters) and src/lib/components/guild/GiftDialog.svelte
    // (GIFTS_PER_PAIR_PER_DAY 5, GIFTS_PER_DAY 20, the per-gift ceiling shown in
    // the dialog itself).
    id: 'transfers-and-gifts',
    title: 'Sending items and Solari',
    since: '2.1.0',
    body: [
      'An item in your CHOAM bank can be sent to another player. Open it in Storage and choose Send from bank. You address the recipient by character name or by identity code, and the server resolves who that is.',
      'A send takes the item out of your bank, so it is offered only while you are logged out of the game. The dialog counts your sends down: how many you have left today, and how many of those may go to this one player.',
      'Solari go the same way through Send Solari, to another of your own accounts, to somebody in your guild, or to an identity code. The caps are 5 gifts to the same player per day and 20 gifts a day in total, with a ceiling on any one gift that the dialog prints for you.',
    ],
  },
  {
    // src/routes/rewards/+page.svelte (the page sub and the delivery note),
    // DailyCalendar.svelte (cycleLen 7, the real 28-day dated calendar),
    // WeeklyTrack.svelte (unlocks at a 7-day streak, rotating tier-six weapon,
    // rotates at the week boundary), MonthlyTrack.svelte (N distinct login days
    // inside a fixed 28-day period).
    id: 'daily-rewards',
    title: 'Daily, weekly and monthly rewards',
    since: '2.1.0',
    body: [
      'Log in each day to build a streak and claim your Solari. The daily ramp runs on a seven day cycle, and the calendar shows the real dates of the current 28 day period so you can see which days you actually logged in.',
      'A seven day streak unlocks the weekly reward, a tier-six weapon that rotates at the week boundary.',
      'Logging in on enough separate days inside the 28 day period unlocks the monthly reward, a pre-augmented grade-five weapon. A missed day costs you the streak, but it does not reset that day count.',
      'Item rewards are delivered to your CHOAM bank when you redeem. Collect them in game at any bank terminal.',
    ],
  },
  {
    // src/lib/components/map/LayerTree.svelte (the category tree, the All / None
    // / Default picks, and that a linked player's preset saves under map_hidden
    // in the selected character's scope), src/routes/maps/[key]/+page.svelte
    // (the viewer choice saved under map_viewer), LayoutPanel.svelte
    // (VIEWER_LABEL: carved = Board, holo = Holo table).
    id: 'maps',
    title: 'Map layers and the holo table',
    since: '2.1.0',
    body: [
      'Every board carries a layer tree grouped by category. Filter it by name, turn a category or a single type on and off, and use All, None or Default. Default means the board as it first opened, not everything on.',
      'A board draws either as the carved Board or as the Holo table. The holo view needs a browser that can run WebGL2 and quietly stays on the Board where it cannot.',
      'Once you are linked, your layers and your viewer choice save against the character you have selected, so each character can open the same board its own way, on any browser. Settings, Layout lists what each character has saved and lets you clear a board back to its defaults.',
    ],
  },
  {
    // admin-backend/portal_quiz.py (three questions, every one answerable from
    // what the player can read in game: char_level, current_map,
    // faction_alignment, then pledged_house and character_name),
    // admin-backend/routers/portal_link.py and config.py
    // (PORTAL_RATE_COOLDOWN_HOURS 24 after three failures),
    // src/lib/components/settings/IdentityCodePanel.svelte (eight symbols from a
    // 32-symbol alphabet with no 0, 1, I or O; one per Discord identity;
    // rotatable; never a link).
    id: 'linking',
    title: 'Linking through Discord',
    since: '2.2.0',
    body: [
      'Connect Discord, then prove the character is yours. The portal asks three questions about it that only somebody looking at it in game can answer: what level it is, what map it is standing on, which faction it leans to, and if those cannot be read, which house it has pledged to or what it is called.',
      'Three wrong attempts put that pairing on a 24 hour cooldown, so take the questions to the game and come back.',
      'Once you are linked you carry an identity code: eight characters drawn from an alphabet with no 0, 1, I or O in it, so it survives being read aloud. It is one code per portal profile and you can rotate it whenever you like, which retires the old one. Another player uses it to send you items or Solari without ever knowing your character name. It is never a link, so nothing you scan or paste can send you anywhere.',
    ],
  },
  {
    // src/routes/mailbox/+page.svelte and
    // src/lib/components/guild/Mailbox.svelte (inbox, composer, the officer's
    // guild inbox as a second tab, reads reconcile the topbar bell);
    // src/lib/chat.svelte.js WHISPER_SUBJECT for the whisper line.
    id: 'mailbox',
    title: 'The mailbox and whispers',
    since: '2.1.0',
    body: [
      'The bell in the top bar carries your unread count and opens the Mailbox. Inside are your own messages, a composer for sending one, and, if you are an officer, your guild inbox as a second tab.',
      'A chat whisper lands here too, under the subject "Whisper from chat", so it reaches you whether or not you had the room open.',
      'The player directory on the same page is how you find somebody to write to. Whether you appear in it yourself is a switch under Settings.',
    ],
  },
  {
    // docs/dune-research/v3-portal/WAVE12-DELIVERIES-HELP-PLAN-2026-09-05.md
    // sections 1a and 1c (the two packages, the legs and where each one lands);
    // ops/return-package/README.md (28 days away, tier by character level,
    // resources to the bank and three tools to the backpack, repeats no closer
    // than 90 days); scripts/2026-06-11-welcome-pack-identity-cooldown.sql and
    // ops/welcome-pack-reroll-heal/staging/watcher.sh
    // (WELCOME_PACK_COOLDOWN_DAYS 30, one pack per identity per window).
    id: 'deliveries',
    title: 'Welcome and Return Packages',
    since: '2.14.0',
    body: [
      'The server sends two packages of its own. A Welcome Package goes to a new account, and a Return Package goes to a player who has been away 28 days or more, sized by the level of the character that comes back.',
      'The parts land in different places. Gear goes into your backpack. Anything that would not fit is listed for you on the CHOAM Exchange, where it waits under the Completed tab reading CANCELED: press Take item at any tradepost terminal to collect it. Research, House Scrip and Intel are applied the next time you log out and back in. A Return Package puts its resources in your CHOAM bank and its three tools in your backpack, or in the bank as well if your backpack is full.',
      'Deliveries on the Mailbox page shows you every package the server sent you, which parts have landed, and what is still on its way. Home carries the short version.',
      'What was in it starts open and groups recorded item names and quantities by destination. You can collapse it. These are package records, not a view of your current inventory; a waiting or pending line has not been confirmed delivered. Up to 40 recorded lines are shown, with a count when more exist.',
      'Some older deliveries have only a package-level record. If their item names or quantities were not recorded, Mailbox says that detail is unavailable rather than substituting the contents of today\'s welcome package.',
      'Two rules are worth knowing. A fresh start inside 30 days of your last one does not earn a second Welcome Package, and the page tells you the date you become eligible again. A Return Package can come more than once, but never closer together than 90 days.',
    ],
  },
];

/** Entries a viewer may see, given the features map from GET /portal/features.
 *
 *  An entry with NO `feature` key always passes: it describes something that is
 *  simply there. An entry WITH one passes only on an explicit true, so a read
 *  that is still in flight, that failed, or that answered with a shape we did not
 *  expect leaves the entry hidden. That direction is deliberate: a how-to for a
 *  door that is not open yet sends the player looking for a button nobody has,
 *  which is worse than a page that is briefly one entry short.
 */
export function visible(entries, features) {
  if (!Array.isArray(entries)) return [];
  return entries.filter((entry) => {
    const key = entry && entry.feature;
    if (!key) return true;
    return !!features && features[key] === true;
  });
}
