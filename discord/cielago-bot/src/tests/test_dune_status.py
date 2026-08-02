from cielago.cogs.dune_status import (
    DEATH_VARIANTS,
    _clean_base_label,
    _et_hour_range,
    _f_almanac,
    _f_construction,
    _f_moderation,
    _f_most_active,
    _f_raids,
    _f_spice,
    _f_testing_stations,
    _f_worlds,
    _friendly_placeable,
    _friendly_vehicle,
    _name,
    build_embed,
)


def test_name_fallback():
    assert _name("acct 17") == "a fellow Sleeper"
    assert _name("acct 149") == "a fellow Sleeper"
    assert _name("Chani") == "Chani"
    assert _name("acctington") == "acctington"  # not the acct-N pattern


def test_most_active_uses_name_fallback():
    out = _f_most_active([{"name": "acct 17", "hours": 3.0}, {"name": "AlphaPlayer", "hours": 1.0}])
    assert "a fellow Sleeper" in out
    assert "acct 17" not in out


def test_worlds_elsewhere_reconciles():
    out = _f_worlds({"online_total": 2, "hagga_players": 1, "deep_desert_players": 0})
    assert "+1 elsewhere" in out
    assert "Online now: **2**" in out


def test_worlds_no_elsewhere_when_balanced():
    out = _f_worlds({"online_total": 3, "hagga_players": 2, "deep_desert_players": 1})
    assert "elsewhere" not in out

# A realistic stats blob shaped like the lastsietch-relay /dune/stats/digest payload.
RETIRED_BRANDING: list[str] = []

SAMPLE_DAILY = {
    "period": "daily",
    "new_players": {"names": ["AlphaPlayer", "GammaPlayer"], "count": 2, "total_all_time": 40},
    "worlds": {"hagga_players": 3, "deep_desert_players": 1, "online_total": 4},
    "server_pulse": {"peak": 4, "play_hours": 29.6, "busiest_hour_utc": 0, "active_days": None},
    "most_active": [{"name": "AlphaPlayer", "hours": 13.5}, {"name": "BetaPlayer", "hours": 7.4}],
    "deaths": [{"name": "DeltaPlayer", "count": 1}],
    "raids": {"events": [], "self_demos": 2, "storm_orni": 1, "storm_buggy": 0},
    "server_health": {"pods_total": 6, "troubled": [], "up_since_utc": "2026-05-28T18:53:38+00:00"},
    "improvements": ["Spice toggle shipped", "Moderation trio live"],
    "spice": {"fields": [], "active_now_total": 72, "spawning_count": 8, "field_count": 8},
    "moderation": {"kicks": 0, "bans": 0, "unbans": 0, "active_bans": 0},
    "pilot": None,
    "construction": None,
    "origins": None,
}


def test_et_hour_range():
    assert _et_hour_range(0) == "8 PM - 9 PM ET"
    assert _et_hour_range(16) == "12 PM - 1 PM ET"


def test_friendly_vehicle_blueprint_path():
    path = (
        "/Game/Dune/Systems/Vehicles/Blueprints/FlyingVehicles/"
        "BP_LightOrnithopter_Choam.BP_LightOrnithopter_Choam_C"
    )
    assert _friendly_vehicle(path) == "Light Ornithopter"


def test_friendly_vehicle_model_name():
    assert _friendly_vehicle("#MediumOrnithopterCHOAM") == "medium ornithopter"


def test_worlds_render():
    out = _f_worlds(SAMPLE_DAILY["worlds"])
    assert "Habbanya" in out and "Kulon" in out
    assert "Online now: **4**" in out


def test_spice_all_spawning():
    out = _f_spice({"active_now_total": 72, "spawning_count": 8, "field_count": 8})
    assert "**72**" in out
    assert "All **8** field types spawning" in out


def test_spice_some_suppressed():
    out = _f_spice({"active_now_total": 10, "spawning_count": 5, "field_count": 8})
    assert "5/8" in out and "3 suppressed" in out


def test_moderation_skips_when_empty():
    assert _f_moderation({"kicks": 0, "bans": 0, "unbans": 0, "active_bans": 0}) is None


def test_moderation_reports_activity():
    out = _f_moderation({"kicks": 2, "bans": 1, "unbans": 0, "active_bans": 3})
    assert "Kicks: **2**" in out and "Bans: **1**" in out
    assert "Active bans standing: **3**" in out


