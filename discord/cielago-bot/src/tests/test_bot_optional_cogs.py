import asyncio

from discord.ext import commands

from cielago import bot as bot_mod


def test_setup_survives_a_missing_social_approvals_cog(monkeypatch):
    loaded = []

    async def fake_load(self, name):
        if name.endswith("social_approvals"):
            raise commands.ExtensionNotFound(name)
        loaded.append(name)

    async def fake_sync(*a, **kw):
        return []

    monkeypatch.setattr(bot_mod.Cielago, "load_extension", fake_load)
    b = bot_mod.Cielago()
    monkeypatch.setattr(b.tree, "sync", fake_sync)
    monkeypatch.setattr(b.tree, "copy_global_to", lambda **kw: None)
    asyncio.run(b.setup_hook())
    assert "cielago.cogs.contests" in loaded
    assert not any(n.endswith("social_approvals") for n in loaded)
