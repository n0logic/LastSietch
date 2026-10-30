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


# --- Founders mode ---

import asyncio  # noqa: E402
import json  # noqa: E402

from cielago.cogs.giveaways import normalize_friend_code  # noqa: E402


def test_friend_code_digits_and_spacing():
    assert normalize_friend_code("123456789") == "123456789"
    assert normalize_friend_code(" 1234 5678-9 ") == "123456789"


def test_friend_code_profile_links():
    assert normalize_friend_code("https://steamcommunity.com/id/example-player/") is not None
    assert normalize_friend_code("steamcommunity.com/profiles/" + "7656119" + "0" * 10) is not None


def test_friend_code_rejects_junk():
    assert normalize_friend_code("") is None
    assert normalize_friend_code("1234") is None
    assert normalize_friend_code("12345678901") is None
    assert normalize_friend_code("me@example.com") is None
    assert normalize_friend_code("https://evil.example/steamcommunity.com/id/x") is None


def test_founders_round_trip_keeps_codes_and_role():
    g = Giveaway(
        message_id=1,
        channel_id=2,
        guild_id=3,
        prizes=["DLC"] * 5,
        ends_at=10,
        entrants={7, 8},
        founders_role_id=99,
        starter_id=42,
        codes={7: "123456789"},
    )
    back = Giveaway.from_dict(json.loads(json.dumps(g.to_dict())))
    assert back.founders_role_id == 99
    assert back.starter_id == 42
    assert back.codes == {7: "123456789"}


def test_old_records_load_without_founders_fields():
    g = Giveaway.from_dict(
        {"message_id": 1, "channel_id": 2, "guild_id": 3, "prizes": ["A"], "ends_at": 5}
    )
    assert g.founders_role_id is None and g.codes == {}


def test_founders_draw_discards_non_winner_codes_and_dms_winners():
    from cielago.cogs.giveaways import Giveaways

    sent_channel, sent_dm = [], []

    class Chan:
        guild = type("G", (), {"get_member": staticmethod(lambda uid: None)})()

        async def send(self, text, **kw):
            sent_channel.append(text)

    class User:
        async def send(self, text):
            sent_dm.append(text)

    class Bot:
        def get_user(self, uid):
            return User()

    cog = Giveaways.__new__(Giveaways)
    cog.bot = Bot()
    cog.store = type("S", (), {"save": lambda self: None})()
    g = Giveaway(
        message_id=1,
        channel_id=2,
        guild_id=3,
        prizes=["DLC"] * 2,
        ends_at=10,
        entrants={1, 2, 3},
        winners=[1, 3],
        founders_role_id=99,
        starter_id=42,
        codes={1: "111111", 2: "222222", 3: "333333"},
    )
    asyncio.run(cog._finish_founders(g, Chan()))
    assert g.codes == {1: "111111", 3: "333333"}
    assert "222222" not in sent_dm[0]
    assert "111111" in sent_dm[0] and "333333" in sent_dm[0]
    assert "111111" not in sent_channel[0]