def test_raids_quiet():
    name, value = _f_raids(SAMPLE_DAILY["raids"])
    assert name == "Raid / Destruction Watch"
    assert "All quiet on the sands" in value
    assert "Self-demolitions: 2" in value


def test_raids_with_events():
    raids = {
        "events": [
            {"epoch": 1748000000, "owner": "n0logic", "raider": "Bandit",
             "thing": "wall", "shielded": True}
        ],
        "self_demos": 0, "storm_orni": 0, "storm_buggy": 0,
    }
    name, value = _f_raids(raids)
    assert name.startswith("⚠️")
    assert "n0logic" in value and "Bandit" in value and "shielded" in value


def test_death_variants_rotate():
    assert len(DEATH_VARIANTS) == 3


def test_build_embed_daily_branding():
    embed = build_embed(SAMPLE_DAILY)
    assert embed.title == "🏜️ Sietch Daily Report"
    assert embed.author.name == "Last Sietch"
    assert "lastsietch.com" in embed.footer.text
    blob = (embed.title + embed.footer.text + embed.author.name).lower()
    # Guard against retired branding leaking into player-facing chrome.
    # Fill RETIRED_BRANDING with whatever names your server used to go by:
    # renames are exactly when this slips through, and the embed chrome is
    # the last place anyone thinks to look.
    for term in RETIRED_BRANDING:
        assert term not in blob, f"retired branding {term!r} leaked into embed chrome"


def test_build_embed_daily_has_new_sections():
    embed = build_embed(SAMPLE_DAILY)
    names = [f.name for f in embed.fields]
    assert "🏜️ The Sietches" in names
    assert "🌶️ Spice Fields" in names
    # moderation all-zero -> no Sietch Justice field
    assert "⚖️ Sietch Justice" not in names


def test_build_embed_weekly_sections():
    weekly = dict(SAMPLE_DAILY)
    weekly["period"] = "weekly"
    weekly["server_pulse"] = {
        "peak": 5, "play_hours": 108.8, "busiest_hour_utc": 0, "active_days": 7,
    }
    weekly["pilot"] = [
        {"name": "EtaPlayer", "km": 145.0, "vehicle_raw": "BP_LightOrnithopter_Choam_C"}
    ]
    weekly["construction"] = {
        "total_subfiefs": 25, "great": 16, "lesser": 9, "pieces_total": 11531,
        "biggest": [{"label": "n0logic's base", "pieces": 900}], "pacts": [], "renames": [],
    }
    weekly["origins"] = [{"country": "United States", "count": 25}]
    embed = build_embed(weekly)
    names = [f.name for f in embed.fields]
    assert embed.title == "🏜️ Sietch Weekly Report"
    assert "🪶 Pilot of the Week" in names
    assert "🏗️ Sietch Construction" in names
    assert "Origins" in names


# ---- leak fix: destroyed-placeable prettifying ----------------------------

def test_friendly_placeable_strips_and_reorders():
    assert _friendly_placeable("Totem_Small_Placeable") == "small totem"
    assert _friendly_placeable("BP_Wall_Large_C") == "large wall"
    assert _friendly_placeable("wall") == "wall"


def test_raids_prettifies_placeable_thing():
    raids = {
        "events": [
            {"epoch": 1748000000, "owner": "n0logic", "raider": "Bandit",
             "thing": "Totem_Small_Placeable", "shielded": False}
        ],
        "self_demos": 0, "storm_orni": 0, "storm_buggy": 0,
    }
    _, value = _f_raids(raids)
    assert "small totem" in value
    assert "Totem_Small_Placeable" not in value


def test_raids_buggy_pluralization():
    one = {"events": [], "self_demos": 0, "storm_orni": 1, "storm_buggy": 1}
    _, value = _f_raids(one)
    assert "1 ornithopter," in value and "1 buggy " in value
    assert "buggy/ies" not in value and "(s)" not in value
    many = {"events": [], "self_demos": 0, "storm_orni": 2, "storm_buggy": 3}
    _, value = _f_raids(many)
    assert "2 ornithopters," in value and "3 buggies " in value


# ---- leak fix: console-name biggest-base label ----------------------------

def test_clean_base_label_console():
    assert _clean_base_label("Advanced Sub-Fief Console") == "an unnamed holding"
    assert _clean_base_label("Sub-Fief Console") == "an unnamed holding"
    assert _clean_base_label("n0logic's base") == "n0logic's base"


