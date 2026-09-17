"""Landsraad house -> in-game rep location (where a player travels to claim a
reward queued at that house). Single source of truth shared by the V1/V2 admin
grant UI (routers/v2_players.py) and the public player portal
(routers/portal.py landsraad page). Sourced from method.gg 2026-05-23:
method.gg/dune-awakening/all-landsraad-house-representative-locations-in-dune-awakening
"""

HOUSE_REP_LOCATIONS = {
    "DA_HouseAlexin": "Harko Village council chamber (before tax counter)",
    "DA_HouseArgosaz": "Griffins Reach Tradepost (Hagga south starter)",
    "DA_HouseDyvetz": "Hagga Rift, NW of Riftwatch (Harkonnen checkpoint)",
    "DA_HouseEcaz": "Southern Hagga Rift, small cave in spire nook",
    "DA_HouseHagal": "Eastern Shield Wall, rocks beside cliff edge",
    "DA_HouseHurata": "Eastern Vermillius Gap, cave halfway up cliff",
    "DA_HouseImota": "Jabal Eifrit Al-Sharq border, small cave halfway up cliff",
    "DA_HouseKenola": "Mysa Tarill, cave atop rocky spire behind Mass Kharet stronghold",
    "DA_HouseLindaren": "Western Shield Wall, cave in cliff south of The View",
    "DA_HouseMaros": "Deep Desert tile B8, northeastern mountain base",
    "DA_HouseMikkarol": "Jabal Eifrit Al-Janub, cliffs southwest of The Slant",
    "DA_HouseMoritani": "Hagga Rift, Harkonnen building balcony NE of Riftwatch",
    "DA_HouseMutelli": "Arrakeen, upper walkway near entrance (after stairs)",
    "DA_HouseNovebruns": "Western Vermillius Gap, cave atop cliff SE of Wreck of the Pallas",
    "DA_HouseRichese": "Helius Gate (Eastern Shield Wall), behind contract board",
    "DA_HouseSor": "Pinnacle Station Tradepost, Jabal Eifrit Al-Gharb",
    "DA_HouseSpinette": "Harko Village, upper walkway overlooking central grounds",
    "DA_HouseTaligari": "Crossroads Tradepost, Mysa Tarill",
    "DA_HouseThorvald": "Hagga Rift, small cave at chasm bottom near Wreck of the Kytheria",
    "DA_HouseTseida": "Anvil Tradepost, Eastern Vermillius Gap",
    "DA_HouseVarota": "Arrakeen council chamber (before tax counter)",
    "DA_HouseVernius": "Helius Gate (Eastern Shield Wall), off to the side",
    "DA_HouseWallach": "Arrakeen, building on left past ornithopter landing site",
    "DA_HouseWayku": "Deep Desert tile A2, east of huge NE mountain",
    "DA_HouseWydras": "Northern Sheol, rocky outcroppings north of Edge of Acheron",
}
