import os

# config.py builds Settings() at import time, requiring these. Set dummies so the
# package imports without a real .env during tests.
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
os.environ.setdefault("LAST_SIETCH_GUILD_ID", "<discord-id>")
