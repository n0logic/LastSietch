from cielago.cogs.giveaways import (
    Giveaway,
    GiveawayStore,
    group_game_entries,
    ordinal,
    parse_game_entry,
    select_winners,
)


def test_ordinal_basic():
    assert ordinal(1) == "1st"
    assert ordinal(2) == "2nd"
    assert ordinal(3) == "3rd"
    assert ordinal(4) == "4th"
    assert ordinal(21) == "21st"


def test_ordinal_teens_are_th():
    assert ordinal(11) == "11th"
    assert ordinal(12) == "12th"
    assert ordinal(13) == "13th"


def test_parse_game_entry_plain():
    assert parse_game_entry("Conan Exiles") == {"name": "Conan Exiles", "url": None}


def test_parse_game_entry_with_url():
    entry = parse_game_entry("Dune|https://store.steampowered.com/app/1172710")
    assert entry["name"] == "Dune"
    assert entry["url"] == "https://store.steampowered.com/app/1172710"


def test_parse_game_entry_collapses_whitespace():
    assert parse_game_entry("Conan   Exiles")["name"] == "Conan Exiles"


def test_group_game_entries_counts_duplicates():
    entries = [
        {"name": "Conan Exiles", "url": None},
        {"name": "conan exiles", "url": None},
        {"name": "Dune", "url": "u"},
    ]
    grouped = group_game_entries(entries)
    by_name = {g["name"]: g for g in grouped}
    assert by_name["Conan Exiles"]["count"] == 2
    assert by_name["Dune"]["count"] == 1


def test_select_winners_count_and_distinct():
    winners = select_winners({1, 2, 3, 4, 5}, 3)
    assert len(winners) == 3
    assert len(set(winners)) == 3
    assert set(winners) <= {1, 2, 3, 4, 5}


def test_select_winners_caps_at_pool_size():
    assert sorted(select_winners({1, 2}, 5)) == [1, 2]


def test_select_winners_empty_pool():
    assert select_winners(set(), 3) == []


def test_giveaway_round_trip():
    g = Giveaway(
        message_id=123,
        channel_id=456,
        guild_id=789,
        prizes=["A", "B"],
        ends_at=1700000000,
        entrants={11, 22},
        winners=[11],
        ended=True,
        is_weekend=True,
        game_entries=[{"name": "A", "url": None}],
        platform="steam",
        reminder_sent=True,
    )
    restored = Giveaway.from_dict(g.to_dict())
    assert restored == g


def test_store_load_save_round_trip(tmp_path):
    path = tmp_path / "giveaways.json"
    store = GiveawayStore(str(path))
    store.giveaways[123] = Giveaway(
        message_id=123, channel_id=1, guild_id=2, prizes=["X"], ends_at=10, entrants={5}
    )
    store.save()

    reloaded = GiveawayStore(str(path))
    reloaded.load()
    assert 123 in reloaded.giveaways
    assert reloaded.giveaways[123].entrants == {5}


def test_store_load_missing_file_is_empty(tmp_path):
    store = GiveawayStore(str(tmp_path / "nope.json"))
    store.load()
    assert store.giveaways == {}


def test_store_active_excludes_ended():
    store = GiveawayStore("unused")
    store.giveaways[1] = Giveaway(1, 1, 1, ["A"], 10, ended=False)
    store.giveaways[2] = Giveaway(2, 1, 1, ["B"], 10, ended=True)
    active = store.active()
    assert [g.message_id for g in active] == [1]
