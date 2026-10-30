#!/usr/bin/env python3
"""Register routers.v2_events in a lastsietch-admin main.py, in place.

Usage: patch_main.py <main.py> [--check]

The live <web-host> main.py trails origin/main (drift bucket B), so the Events
Manager lane patches this one hunk in instead of shipping the repo copy.
Idempotent; the anchor must match exactly once or nothing is written. Applied to
the origin/main copy it reproduces the branch file (pinned in test_v2_events.py).
"""
from pathlib import Path
import sys

# The block must sit before the v2_player include: its /v2/players/{account_id}
# catch-all otherwise answers /v2/players/events with 400 "account_id must be a
# positive integer" (the 2026-10-27 bug). OLD_BLOCK is what earlier runs left
# after the portal_events guard; patch() moves it.
ANCHOR = 'app.include_router(v2_player.router)\n'
OLD_BLOCK = '''# Events Manager (ADMIN-02): the admin-panel door onto the same event store.
try:
    from routers import v2_events
    app.include_router(v2_events.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.v2_events absent; /v2/players/events not registered")
'''
BLOCK = '''# Events Manager (ADMIN-02): the admin-panel door onto the same event store.
# /v2/players/events must also register before the v2_player catch-all.
try:
    from routers import v2_events
    app.include_router(v2_events.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.v2_events absent; /v2/players/events not registered")
'''


def patch(text):
    if BLOCK in text:
        return text
    if text.count(OLD_BLOCK) > 1:
        raise SystemExit('REFUSE: old block matched %d times' % text.count(OLD_BLOCK))
    text = text.replace(OLD_BLOCK, '')
    if text.count(ANCHOR) != 1:
        raise SystemExit('REFUSE: anchor matched %d times' % text.count(ANCHOR))
    return text.replace(ANCHOR, BLOCK + ANCHOR)


def main():
    path = Path(sys.argv[1])
    before = path.read_text()
    after = patch(before)
    if after == before:
        print('  %s: already patched' % path)
    elif '--check' in sys.argv:
        print('  %s: would patch' % path)
    else:
        path.write_text(after)
        print('  %s: patched' % path)


if __name__ == '__main__':
    main()
