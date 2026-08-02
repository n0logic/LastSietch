from cielago.cogs.channels import channel_name, load_layout


def test_channel_name_composition():
    assert channel_name("🏜️", "dune-general", "｜") == "🏜️｜dune-general"


def test_layout_loads_with_separator():
    layout = load_layout()
    assert layout["separator"] == "｜"
    assert layout["categories"]


def test_deep_desert_channel_present():
    layout = load_layout()
    cats = {c["name"]: c for c in layout["categories"]}
    assert "DUNE: AWAKENING" in cats
    names = [ch["name"] for ch in cats["DUNE: AWAKENING"]["channels"]]
    assert "deep-desert" in names


def test_text_channel_names_are_normalized():
    # Discord lowercases + hyphenates text channel names; pre-normalizing keeps
    # /channel-template idempotent (the "already exists" match is exact).
    layout = load_layout()
    for cat in layout["categories"]:
        for ch in cat["channels"]:
            if ch.get("type") != "voice":
                assert ch["name"] == ch["name"].lower()
                assert " " not in ch["name"]


def test_admin_category_is_private():
    layout = load_layout()
    admin = next(c for c in layout["categories"] if c["name"] == "ADMIN")
    assert admin["private"] is True
