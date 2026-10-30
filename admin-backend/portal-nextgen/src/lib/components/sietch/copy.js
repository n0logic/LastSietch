// Player copy for the Seven Nights page. GENERATED from
// admin-backend/data/sietch_nights_copy.json (the copy of record), minus the
// Discord block and anything secret: the secret stall copy reaches a player only
// through /portal/sietch/state once it has opened, never through the bundle.
// The remote build ships portal-nextgen alone, so the JSON cannot be imported
// across the tree; scripts/tests/test_sietch_copy.py keeps the two equal.
// Regenerate: python3 scripts/tests/test_sietch_copy.py --write-js
export const COPY = {
  "version": 1,
  "event": {
    "name": "Seven Nights of the Sietch",
    "currency": "LS Tokens",
    "starts_at": "2026-10-26T00:00:00Z",
    "ends_at": "2026-11-02T00:00:00Z",
    "starts_text": "Sunday, October 25 at 8 PM ET",
    "ends_text": "Sunday, November 1 at 7 PM ET",
    "tagline": "Seven nights. Seven doors. The Sietch keeps its lanterns lit.",
    "summary": "For seven nights the Sietch opens its doors after dusk. Visit the portal each night, Knock at the places the desert names, bring offerings to the Landsraad, and earn LS Tokens. Spend them at the Token Stall from Wednesday night.",
    "dst_note": "Clocks fall back on the last night, so Night 7 ends at 7 PM ET, one hour earlier than the others.",
    "carryover_note": "LS Tokens you do not spend stay in your Hoard for future Sietch events."
  },
  "nights": [
    {
      "n": 1,
      "key": "2026-10-26",
      "starts_at": "2026-10-26T00:00:00Z",
      "ends_at": "2026-10-27T00:00:00Z",
      "runs_text": "Sunday 8 PM ET to Monday 8 PM ET",
      "activity": "porches",
      "title": "The Porches Open",
      "hook": "The Sietch doors open at dusk. Knock on three porches tonight and see what the desert leaves you.",
      "how": "Travel to one of tonight's porches in Hagga Basin, stand at the door, and press Knock on the Seven Nights page of the portal while you are in game. Each porch gives 2 LS Tokens, three porches in all. One porch lies in irradiated ground, so bring protection.",
      "award_text": "2 LS Tokens per porch"
    },
    {
      "n": 2,
      "key": "2026-10-27",
      "starts_at": "2026-10-27T00:00:00Z",
      "ends_at": "2026-10-28T00:00:00Z",
      "runs_text": "Monday 8 PM ET to Tuesday 8 PM ET",
      "activity": "riddles",
      "title": "The Riddle Trail",
      "hook": "Three riddles, three doors. Solve one to hear the next.",
      "how": "Read the riddle, find the place it describes, and Knock there. A right answer gives you the next riddle. A wrong place only tells you it is not here. One of tonight's places lies in irradiated ground, so bring protection.",
      "award_text": "4 LS Tokens per riddle, 6 more for solving all three"
    },
    {
      "n": 3,
      "key": "2026-10-28",
      "starts_at": "2026-10-28T00:00:00Z",
      "ends_at": "2026-10-29T00:00:00Z",
      "runs_text": "Tuesday 8 PM ET to Wednesday 8 PM ET",
      "activity": "offerings",
      "title": "Offerings to the Sietch",
      "hook": "A new Landsraad term rises. Tonight up to four offerings pay.",
      "how": "Turn in Landsraad contracts as you normally would. Each turn-in earns 2 LS Tokens, and tonight up to four turn-ins count instead of two.",
      "award_text": "Up to 8 LS Tokens from offerings"
    },
    {
      "n": 4,
      "key": "2026-10-29",
      "starts_at": "2026-10-29T00:00:00Z",
      "ends_at": "2026-10-30T00:00:00Z",
      "runs_text": "Wednesday 8 PM ET to Thursday 8 PM ET",
      "activity": "stall",
      "title": "The Stall Opens",
      "hook": "The Token Stall lifts its shutters at dusk. Knock at the stall and bring your LS Tokens.",
      "how": "The Token Stall opens on the portal at 8 PM ET. Knock at tonight's stall landmark in game for a bonus.",
      "award_text": "6 LS Tokens for Knocking at the stall"
    },
    {
      "n": 5,
      "key": "2026-10-30",
      "starts_at": "2026-10-30T00:00:00Z",
      "ends_at": "2026-10-31T00:00:00Z",
      "runs_text": "Thursday 8 PM ET to Friday 8 PM ET",
      "activity": "seek",
      "title": "Hide and Seek: The Lost Cielago",
      "hook": "Deep in the Hagga Rift, above a Deserter camp the old maps call by a star's name, a lone figure keeps watch and speaks to no one. Somewhere near him, Cielago, the Fremen messenger bat, has gone to ground. Mind your fingers: Cielago bites.",
      "how": "Knock anywhere in Hagga Basin. The desert answers Scorching, Warm, Cool or Cold. Follow the heat until you find Cielago.",
      "award_text": "10 LS Tokens for finding Cielago, 10 more for the first three seekers"
    },
    {
      "n": 6,
      "key": "2026-10-31",
      "starts_at": "2026-10-31T00:00:00Z",
      "ends_at": "2026-11-01T00:00:00Z",
      "runs_text": "Friday 8 PM ET to Saturday 8 PM ET",
      "activity": "haunt",
      "title": "Fright Night",
      "hook": "Something stirs where the worms feed. Knock at the haunted ground, and braver still if the storm is up.",
      "how": "Find tonight's haunted ground and Knock there. If a sandstorm is live in Hagga Basin when you Knock, the desert pays extra. The haunted ground is irradiated, so bring protection.",
      "award_text": "6 LS Tokens, or 10 in a sandstorm"
    },
    {
      "n": 7,
      "key": "2026-11-01",
      "starts_at": "2026-11-01T00:00:00Z",
      "ends_at": "2026-11-02T00:00:00Z",
      "runs_text": "Saturday 8 PM ET to Sunday 7 PM ET",
      "activity": "costumes",
      "title": "Night of Costumes",
      "hook": "Halloween night. Wear your strangest look, and visit the Stall. Something new is waiting.",
      "how": "Submit a screenshot of your costume or your spookiest base in the Discord server-events channel. Every entry earns LS Tokens, and the community votes for three winners. The last night ends at 7 PM ET.",
      "award_text": "6 LS Tokens per entry; winners earn 20, 30 and 50"
    }
  ],
  "knock": {
    "button": "Knock",
    "button_busy": "Knocking",
    "treat": "Treat! The door opens.",
    "miss": "No one answers here.",
    "miss_distance": "No one answers here. The nearest door is {dist} away.",
    "riddle_miss": "Not here.",
    "riddle_hit": "The door opens. The next riddle is yours.",
    "stale": "The desert is still settling. Stand at the door a moment and knock again.",
    "closed": "The doors are shut right now. Come back at dusk.",
    "ineligible": "Unlocks at level 20.",
    "amtal": "No doors out here.",
    "offline": "You need to be in game to Knock.",
    "cooldown": "Give the door a moment before you knock again.",
    "done": "You have Knocked at every door tonight.",
    "bands": {
      "scorching": "Scorching. Cielago is very close.",
      "warm": "Warm. You are on the trail.",
      "cool": "Cool. Cielago is somewhere farther off.",
      "cold": "Cold. Nothing out here but sand."
    },
    "found": "You found Cielago!"
  },
  "progress": {
    "visit": "Visit tonight",
    "knock": "Knocks",
    "landsraad": "Landsraad offerings",
    "activity": "Tonight's activity"
  },
  "stall": {
    "name": "Token Stall",
    "opens_at": "2026-10-29T00:00:00Z",
    "closes_at": "2026-11-04T00:00:00Z",
    "opens_text": "Wednesday, October 28 at 8 PM ET",
    "closes_text": "Tuesday, November 3 at 7 PM ET",
    "closed": "The Stall is closed for now.",
    "before_open": "The Token Stall opens Wednesday at 8 PM ET. Save your LS Tokens.",
    "bank_note": "Your goods wait in your CHOAM bank. Change zones or relog to see them, then use the item to unlock it.",
    "spec_note": "Delivered when you next log out: the XP can only be written while your character is offline. Until then the order shows as waiting, which is normal. Log back in to see it on the track.",
    "spec_pending": "Delivered when you next log out",
    "charxp_note": "Delivered when you next log out: character XP can only be written while you are offline. Until then the order shows as waiting, which is normal. Log back in to see your new level.",
    "charxp_pending": "Delivered when you next log out",
    "bank_full": "Your CHOAM bank is full. Make room and buy again.",
    "link_prompt": "Link a game character to buy from the Stall."
  },
  "board": {
    "title": "Brightest Lanterns",
    "anon_name": "A wanderer",
    "opt_out": "Hide my name on the leaderboard"
  }
};