def test_construction_hides_console_label():
    out = _f_construction({
        "total_subfiefs": 1, "great": 1, "lesser": 0, "pieces_total": 100,
        "biggest": [{"label": "Advanced Sub-Fief Console", "pieces": 900}],
        "pacts": [], "renames": [],
    })
    assert "an unnamed holding" in out
    assert "Console" not in out


# ---- Desert Almanac renderer ----------------------------------------------

def test_almanac_full():
    out = _f_almanac({
        "flight_km_total": 1234.5, "structures_delta": 312, "vehicles_now": 48,
        "worm_breaches": 14, "sandstorms": {"HaggaBasin": 6, "DeepDesert": 9},
    })
    assert "**1234.5 km**" in out
    assert "grew by **312** build pieces" in out
    assert "**48** vehicles roam the sands" in out
    # 14 / 7 = 2.0 per day
    assert "average of **2.0** times per day" in out
    assert "**6** sandstorms swept Hagga" in out
    assert "**9** sandstorms crossed the Deep Desert" in out


def test_almanac_deep_desert_only():
    out = _f_almanac({"sandstorms": {"HaggaBasin": 0, "DeepDesert": 5}})
    assert "**5** sandstorms crossed the Deep Desert" in out
    assert "Hagga" not in out


def test_almanac_singular_grammar():
    out = _f_almanac({"vehicles_now": 1, "structures_delta": 1,
                      "sandstorms": {"HaggaBasin": 1, "DeepDesert": 1}})
    assert "**1** vehicle roams the sands" in out
    assert "grew by **1** build piece." in out
    assert "**1** sandstorm swept Hagga" in out


def test_almanac_hides_absent_and_nonpositive():
    # None facts and a non-positive structures delta are all hidden.
    assert _f_almanac({}) is None
    assert _f_almanac({"flight_km_total": None, "structures_delta": -5,
                       "vehicles_now": None, "worm_breaches": None,
                       "sandstorms": None}) is None


def test_build_embed_weekly_has_almanac():
    weekly = dict(SAMPLE_DAILY)
    weekly["period"] = "weekly"
    weekly["almanac"] = {"flight_km_total": 500.0, "vehicles_now": 12}
    embed = build_embed(weekly)
    names = [f.name for f in embed.fields]
    assert "📜 Desert Almanac" in names


def test_build_embed_daily_no_almanac():
    daily = dict(SAMPLE_DAILY)
    daily["almanac"] = {"flight_km_total": 500.0}
    embed = build_embed(daily)
    names = [f.name for f in embed.fields]
    assert "📜 Desert Almanac" not in names


# --- testing station records -------------------------------------------------

_TS_STATIONS = {
    "first_run": False,
    "stations": [
        {"name": "Testing Station 195", "top_difficulty": 59, "top_this_period": 59,
         "record_runner": "ZetaPlayer", "record_party_size": 3},
        {"name": "Testing Station 152", "top_difficulty": 40, "top_this_period": 0,
         "record_runner": "ZetaPlayer", "record_party_size": 1},
        {"name": "Testing Station 89", "top_difficulty": 40, "top_this_period": 0,
         "record_runner": "EpsilonPlayer", "record_party_size": 4},
    ],
}


def test_testing_stations_orders_by_tier_and_marks_new():
    out = _f_testing_stations(_TS_STATIONS, "weekly")
    lines = out.splitlines()
    assert lines[0].startswith("**Testing Station 195**")
    assert "tier **59**" in lines[0]
    assert lines[0].endswith("\U0001f195") or "\U0001f195" in lines[0]
    # only the station whose record was set this period is flagged
    assert "\U0001f195" not in lines[1]
    assert "record set this week" in out


def test_testing_stations_shows_party_size_not_a_solo_credit():
    """A 4-player clear must never render as one player's solo record."""
    out = _f_testing_stations(_TS_STATIONS, "weekly")
    assert "EpsilonPlayer +3" in out
    assert "ZetaPlayer +2" in out
    # a genuine solo run carries no +N
    assert "ZetaPlayer\n" in out or out.rstrip().endswith("ZetaPlayer")


def test_testing_stations_first_run_suppresses_new_markers():
    """With no cursor baseline every record would otherwise look brand new."""
    out = _f_testing_stations(dict(_TS_STATIONS, first_run=True), "daily")
    assert "\U0001f195" not in out
    assert "record set" not in out


def test_testing_stations_empty_returns_none():
    assert _f_testing_stations({"stations": []}, "daily") is None
    assert _f_testing_stations({}, "weekly") is None
