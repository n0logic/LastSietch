from cielago.cogs.channels import load_layout
from cielago.cogs.voice import is_trigger_name, temp_channel_name


def test_is_trigger_name_matches_composed_voice_name():
    # Voice channels keep case; the composed display name is "➕｜Join to Create".
    assert is_trigger_name("➕｜Join to Create")
    assert is_trigger_name("Join to Create")


def test_is_trigger_name_rejects_others():
    assert not is_trigger_name("🔊｜Lounge")
    assert not is_trigger_name("Gaming")


def test_temp_channel_name_uses_game_when_present():
    assert temp_channel_name("the owner", "Conan Exiles") == "Conan Exiles"


def test_temp_channel_name_falls_back_to_owner():
    assert temp_channel_name("the owner", None) == "the owner's Channel"


def test_temp_channel_name_truncates_to_100():
    assert len(temp_channel_name("x", "g" * 200)) == 100


def test_layout_has_join_to_create_trigger():
    layout = load_layout()
    voice = next(c for c in layout["categories"] if c["name"] == "VOICE")
    names = [ch["name"] for ch in voice["channels"]]
    assert "Join to Create" in names
