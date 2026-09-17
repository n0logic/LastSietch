"""Compat shim — superseded by name_lookups.py (LIFT-11, 2026-05-25).

All ITEMS-namespace lookups now delegate to name_lookups.ITEMS. Existing
call sites (v2_player.py etc.) keep working unchanged. New code SHOULD
import from name_lookups directly to access the per-namespace sidecars
(BUILDINGS, SKILLS, LORE, PROGRESSION, COMMUNINET).
"""
from name_lookups import ITEMS

load_sidecar = ITEMS.load
lookup = ITEMS.lookup
size = ITEMS.size
