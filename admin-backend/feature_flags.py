"""Request-time feature gates for the admin panel and the portal.

Three layers, highest first:

  1. the override file, data/feature_flags.json beside this module. Written by
     the owner-only Systems toggle, read fresh on EVERY call. It is never
     memoised: the whole point is that flipping a gate takes effect on the next
     request with no restart, and a cache would make a live toggle a lie.
  2. the process environment, which is what the service unit resolved at boot.
  3. the coded default, written the way the environment writes it ("0" / "1")
     or as a bool. The string form is deliberate: the coded default of a gate is
     pinned by grep in ops/deploy-karum.sh and in the augment suite, and those
     guards keep working only while the literal survives in the caller.

A missing, unreadable or corrupt override file falls through to the environment
rather than failing the request. A gate that cannot read its override is a gate
answering with what the process was started with, which is the pre-override
behaviour and is always a safe answer.

The file is written the way dd_layout_match's override is: a tmp file in the
SAME directory, fsync, chmod, then os.replace. Never open(target, "w") -- a
truncated file here is a live player read.
"""
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent
_RUNTIME = Path(os.environ.get("LASTSIETCH_PORTAL_RUNTIME_DIR", str(_HERE / "data")))
_OVERRIDE = _RUNTIME / "feature_flags.json"

# Only LASTSIETCH_-prefixed upper-case settables can be overridden. The route that
# calls set_override carries its own allowlist of the nine gates; this is the
# floor under it, so no caller can write an arbitrary key into the file.
_NAME_RE = re.compile(r"^LASTSIETCH_[A-Z0-9_]+$")


def _as_bool(value) -> bool:
    """A default (or an override) written either as a bool or the way the
    environment writes it."""
    if isinstance(value, bool):
        return value
    return str(value).strip() == "1"


def overrides() -> dict:
    """The override file, re-read on every call. Anything unparseable, any
    non-dict, and any entry that is not name -> bool is dropped rather than
    trusted."""
    try:
        raw = json.loads(_OVERRIDE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("feature_flags: override read failed: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items()
            if isinstance(k, str) and _NAME_RE.match(k) and isinstance(v, bool)}


def env_value(name: str):
    """What the process environment says, or None when it says nothing."""
    raw = os.environ.get(name)
    return None if raw is None else (raw.strip() == "1")


def enabled(name: str, default=False) -> bool:
    """The gate answer: override, then process env, then the coded default."""
    over = overrides().get(name)
    if isinstance(over, bool):
        return over
    env = env_value(name)
    if env is not None:
        return env
    return _as_bool(default)


def set_override(name: str, value) -> dict:
    """Set (value True/False) or clear (value None) one override and return the
    whole file as it now stands. Atomic: same-directory tmp, fsync, chmod 0640,
    os.replace. Raises ValueError on a name this module will not write."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError("flag name must look like LASTSIETCH_SOMETHING")
    data = overrides()
    if value is None:
        data.pop(name, None)
    else:
        data[name] = bool(value)

    _OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _OVERRIDE.parent / ("." + _OVERRIDE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    # chmod the tmp, not the target: the file must never exist at the real path
    # with the wrong mode, not even for the width of one syscall.
    os.chmod(tmp, 0o640)
    os.replace(tmp, _OVERRIDE)
    return data


def snapshot(specs) -> list:
    """Per-flag {name, env, override, effective} for the board. `specs` is an
    iterable of (name, default) pairs; every value is computed by the same
    functions the gates themselves call, so the board cannot drift from the
    gate it claims to describe."""
    over = overrides()
    rows = []
    for name, default in specs:
        rows.append({
            "name": name,
            "env": env_value(name),
            "env_set": name in os.environ,
            "default": _as_bool(default),
            "override": over.get(name) if isinstance(over.get(name), bool) else None,
            "effective": enabled(name, default),
        })
    return rows


def override_path() -> str:
    return str(_OVERRIDE)
