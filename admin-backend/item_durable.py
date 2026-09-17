"""Durable-item classification for Dune template_ids.

A template is "durable" if the item has a durability/deterioration component
(weapons, gear/clothing, tools, vehicle parts). Sourced from item_tags in the
awakening.wiki data (Items.Holsters.* / Items.Clothes.*), built by
scripts/build-item-icon-sidecar.py into data/dune-item-durable.json.

Used so the portal can render a never-used durable item (whose per-instance
durability stats are empty until first use) as full, instead of blank, while
keeping resources/consumables meter-free.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SIDECAR = Path(__file__).parent / "data" / "dune-item-durable.json"
_durable: set[str] = set()
_loaded = False


def load() -> int:
    global _loaded
    if _loaded:
        return len(_durable)
    try:
        raw = json.loads(_SIDECAR.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _durable.update(k.lower() for k in raw)
    except FileNotFoundError:
        logger.warning("item_durable: sidecar missing at %s", _SIDECAR)
    except Exception as exc:
        logger.warning("item_durable: load failed: %s", exc)
    _loaded = True
    return len(_durable)


def is_durable(template_id: str) -> bool:
    if not _loaded:
        load()
    return bool(template_id) and template_id.lower() in _durable
