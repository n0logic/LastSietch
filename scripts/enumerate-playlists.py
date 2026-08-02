#!/usr/bin/env python3
# enumerate-playlists.py — dump all Tidal playlist URLs (owned + favorited) to stdout.
# Stats + per-playlist breakdown go to stderr so stdout is clean for `tidaler dl --list`.
#
# Usage:
#   ~/.venvs/tidaler/bin/python scripts/enumerate-playlists.py > playlists.txt
#   ~/.local/bin/tidaler-sync dl --list playlists.txt

import json
import sys
from pathlib import Path

import tidalapi

TOKEN_FILE = Path.home() / ".config/tidaler/token.json"


def main():
    t = json.loads(TOKEN_FILE.read_text())
    s = tidalapi.Session()
    s.load_oauth_session(t["token_type"], t["access_token"], t["refresh_token"])
    if not s.check_login():
        sys.exit("not logged in — run: ~/.venvs/tidaler/bin/tidaler login")

    owned = list(s.user.playlists())
    all_with_favs = list(s.user.playlist_and_favorite_playlists())
    owned_ids = {p.id for p in owned}
    favorites = [p for p in all_with_favs if p.id not in owned_ids]

    print(f"Owned playlists:     {len(owned)}", file=sys.stderr)
    print(f"Favorited playlists: {len(favorites)}", file=sys.stderr)
    print(f"Total:               {len(owned) + len(favorites)}", file=sys.stderr)
    print("-- breakdown --", file=sys.stderr)
    for p in owned:
        print(f"OWNED  {p.num_tracks:>4} tracks  {p.name}", file=sys.stderr)
    for p in favorites:
        print(f"FAV    {p.num_tracks:>4} tracks  {p.name}", file=sys.stderr)

    for p in owned + favorites:
        print(f"https://tidal.com/browse/playlist/{p.id}")


if __name__ == "__main__":
    main()
