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
export const FEATURE_KEYS = ['chat', 'refinery', 'storage_move', 'market_sell_backpack', 'augment', 'profile_login', 'game_login', 'reports', 'sietch'];

// Newest first. The page marks the entry whose version equals the running build.
export const WHATS_NEW = [
  {
    version: '2.28.25',
    date: '2026-10-30',
    title: 'New gear in the Token Stall',
    items: [
      { text: 'Card art for the new Token Stall gear: the Perforator, the Experimental Lightning Gun, the Tactical Radiation Suit, the Tabr Softstep Boots and The Forge.' },
    ],
  },
  {
    version: '2.28.24',
    date: '2026-10-30',
    title: 'Groundwork for a new Home',
    items: [
      { text: 'Behind the scenes: groundwork for a redesigned Home page. Nothing changes for you yet.' },
    ],
  },
  {
    version: '2.28.23',
    date: '2026-10-29',
    title: 'Amtal wording',
    items: [
      { text: 'Amtal page and rules: clearer wording on keeping your generators fueled and on exploits.' },
    ],
  },
  {
    version: '2.28.22',
    date: '2026-10-29',
    title: 'Weekend events',
    items: [
      { text: 'Home and the Events page show This weekend and Next weekend: which event runs, what it does, and when it starts and ends in Eastern time and your own.' },
      { text: 'Event cards show what each bonus does and whether it is live now.' },
      { text: 'Help has a new guide to the weekend event rotation, starting Friday, November 6 with Double XP.' },
      { text: 'Seven Nights titles: wear a title you earned beside your name on Standings, in chat and on the Seven Nights board. Pick it in Settings, under Appearance.' },
    ],
  },
  {
    version: '2.28.21',
    date: '2026-10-29',
    title: 'Rescue, rules, and Help for selling',
    items: [
      { text: 'Help has a new guide to selling: the CHOAM Exchange or the Karum, selling to the CHOAM bot, the bot price, the SOLD label and Take item. The Exchange and Karum pages link to it when a list is empty.' },
      { text: 'Help: how to use the higher Solido Replicator limits.' },
      { text: 'If you are signed out, pages such as the Karum and Rewards now ask you to sign in with a linked character, instead of saying it is to use storage.' },
      { text: 'Self-rescue is back: if you\'re stuck on Habbanya, use Rescue on the portal to return to your nearest base (once an hour).' },
      { text: 'Rescue also works in the Deep Desert PvE instance, where it takes you to your nearest base or to an ornithopter pilot. Rescue now shows where you will land before you send it. It never works on PvP maps.' },
      { text: 'Rules: on Amtal, keep your generators fueled. Getting in through an exploit (glitching through walls, terrain or shields) is still banned.' },
      { text: 'Rules and Help: how inactive bases on Habbanya are handled. After 30 days without the owner playing, a notice and 14 days of grace, the base is saved to the owner\'s Base Vault, opened for a public salvage event, then demolished.' },
    ],
  },
  {
    version: '2.28.20',
    date: '2026-10-28',
    title: 'Seven Nights calendar descriptions',
    items: [
      { text: 'The Seven Nights calendar now shows a short description and the token payout under each night, so you can see which nights are Knock nights and which pay for something else, like Landsraad offerings.' },
    ],
  },
  {
    version: '2.28.19',
    date: '2026-10-28',
    title: 'Kulon has closed',
    items: [
      { text: 'Kulon closed on October 28 and its bases moved to Amtal at the same spots. Hagga Basin now has two sietches: Habbanya (PvE) and Amtal (full PvP).' },
      { text: 'Home, Maps, Desert, Search and the Sand Table offer Habbanya and Amtal. An old Kulon map link opens Habbanya. The Kulon chat room stays readable but takes no new messages.' },
    ],
  },
  {
    version: '2.28.18',
    date: '2026-10-28',
    title: 'Chat shows the date',
    items: [
      { text: 'Chat messages from before today now show their date next to the time. Hover a time to see the full date.', feature: 'chat' },
    ],
  },
  {
    version: '2.28.17',
    date: '2026-10-27',
    title: 'What\'s new is up to date',
    items: [
      { text: 'This page now lists the four Seven Nights updates from October 27, below.', feature: 'sietch' },
    ],
  },
  {
    version: '2.28.16',
    date: '2026-10-27',
    title: 'Seven Nights: every Landsraad turn-in counts',
    items: [
      { text: 'Each Landsraad turn-in earns 2 LS Tokens, up to two turn-ins a night. On the Landsraad night up to four count. Nights 1 and 2 were recounted this way.', feature: 'sietch' },
    ],
  },
  {
    version: '2.28.15',
    date: '2026-10-27',
    title: 'Token Stall: the Wayfarer\'s Writ',
    items: [
      { text: 'A new Stall good, the Wayfarer\'s Writ: +1,200 character XP for 15 LS Tokens, one per player. It opens on night 6 with the Training Writs and arrives when you next log out.', feature: 'sietch' },
    ],
  },
  {
    version: '2.28.14',
    date: '2026-10-27',
    title: 'Token Stall: new artwork',
    items: [
      { text: 'Three Token Stall goods have new artwork.', feature: 'sietch' },
    ],
  },
  {
    version: '2.28.13',
    date: '2026-10-27',
    title: 'Seven Nights rewards doubled',
    items: [
      { text: 'Every Seven Nights reward is doubled: 4 LS Tokens for playing each night, up to 6 more for Knocking, and a 20 token streak bonus. Everything earned before the change was doubled too.', feature: 'sietch' },
    ],
  },
  {
    version: '2.28.12',
    date: '2026-10-26',
    title: 'Reading font size: Small, Medium or Large',
    items: [
      { text: 'With the Easier reading font on, Settings, Display lets you pick Small, Medium or Large body text. Medium is the default; Large is the size it used before. Saved on this device and on your account.' },
    ],
  },
  {
    version: '2.28.11',
    date: '2026-10-26',
    title: 'Rules: trading channel name',
    items: [
      { text: 'The trading rule now points to the #karum channel, its current name on the Discord.' },
    ],
  },
  {
    version: '2.28.10',
    date: '2026-10-26',
    title: 'The full Rules of the Sietch',
    items: [
      { text: 'The Rules page now has the full code: zero tolerance offences, base rules for Habbanya and Amtal, trading, streaming, and how to appeal a decision.' },
    ],
  },
  {
    version: '2.28.9',
    date: '2026-10-26',
    title: 'Easier reading font, Storage on phones, steadier home cards',
    items: [
      { text: 'Settings, Display has an Easier reading font switch. It swaps body text to OpenDyslexic, a font some readers find easier. It is saved on this device and on your account.' },
      { text: 'Storage on a phone has an Open container picker at the top. Pick a box and the page jumps straight to what is inside it.' },
      { text: 'Opening a card on the home page no longer stretches the card beside it.' },
    ],
  },
  {
    version: '2.28.8',
    date: '2026-10-25',
    title: 'Events calendar and the active decree',
    items: [
      { text: 'Events has a Subscribe (calendar) button. Add it once to Google Calendar, Outlook or your phone and every published Last Sietch event shows up there, updates included.' },
      { text: 'The Landsraad card on the home page names the active decree. If the reading is more than a couple of minutes old, the card says when it was taken.' },
    ],
  },
  {
    version: '2.28.7',
    date: '2026-10-25',
    title: 'Seven Nights of the Sietch',
    items: [
      { text: 'While Seven Nights runs, the home page opens with tonight\'s night and a button straight to the event, and Seven Nights carries a Live tag in the Sietch menu.', feature: 'sietch' },
      { text: 'Amtal is part of Seven Nights. Knock at the doors on Habbanya, Kulon or Amtal: every Hagga Basin sietch counts the same, and the doors show on the Amtal map too.', feature: 'sietch' },
      { text: 'Seven Nights of the Sietch runs from Sunday, October 25 at 8 PM ET to Sunday, November 1 at 7 PM ET. Open Seven Nights under Sietch, Knock at the doors the desert names each night while you are in game, and earn LS Tokens.', feature: 'sietch' },
      { text: 'Your Hoard shows your LS Tokens, where each one came from, and the badges and map markers you own. The Token Stall opens Wednesday at 8 PM ET.', feature: 'sietch' },
      { text: 'Help has a new Seven Nights section that answers the common questions: who can earn, how Knocking works, the Token Stall, the Night of Costumes and where your goods arrive.', feature: 'sietch' },
    ],
  },
  {
    version: '2.27.12',
    date: '2026-10-24',
    title: 'Events that are running now',
    items: [
      { text: 'The Events page has a Happening now section for an event that has started and not yet ended, and the Home page shows it with the time it ends. A running event no longer appears under Past.' },
      { text: 'The installed app on iPhone now opens on the latest version instead of an older copy of the Home page.' },
    ],
  },
  {
    version: '2.27.11',
    date: '2026-10-23',
    title: 'Joining steps for the 1.5 menus',
    items: [
      { text: 'First session now follows the 1.5 game menus. A new character goes New Game, Multiplayer, then Server Browser in the Recommended Server popup. An existing character goes Characters, then Server Browser, and picks Transfer on Last Sietch. Both then use the Experimental tab and search Last Sietch.' },
    ],
  },
  {
    version: '2.27.10',
    date: '2026-10-21',
    title: 'A clearer sign-in for new players',
    items: [
      { text: 'The sign-in page now asks first: New to the portal, or I have been here before. New players get three steps to link a character with Discord: sign in, find your character by its in-game name, and answer three quick questions. Verify in game is right below for players without Discord.' },
      { text: 'Returning players still see Continue with Discord first, with passkey and password under Other ways to sign in. This browser remembers that you have signed in before and opens on that side.' },
    ],
  },
  {
    version: '2.27.9',
    date: '2026-10-20',
    title: 'Chat in the header, tidier Settings and sign-in',
    items: [
      { text: 'Chat is now a button in the header, next to Mail, on phone and desktop. It shows how many messages you have not read. The Sietch menu is shorter: Guilds, Events, Amtal, Reports, Landsraad and Standings.' },
      { text: 'Settings is sorted into five sections: Account and sign-in, Characters, Display, Privacy and data, and Transfers and activity. Your password, passkeys, Discord link and sessions are all on the first screen of Account and sign-in.' },
      { text: 'Linked accounts and Playing as are now one Characters card, with a Play as button on each account. Adding an alt or verifying in game now lives under one Add a game account button.' },
      { text: 'The sign-in page leads with Discord. Passkey, password and verify in game are under Other ways to sign in.' },
    ],
  },
  {
    version: '2.27.8',
    date: '2026-10-20',
    title: 'Amtal PvP board, kill-streak bounties and titles',
    items: [
      { text: 'The Amtal page now opens with a PvP board: this week\'s and this season\'s player kills, deaths, K/D and best streak, plus who is on a kill streak right now. Amtal is back in the Sietch menu.' },
      { text: 'String together 5 player kills on Amtal without dying and a 2,500 spice bounty goes on your head, growing every 5 kills. Whoever ends the streak collects it. Killing the same player again within 10 minutes does not count.' },
      { text: 'Four titles show next to names on the board: Sardaukar of the Week (most kills last week), Kanly (longest streak last week), Headtaker (collected a bounty) and Blooded (10 kills this season). Staff appear on the board but cannot hold a title.' },
    ],
  },
  {
    version: '2.27.4',
    date: '2026-10-13',
    title: 'House swatches claim from the portal too',
    items: [
      { text: 'House swatches can now be claimed from the Landsraad page along with the rest of your rewards. They unlock straight into your customization menu.' },
      { text: 'After a claim, the Landsraad page now shows your rewards as taken right away, and long reward names no longer get cut off.' },
    ],
  },
  {
    version: '2.27.1',
    date: '2026-10-12',
    title: 'Claim Landsraad rewards from the portal',
    items: [
      { text: 'Your Landsraad house rewards can now be claimed from the Landsraad page, without visiting each house representative. Log out of the game first and wait a few minutes, then press Review claim.' },
      { text: 'Everything lands in your backpack. If it runs out of room, the rest waits at the CHOAM Exchange: open Completed and press Take item. Cielago sends you a receipt on Discord when you are next online.' },
      { text: 'House swatches still have to be claimed at the representative in game for now.' },
    ],
  },
  {
    version: '2.26.4',
    date: '2026-10-12',
    title: 'Hagga storms on the Sand Table',
    items: [
      { text: 'The Sand Table now shows live storms over Hagga Basin as well as the Deep Desert.' },
      { text: 'When more than one sietch or Deep Desert world is storming, every storm shows on its stone at once, each blowing its own way.' },
    ],
  },
  {
    version: '2.26.3',
    date: '2026-10-09',
    title: 'Storms show their real footprint',
    items: [
      { text: 'Storms on the War-Tables now show their real footprint and path, with a countdown to when they clear.' },
      { text: 'Hagga Basin storms show too, on the sietch where they blow: smaller than Deep Desert storms, and they cross the basin in a few minutes. Hover a storm to see when it clears.' },
    ],
  },
  {
    version: '2.26.2',
    date: '2026-10-09',
    title: 'Amtal rates from the poll',
    items: [
      { text: 'Amtal now runs 1.5x stock harvest, as voted in the Amtal resource poll. With Funcom\'s 2.5x for PvP zones, which covers all of Amtal outside Riftwatch and Helius Gate, that is 3.75x almost anywhere you harvest.' },
    ],
  },
  {
    version: '2.26.1',
    date: '2026-10-07',
    title: 'Spice blows and steadier maps',
    items: [
      { text: 'Spice fields show on the 3D Deep Desert War-Table. Every field is a bubbling purple patch, an erupting medium field throws up a small spice blow, and the active large field the full violet blow, as in game.' },
      { text: 'Medium spice counts and fields follow the instance you pick. PvE and PvP no longer add up together.' },
      { text: 'Map icons, counts and sector labels stay on the ground when you tilt, turn or pan the War-Table.' },
      { text: 'Sandstorms only show when the storm is actually tracked. The guessed sweep after a storm was due is gone. While a storm is up, the card shows where it is and when it clears.' },
      { text: 'Sandworms are easier to spot on the War-Table.' },
    ],
  },
  {
    version: '2.26.0',
    date: '2026-10-06',
    title: 'Real terrain and the Sand Table',
    items: [
      { text: 'Every War-Table now shows real terrain built from the game\'s own data: Hagga Basin\'s landscape and rock formations, the Deep Desert\'s current layout, and the Arrakeen and Harko Village dioramas, with the map art laid over it.' },
      { text: 'Turn the War-Table to any angle: right-drag, Q and E, or a two-finger twist. The compass N puts north back up.' },
      { text: 'Map markers use the game\'s own icons. Dense ore, salvage and camp layers group into counted icons that split as you zoom in.' },
      { text: 'Light follows your local time of day: dawn, day, dusk and night. Pick one yourself with the light switch, and it carries across every map.' },
      { text: 'The new Sand Table shows all of Arrakis as a carved relief in a sietch chamber, with the game\'s own miniature for every location. Stones glow when a map is online. Click one to descend into its War-Table, or use the arrow keys and Enter. Phones and reduced motion get a flat Guild chart instead.' },
    ],
  },
  {
    version: '2.25.2',
    date: '2026-10-06',
    title: 'Notes tidy-up',
    items: [
      { text: 'The 2.25.0 note no longer quotes the old Amtal rates. Current rates are in 2.25.1 and on the Amtal page.' },
    ],
  },
  {
    version: '2.25.1',
    date: '2026-10-06',
    title: 'Per-sietch rates',
    items: [
      { text: 'Harvest rates are now set per sietch and quoted against stock: Habbanya 1.5x, Kulon 1.75x, Amtal 2.5x, Deep Desert 1.5x PvE and 2.5x PvP. PvP zones add Funcom\'s 2.5x on top. The Amtal page shows the current Amtal numbers, provisional until the poll closes.' },
    ],
  },
  {
    version: '2.25.0',
    date: '2026-10-06',
    title: 'Amtal update',
    items: [
      { text: 'The Amtal page has a new How Amtal plays section: a PvP kill drops the loose loot in your backpack, and safe arrival at your own Amtal base or your faction\'s safe area. Amtal\'s harvest rate and backpack size are on the Amtal page (see 2.25.1).' },
    ],
  },
  {
    version: '2.24.1',
    date: '2026-09-30',
    title: 'Standings update hourly',
    items: [
      { text: 'Standings now refresh once an hour and show level only, not exact experience.' },
    ],
  },
  {
    version: '2.24.0',
    date: '2026-09-30',
    title: 'Live players need a sign-in',
    items: [
      { text: 'Other players and vehicles on the live maps now show only when you are signed in with Discord.' },
    ],
  },
  {
    version: '2.23.2',
    date: '2026-09-30',
    title: 'Clearer wording',
    items: [
      { text: 'The dashboard and map pages now just say that blue figures update live.' },
    ],
  },
  {
    version: '2.23.1',
    date: '2026-09-30',
    title: 'Server-wide player counts',
    items: [
      { text: 'Maps now show one player count for the whole server instead of a count per world.' },
    ],
  },
  {
    version: '2.23.0',
    date: '2026-09-30',
    title: 'Standings',
    items: [
      { text: 'A new Standings page under Sietch shows the Top 10 characters by level and the level spread across the server. It moved here from the public Dune page.' },
      { text: 'Map counters no longer show a player count for Amtal.' },
    ],
  },
  {
    version: '2.22.3',
    date: '2026-09-30',
    title: 'Amtal safe areas corrected',
    items: [
      { text: 'The whole Riftwatch and Helius Gate settlements are safe on Amtal, not just the quartermaster. The CHOAM Exchange terminal at each sits near the quartermaster.' },
      { text: 'The live map no longer shows vehicles on Amtal, the same as players.' },
    ],
  },
  {
    version: '2.22.2',
    date: '2026-09-30',
    title: 'Amtal safe areas',
    items: [
      { text: 'Amtal is PvP everywhere, including the tradeposts. The Riftwatch and Helius Gate settlements are the two safe areas, each with a CHOAM Exchange terminal near the quartermaster. The Amtal page shows where they are.' },
    ],
  },
  {
    version: '2.22.1',
    date: '2026-09-29',
    title: 'Facebook page',
    items: [
      { text: 'Last Sietch now has a Facebook page. The footer has Discord and Facebook icons that open each one.' },
    ],
  },
  {
    version: '2.22.0',
    date: '2026-09-28',
    title: 'Events Manager',
    items: [
      { text: 'Events are now written by admins in the admin panel Events Manager, with times set in Eastern, a preview of the event card, and a history of every change.' },
      { text: 'A cancelled event now shows why it was cancelled on its event page.' },
    ],
  },
  {
    version: '2.21.10',
    date: '2026-09-28',
    title: 'Karum item details fixed',
    items: [
      { text: 'Details and comparison on a Karum listing or wanted order opens the full panel again: grade, quantity, price per unit and the comparison list. It was opening half off the screen, showing only the Exchange watch link.' },
    ],
  },
  {
    version: '2.21.9',
    date: '2026-09-28',
    title: 'In-game verification fixed',
    items: [
      { text: 'The verification code box on the sign-in page accepts the 6-digit code from Cielago again. A formatting rule on the box rejected every valid code, so new players could not create a profile by verifying in game.' },
    ],
  },
  {
    version: '2.21.8',
    date: '2026-09-24',
    title: 'Server settings rolled back',
    items: [
      { text: 'After community feedback, the tuning added since 1.5 is back to stock: character XP, Intel and the Landsraad mission bonuses, enemies, stamina, water extraction, resource and loot respawn, crafting and building costs, the building piece budget, gear wear and repair durability, hand gathering, and the sandworm takes everything again.' },
      { text: 'Staying from before 1.5: 3x cutteray and vehicle mining, doubled power, the Deep Desert PvP resource bonus, and our Landsraad board (6 contracts, 15 minute repeat, 10 second abandon, 35 charges a day, 20,000 house goal).' },
      { text: 'Double XP events replace the permanent XP bonus. 2x inventory volume stays until the community votes. Polls run in Discord, one a day.' },
    ],
  },
  {
    version: '2.21.6',
    date: '2026-09-22',
    title: 'Server rates update',
    items: [
      { text: 'Server side, with Funcom Hotfix 1.5.3.3: 3x gathering and mining is now the native server setting (the old mining console variables are back at 1x, so nothing stacks), 2x character XP and Intel, 3x specialization XP, 1.5x contribution and 2x faction standing per Landsraad mission.' },
      { text: 'Stamina drains at half rate. Water extraction and loot chests are twice as fast, inventory volume is doubled, the building piece budget is 1.2x, repairs never lower max durability, and the sandworm only takes your backpack.' },
      { text: 'Every one of these is visible in the game\'s own Custom Settings screen on the server page, and the Rules page here lists them under server configuration.' },
    ],
  },
  {
    version: '2.21.3',
    date: '2026-09-16',
    title: 'Clearer daily reward dates',
    items: [
      { text: 'Rewards now labels its calendar and current reward day as UTC, and shows the next daily reset in your local time. The daily reset remains midnight UTC.' },
    ],
  },
  {
    version: '2.21.2',
    date: '2026-09-13',
    title: 'Discord sign-in first',
    items: [
      { text: 'Continue with Discord is now the first sign-in option, followed by passkey and password. Existing Discord users can keep using their linked profile and add another sign-in method in Settings.', feature: 'profile_login' },
    ],
  },
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
    // dune-market-bot/exchange.py buy_player_listings (buys a player listing at or
    // below its buy price, pays the listing's own price, writes the seller's
    // completion_type 4 payout row that the client shows as SOLD with Take Solari);
    // routers/portal.py _bot_buy_base_cap, the flips board and _sell_fee
    // (_SELL_DURATIONS 1, 3, 7, 14); PriceLadderDrawer.svelte ("CHOAM bot buys up
    // to", per grade); BotLimits.svelte (BUY is a hard weekly limit, gear uncapped);
    // FlipBoard.svelte; OrderRow.svelte (completion_type 3 = a pulled listing);
    // the design notes (not published) (Completed tab, SOLD, CANCELED,
    // Take item, a cancel forfeits the listing fee);
    // the design notes (not published) (offline
    // listing, escrow at listing, no fee) and scripts/dune-karum-op.sh (payment is a
    // bank Solari debit and credit). The bot's buy interval (300 s) is the
    // market_bot.py default.
    id: 'selling',
    title: 'Selling: the Exchange, the CHOAM bot and the Karum',
    since: '2.28.21',
    body: [
      'There are two places to sell. The CHOAM Exchange is the in-game market: a listing runs for 1, 3, 7 or 14 days, a listing fee comes out of your bank, and the CHOAM bot may buy it. The Karum is between players only: no listing fee, nothing expires, and the bot never buys there.',
      'Selling to the bot: open an item on the Exchange page to see its price ladder. The CHOAM bot buys up to line shows the most the bot pays per unit, for each grade. List on the Exchange at or below that price and the bot may buy your listing at your price. It checks every few minutes.',
      'The bot price is the floor. A player ask under it shows on the Bot-Floor flips board, where anyone can buy it and sell it on to the bot for the difference. If you list under the bot price, you leave Solari on the table.',
      'The bot has weekly buying limits for bulk resources. The CHOAM buyer panel at the top of the Exchange page shows them. When a BUY meter is full, the bot stops buying that item until purchases older than 7 days drop off. Gear has no limit.',
      'When a listing sells, to a player or to the bot, it moves to the Completed tab at any exchange terminal, marked SOLD. Select it and press Take Solari to move the money into your bank.',
      'Items waiting for you also sit in Completed. Press Take item to collect them. A listing you pulled, a Karum purchase and a server delivery all show as CANCELED. That is the only label the game can show for them, and nothing is wrong. Pulling a listing on the Exchange keeps the item but loses the listing fee.',
      'The Karum: list one stack from your bank or backpack at a fixed price while you are logged out of the game. The item is held from the moment you list it. When it sells, the buyer pays from their banked Solari and you are paid straight into your bank, with nothing to collect. The buyer collects the item at an exchange terminal, under Completed, with Take item.',
    ],
  },
  {
    // The owner-approved player text, adapted to this format with the wording kept.
    // Help only: it is not announced.
    id: 'solido-replicator',
    title: 'Solido Replicator limits raised: one quick client fix',
    since: '2.28.21',
    body: [
      'Funcom recently added a limit on how many Solido Replicators you can hold. We raised that limit on our servers, so they now allow far more.',
      'Backpack: 100 (Funcom: 20). Base storage: 2,000 (Funcom: 10). Bank: 200 (Funcom: 20). Vehicle storage: 100 (Funcom: 10).',
      'Your backpack, bank and vehicles don\'t have that many slots, so in practice there\'s no limit: fill a whole inventory with Solidos if you truly want to.',
      'Your game client still uses Funcom\'s limits. You only need the fix below if you hit Funcom\'s limit and want to use the extra Solidos our servers support. Without it you can still craft past the limit, but the game shows your storage in red (for example 23/10) and greys out Deposit from your backpack.',
    ],
    steps: [
      'Close Dune completely.',
      { text: 'Press Win + R, paste this and press Enter:', code: '%LOCALAPPDATA%\\DuneSandbox\\Saved\\Config\\Windows' },
      'Right-click Game.ini, choose Open with, then Notepad. (Optional: copy the file somewhere first as a backup.)',
      {
        text: 'Go to the very end of the file and paste these two lines on new lines:',
        code: '[/Script/DuneSandbox.InventorySystemSettings]\nInventoryHardCapPerItemMap=(((Name="BuildingBlueprint_CopyDevice"), (HardCapPerInventoryHardCapType=((PlayerInventory, 100),(BaseStorage, 2000),(Bank, 200),(VehicleStorage, 100)))))',
        note: 'The second line must stay on ONE line. Do not change anything else in the file.',
      },
      'Save, close Notepad, and launch the game.',
    ],
    after: [
      'Check it worked: hover a Solido Replicator. It should say Carried x/100 in your backpack, or x/2,000 in base storage, and Deposit works again.',
      'This only changes the limits your own game client shows and checks. The server still enforces the real limits, so typing bigger numbers will not get you more.',
      'To undo it, delete the two lines.',
      'If you don\'t see Game.ini, launch the game once, close it, and try again.',
      'Questions? Ask in #help-and-feedback.',
    ],
  },
  {
    // Seven Nights of the Sietch (Halloween 2026): sietch_nights.py, portal_sietch.py and
    // routes/sietch. Numbers here must match sietch_nights.py (VISIT_AWARD, KNOCK_CAP,
    // CAPS, STREAK_AWARD, DEFAULT_RADIUS, KNOCK_COOLDOWN_S) and the copy in
    // data/sietch_nights_copy.json. Never name a night's doors or the Stall's secrets here.
    id: 'seven-nights',
    title: 'Seven Nights of the Sietch: questions and answers',
    since: '2.28.6',
    feature: 'sietch',
    body: [
      'When does it run? From Sunday, October 25 at 8 PM ET to Sunday, November 1 at 7 PM ET. Each night opens at 8 PM ET and runs until 8 PM ET the next day. Clocks fall back on the last night, so Night 7 ends at 7 PM ET, one hour earlier than the others.',
      'Who can earn LS Tokens? Any linked character at level 20 or higher, or anyone who played on Last Sietch before October 25. Sign in to the portal with Discord or another saved method and link your game character under Settings > Characters first.',
      'How do I earn? Play on Last Sietch each night for 4 LS Tokens. Knock at tonight\'s doors for up to 6 more. Each Landsraad turn-in earns 2, up to 4 a night (two turn-ins). On the Landsraad night up to four turn-ins count, for up to 8. Nights 1 and 2 were recounted this way. Play on all seven nights for a 20 token streak bonus. Every reward was doubled on October 27, and everything earned before that was doubled too. Some nights hold extra places to find, with their own rewards.',
      'How does Knocking work? Open Seven Nights in the portal to see tonight\'s doors. Travel there in game, stand within about 150 m of the door, and press Knock on the portal while you are online. Doors are in Hagga Basin and count on every Hagga sietch: Habbanya and Amtal. On Amtal the doors sit in PvP ground, so watch your back. Wait about 30 seconds between Knocks. If it says the desert is still settling, stand still a moment and Knock again.',
      'Where do I see my tokens? Your Hoard, under Seven Nights, shows your LS Tokens, where each one came from, and the badges and map markers you own. Tokens can take a few minutes to appear after you play or make an offering. LS Tokens you do not spend stay in your Hoard for future Sietch events.',
      'How does the Token Stall work? It opens Wednesday, October 28 at 8 PM ET and closes Tuesday, November 3 at 7 PM ET. New goods arrive each night until Halloween. You need a linked game character to buy. Portal goods such as badges and map markers are yours at once.',
      'Where do game items from the Stall arrive? In your CHOAM bank. Change zones or relog to see them, then use the item to unlock it. If your bank is full, the purchase is refunded: make room and buy again. Items you have already unlocked are not sold to you twice.',
      'What is the Night of Costumes? On Halloween night, Saturday, October 31 from 8 PM ET, post a screenshot of your costume or your spookiest base in the server-events channel on our Discord. Every entry earns LS Tokens. The community votes on Sunday, November 1 from 12 PM to 5 PM ET, and three winners earn more.',
      'Something looks wrong? Use /report with Cielago in our Discord and say which night and what you saw. Staff can check every token in your Hoard.',
    ],
  },
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
    // Owner decision D-2 (2026-10-28, revised 2026-10-29: no guildmate first claim), recorded in
    // the design notes (not published) and
    // ops/subfief-reclaim/ABANDONED-BASE-REMOVAL-RUNBOOK.md. Same wording as rule 11 in routes/rules/+page.svelte.
    id: 'inactive-bases',
    title: 'Inactive bases on Habbanya',
    since: '2.28.21',
    body: [
      'A Habbanya base whose owner has not played for 30 days is treated as inactive. The owner gets a notice in their portal mailbox and on Discord, then 14 days of grace.',
      'When the grace period ends, the base is saved to the owner\'s Base Vault first. Then it is opened for a timed public salvage event, and demolished after the event.',
      'The Base Vault copy is the owner\'s safety net: a returning owner can ask staff about restoring the base from the Vault.',
    ],
  },
  {
    // portal_credentials.py, routers/portal_signin.py and SecurityPanel.svelte.
    id: 'sign-in-security',
    title: 'Passwords, passkeys and recovery',
    since: '2.21.0',
    feature: 'profile_login',
    body: [
      'Already linked through Discord? Sign in with that same Discord account, then open Settings > Account and sign-in. Add a password or passkey to the existing profile. Your linked accounts, characters, history, identity code and allowances stay together.',
      'Choose a portal username of 3 to 32 letters, numbers, dots, underscores or hyphens, starting with a letter or number. Passwords use 8 to 128 characters; spaces, Unicode and password-manager paste are supported. Common passwords are refused. No email is required.',
      'A passkey uses your device lock, fingerprint, face or security key. You can save up to eight passkeys and give each a label. Removing a password or passkey is under Advanced on the same page. Keep another saved method you can reach if a device is lost.',
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
      'Open Sign in, choose Other ways to sign in, then New here? Verify in game. Stay in game and enter your exact online character name. Cielago sends a private six-digit code to that character; enter it in the same browser within five minutes. Five incorrect guesses end the attempt, and repeated requests are limited.',
      'After verification, choose a portal username and save your first password or passkey within ten minutes. This finishes creating the profile. You can add more sign-in methods later in Settings.',
      'An account already linked to the portal must use its existing profile and saved sign-in method. To link another game account, sign in again, then open Settings > Characters and choose Add a game account. Game verification never combines profiles or resets shared allowances.',
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
    // Weekend events (portal plan Q7). Rotation, window and restarts:
    // the design notes (not published) (owner rulings
    // 1 and 4). Values: ops/weekend-events/event-effect-worker.py (LIVE, RESTART)
    // against ops/game-config/values-of-record.json and docs/SERVER-RATES-AND-RULES.md
    // (XP and Landsraad stock 1x, CraftingCost 1.0, mining cvars 1.0, PvP zones
    // 2.5x). State words: src/lib/weekend.js STATE_LABELS. Scope is per event
    // (fleet or Amtal); only a narrowed scope is worded on the card.
    id: 'weekend-events',
    title: 'Weekend events',
    since: '2.28.22',
    body: [
      'Every weekend has an event. It runs from Friday 12:00 AM Eastern (the moment Thursday night becomes Friday) to Monday 12:00 AM Eastern (the end of Sunday). In UTC that is Friday 05:00 to Monday 05:00 in winter (EST), and 04:00 to 04:00 in summer (EDT). Home and the Events page show each start and end in your own time zone as well.',
      'The event depends on which Friday of the month it is. 1st Friday: Double XP weekend. 2nd Friday: Double Landsraad weekend. 3rd Friday: Crafting weekend. 4th Friday: Harvest weekend. A month with a 5th Friday gets an event picked for that month, posted on the Events page. The rotation starts on Friday, November 6 with Double XP.',
      'Double XP weekend: character XP is doubled. Our normal rate is stock (1x), so this is twice stock.',
      'Double Landsraad weekend: Landsraad mission rewards give twice the normal specialization XP. Our normal rate is stock (1x). Character XP is not changed.',
      'Crafting weekend: crafting costs 20% less material than normal (0.8x our normal cost, which is stock).',
      'Harvest weekend: cutteray and vehicle mining yield is doubled on top of each map\'s normal rate. Hand gathering is not changed. Inside PvP zones the 2.5x PvP resource bonus still applies on top.',
      'Where it applies: an event that covers one map only says so on its card, such as "On Amtal only".',
      'Double XP and Crafting weekends need a short server restart at the start and again at the end, because those settings only load when the servers start. You get an in-game notice 10 minutes and 1 minute before each restart. Double Landsraad and Harvest weekends switch on and off with no restart.',
      'Is it on? The event card says. "Live now" means the bonus was checked on the servers. "Set to start" and "Not live yet" mean it has not been confirmed yet. "Did not start" and "Not confirmed live" mean it is not running as planned: staff can see that too, and you can ask with /report in our Discord. "Planned" means the weekend is in the rotation but not posted on the events board yet.',
    ],
  },
  {
    // the design notes (not published): player-confirmed recovery test.
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
      'In the PC game, a new character starts from New Game, then Multiplayer, then Server Browser in the Recommended Server popup. An existing character goes Characters, then Server Browser. Open the Experimental tab, choose North America and search Last Sietch. Expand the row and choose your sietch; existing characters see a Transfer option there. The public Dune page includes a copy-name button and screenshots.',
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
      'The pick is saved on your account rather than in the browser, so it follows you to any browser you sign in from. The same switch lives under Settings > Display, in the Layout panel, with everything else that changes how a page is arranged.',
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
      'Reward days and calendar dates use UTC. A new reward day begins at 00:00 UTC, which can be the previous evening in your local time. Rewards shows the current UTC reward day, a countdown and the next reset in your local time.',
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
      'Once you are linked, your layers and your viewer choice save against the character you have selected, so each character can open the same board its own way, on any browser. Settings > Display, under Layout, lists what each character has saved in its Advanced section and lets you clear a board back to its defaults.',
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
      'Once you are linked you carry an identity code: eight characters drawn from an alphabet with no 0, 1, I or O in it, so it survives being read aloud. It is one code per portal profile and you can rotate it whenever you like, which retires the old one. Another player uses it to send you items or Solari without ever knowing your character name. It is never a link, so nothing you scan or paste can send you anywhere. Find it under Settings > Transfers and activity, with Rotate in the Advanced section.',
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
      'The player directory on the same page is how you find somebody to write to. Whether you appear in it yourself is a switch under Settings > Privacy and data.',
    ],
  },
  {
    // the design notes (not published)
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
