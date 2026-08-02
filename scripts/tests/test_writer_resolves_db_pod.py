#!/usr/bin/env python3
"""Repo-wide guard: a writer that DEFINES resolve_db_pod must also CALL it.

🔴 WHY THIS EXISTS. `scripts/dune-karum-op.sh` defined `resolve_db_pod()`, which is the only
thing that sets `DB_NS` and `DB_POD`, and never called it. Every writer runs under `set -u`, so
the first `run_psql` died with:

    /root/dune-karum-op.sh: line 203: DB_NS: unbound variable
    /root/dune-karum-op.sh: line 205: DB_POD: unbound variable

exit 6, before opening a transaction. **Every apply-mode write failed, 100% of the time.**

It survived a 22-test suite, a six-step deploy script with four preflights, and a live
deployment, because all of those exercise `--dry-run`, which returns before any pod is needed.
The writer also shipped DARK, so apply mode was never once executed until the owner clicked LIST
on a live venue. The dry-run path and the apply path diverge at exactly the line that matters.

**The lesson this file encodes:** a code path that only executes in production is not tested by
a suite that never takes it. Where a script has a dry-run branch, something must assert the
non-dry branch is wired up too.

Prod-safe: reads repo files only, no host contact.

Run:  python3 scripts/tests/test_writer_resolves_db_pod.py     (also import-safe)
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)

SKIP_DIRS = {".git", "node_modules", "__pycache__", "build", ".svelte-kit", "venv"}

DEF_RE = re.compile(r"^\s*resolve_db_pod\s*\(\)\s*\{", re.M)


def _strip_comments(src):
    """Remove comment text so a MENTION of resolve_db_pod cannot pass for a CALL.

    🔴 The first version of this guard matched the bare name anywhere on a non-`#`-leading line.
    Its own negative test then passed when it should have failed, because the synthesised break
    left the name behind in a trailing comment (`: # resolve_db_pod REMOVED`). A guard that a
    comment can satisfy is not a guard. Naive on quoting by design -- these writers do not put
    '#' inside strings on the lines that matter, and over-stripping can only cause a false
    ALARM here, never a false pass.
    """
    out = []
    for line in src.splitlines():
        code = line.split("#", 1)[0]
        out.append(code)
    return "\n".join(out)


def _calls(src):
    """Real call sites: the name used as a command, with the definition line excluded."""
    code = _strip_comments(src)
    hits = []
    for ln in code.splitlines():
        if "resolve_db_pod" not in ln:
            continue
        if re.match(r"\s*resolve_db_pod\s*\(\)", ln):      # the definition itself
            continue
        # must appear as a command, not as part of a longer identifier
        if re.search(r"(^|[;&|(]|\s)resolve_db_pod(\s|$|;|&|\))", ln):
            hits.append(ln.strip())
    return hits


def _walk():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".sh"):
                yield os.path.join(root, f)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _definers():
    out = {}
    for path in _walk():
        src = _read(path)
        if DEF_RE.search(src):
            out[os.path.relpath(path, REPO)] = src
    return out


def test_every_definer_also_calls_it():
    offenders = []
    for rel, src in _definers().items():
        if not _calls(src):
            offenders.append(rel)
    assert not offenders, (
        "these writers define resolve_db_pod but never call it. Under `set -u` the first\n"
        "run_psql will die with 'DB_NS: unbound variable' (exit 6) and EVERY apply-mode write\n"
        "will fail, while --dry-run keeps passing because it returns before needing a pod:\n"
        + "\n".join(f"  {r}" for r in offenders))


def test_at_least_the_three_known_writers_are_covered():
    """Pins the guard to real files, so a rename cannot quietly empty this suite."""
    found = set(_definers())
    expect = {
        "scripts/dune-karum-op.sh",
        "scripts/dune-gift-op.sh",
        "scripts/dune-item-transfer-op.sh",
    }
    still_present = {e for e in expect if os.path.isfile(os.path.join(REPO, e))}
    missing = still_present - found
    assert not missing, (
        f"these writers no longer define resolve_db_pod; if the DB access pattern changed, "
        f"update this guard rather than deleting it: {sorted(missing)}")
    assert len(found) >= len(still_present), "definer discovery regressed"


def test_karum_resolves_inside_emit_or_run_not_per_call_site():
    """The Karum fix deliberately resolves in `emit_or_run`, the single choke point for all six
    write sites, guarded so karum-buy's two transactions resolve once. Resolving per call site
    invites the seventh site to forget, which is how this bug happened."""
    path = os.path.join(REPO, "scripts", "dune-karum-op.sh")
    if not os.path.isfile(path):
        return
    src = _read(path)
    body_m = re.search(r"^emit_or_run\(\)\s*\{(.*?)^\}", src, re.S | re.M)
    assert body_m, "could not find emit_or_run in dune-karum-op.sh"
    body = body_m.group(1)
    # Comment-stripped, so a mention in a comment cannot satisfy this.
    code = _strip_comments(body)
    assert _calls(body), (
        "emit_or_run no longer CALLS resolve_db_pod (a mention in a comment does not count). "
        "Every apply-mode write depends on it and --dry-run will not catch its absence.")
    assert "DRY_RUN" in code and code.index("DRY_RUN") < code.index("resolve_db_pod"), (
        "the dry-run early return must come BEFORE resolve_db_pod, or --dry-run stops working "
        "off-box and the test suite (which asserts on emitted SQL) breaks.")
    assert re.search(r'-z\s+"\$\{DB_POD:-\}"', code), (
        "the resolve must be guarded on an unset/empty DB_POD so karum-buy's two transactions "
        "resolve once rather than shelling out to kubectl twice.")


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    failures = 0
    for fn in _all_tests():
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(_all_tests()) - failures}/{len(_all_tests())} passed")
    raise SystemExit(1 if failures else 0)
