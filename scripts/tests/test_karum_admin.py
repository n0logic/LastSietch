#!/usr/bin/env python3
"""Regression tests for the Karum operator page and the audit transport.

Prod-safe: source-scan plus a real Jinja render, no network, no DB. `routers/v2_karum.py`
pulls the FastAPI dependency chain and is not importable standalone here, same as every
other admin router in this suite.

The template IS rendered, because a broken admin template fails at request time rather than
at import time, and the one moment nobody wants to discover that is while resolving a stuck
trade.

What this covers that a working page would not reveal on its own:
  * refund is a SEPARATE action from force-return, and nothing bundles them;
  * every button routes through the relay, so nothing reaches the game DB directly;
  * a reason is mandatory, because the log is a dispute trail;
  * the audit is a GET (read-only, freeze-safe) and a FAILED audit is never rendered clean;
  * identity comes from the listing row, never from the operator's request body.

Run:  python3 scripts/tests/test_karum_admin.py     (also import-safe)
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
BACKEND = os.path.join(REPO, "admin-backend")

ROUTER = os.path.join(BACKEND, "routers", "v2_karum.py")
TEMPLATE = os.path.join(BACKEND, "templates", "v2", "karum.html")
SUBNAV = os.path.join(BACKEND, "templates", "v2", "_includes", "portal_subnav.html")
BASE_TPL = os.path.join(BACKEND, "templates", "v2", "base.html")
MAIN = os.path.join(BACKEND, "main.py")
RELAY = os.path.join(REPO, "relay", "app.py")
DISPATCH = os.path.join(SCRIPTS, "dune-relay-dispatch.sh")
AUDIT = os.path.join(SCRIPTS, "dune-karum-audit.py")
RUNNER = os.path.join(REPO, "ops", "karum-audit", "karum-audit-run.sh")
UNIT = os.path.join(REPO, "ops", "karum-audit", "lastsietch-karum-audit.timer")
READMEF = os.path.join(REPO, "ops", "karum-audit", "README.md")
DEPLOY = os.path.join(REPO, "ops", "deploy-karum.sh")
WRITER = os.path.join(SCRIPTS, "dune-karum-op.sh")
PORTAL = os.path.join(BACKEND, "routers", "portal.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def test_router_registered_and_page_is_admin_only():
    main = _read(MAIN)
    assert "v2_karum" in main
    assert "app.include_router(v2_karum.router)" in main
    src = _read(ROUTER)
    # 🔴 Registered WITHOUT the /admin prefix on purpose. The public /admin/* prefix is stripped
    # by the proxy before the request reaches this app, so a route registered WITH it is only
    # reachable at /admin/admin/... and 404s for every real visitor. That was a real bug, fixed
    # in `fc179fb`; this assertion was written against the broken form and outlived it.
    assert '@router.get("/v2/portal/karum")' in src
    assert '@router.get("/admin/v2/portal/karum")' not in src, \
        "re-adding the /admin prefix makes the page 404 for every real visitor (see fc179fb)"
    # the page redirects, the JSON routes require_admin
    assert "_admin_or_redirect(request)" in src
    for route in ('@router.get("/api/dune/v2/karum/listings")',
                  '@router.get("/api/dune/v2/karum/audit")',
                  '@router.post("/api/dune/v2/karum/force")'):
        assert route in src, route
    body = src[src.index("async def v2_karum_listings"):]
    assert "require_admin(request)" in body


def test_page_lives_on_the_admin_panel_only():
    """admin.lastsietch.com only, never the player portal: this surface names accounts and
    exposes escrow internals."""
    src = _read(ROUTER)
    # The player portal is served at /portal/*; this operator page lives at /v2/portal/*. Check
    # for a BARE player-portal registration rather than the substring, which /v2/portal/karum
    # legitimately contains -- the earlier version stripped "/admin/v2/portal/karum" and so
    # started failing the moment that prefix was correctly removed.
    import re as _re
    bare = _re.findall(r'@router\.(?:get|post)\("/portal/[^"]*"\)', src)
    assert not bare, f"the operator page must not register a player-portal path: {bare}"
    subnav = _read(SUBNAV)
    assert 'href="/admin/v2/portal/karum"' in subnav
    assert "{% elif current_tab == 'portal' %}" in _read(BASE_TPL)


# --------------------------------------------------------------------------- #
# The three actions
# --------------------------------------------------------------------------- #

def test_refund_is_a_separate_action_never_bundled():
    """🔴 A return with no refund is right when the buyer never paid; a refund with no return
    is right when the goods already reached them. Bundling makes one of those wrong every
    time."""
    src = _read(ROUTER)
    assert '_FORCE_ACTIONS = ("force-deliver", "force-return", "refund")' in src
    # the force-return branch must not also adjust a balance
    ret = src[src.index('if action == "refund":'):]
    ret = ret[:ret.index("ip = client_ip(request)")]
    assert '"admin_action": "refund"' in ret
    # and the template must confirm them separately
    tpl = _read(TEMPLATE)
    assert "does NOT refund the" in tpl
    assert "does NOT move the goods" in tpl


def test_every_action_goes_through_the_relay():
    """The page is a FOURTH caller of the same chain, not a shortcut into the DB."""
    src = _read(ROUTER)
    assert 'call_relay("/dune/karum/admin", method="POST"' in src
    # no direct game-DB access anywhere in this router
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    for forbidden in ("kubectl", "psql", "dune.items", "dune.ls_karum_escrow",
                      "adjust_player_virtual_currency_balance"):
        assert forbidden not in code, f"the admin router reaches for {forbidden} directly"


def test_reason_is_mandatory_and_logged_with_the_operator():
    src = _read(ROUTER)
    assert "a reason of at least 4 characters is required" in src
    assert 'detail = {"operator": operator, "reason": reason}' in src
    # both the append-only trail AND the admin audit log
    assert "portal_karum_events" in src
    assert "audit_log(" in src
    # A FAILED press is logged too, not just a successful one. Scoped to the force route:
    # the first `except HTTPException` in the file belongs to the audit route, and slicing
    # from there made this assertion test the wrong function.
    force = src[src.index("async def v2_karum_force"):]
    unavailable = force[force.index("except HTTPException as exc:"):]
    assert "_record(" in unavailable[:600], "a failed press is not written to the trail"
    assert "success=False" in unavailable[:900]


def test_identity_comes_from_the_listing_not_the_body():
    """An operator says WHICH listing to resolve, never WHO to pay."""
    src = _read(ROUTER)
    force = src[src.index("async def v2_karum_force"):]
    assert 'int(row["buyer_account_id"])' in force
    assert 'int(row["seller_account_id"])' in force
    for client_supplied in ('body.get("buyer_account_id")', 'body.get("seller_account_id")',
                            'body.get("target_account_id")', 'body.get("amount")'):
        assert client_supplied not in force, f"the body can set {client_supplied}"


def test_listing_mirror_only_on_a_confirmed_write():
    src = _read(ROUTER)
    assert 'good = status in ("applied", "replay")' in src
    assert "if not good:" in src
    # the deferred (DARK) case must not move the listing either
    assert 'if status == "deferred":' in src
    dark = src[src.index('if status == "deferred":'):]
    assert "UPDATE portal_karum_listings" not in dark[:400]


def test_page_names_idempotency_so_a_retry_is_obviously_safe():
    """The operator is going to be doing this under pressure. 'Safe to press again' has to be
    on the screen, not only in a docstring."""
    tpl = _read(TEMPLATE)
    assert "idempotent" in tpl
    assert "Safe to repeat" in tpl or "safe to press again" in tpl.lower()
    assert "hand-written SQL" in tpl


# --------------------------------------------------------------------------- #
# The audit
# --------------------------------------------------------------------------- #

def test_audit_is_read_only_end_to_end():
    aud = _read(AUDIT)
    code = "\n".join(l.split("--", 1)[0] for l in
                     aud[aud.index("AUDIT_SQL"):aud.index('"""\n\n\ndef run_sql')].splitlines())
    for w in ("INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE", "CREATE ", "ALTER "):
        assert w not in code.upper(), f"the audit SQL contains {w}"
    # exposed as a GET, which is what makes it freeze-safe
    assert '@app.get("/dune/karum/audit"' in _read(RELAY)
    assert '@router.get("/api/dune/v2/karum/audit")' in _read(ROUTER)
    # and it never repairs
    assert "never repair" in aud.lower() or "It never repairs" in aud


def test_audit_never_reports_a_failure_as_clean():
    """🔴 For an audit whose subject fails SILENTLY, 'we could not look' and 'we looked and it
    was fine' must not be confusable. That is what the third exit code is for."""
    aud = _read(AUDIT)
    assert "RC_OK, RC_PAGE, RC_BROKEN = 0, 1, 3" in aud
    assert "NOT a clean result" in aud
    assert "return RC_BROKEN" in aud
    # an OSError launching dq.sh must be caught: PermissionError is not FileNotFoundError,
    # and letting it escape crashed with exit 1, which is also the PAGE code.
    assert "except OSError as exc:" in aud

    relay = _read(RELAY)
    seg = relay[relay.index('@app.get("/dune/karum/audit"'):]
    seg = seg[:seg.index("@app.post")]
    assert '"page": True' in seg
    assert "if code not in (0, 1):" in seg, "a broken exit code must force page"

    router = _read(ROUTER)
    seg2 = router[router.index("async def v2_karum_audit"):router.index("async def v2_karum_force")]
    assert seg2.count('"page": True') >= 3, "every audit failure path must set page"

    tpl = _read(TEMPLATE)
    assert "NOT a clean result" in tpl


def test_audit_dispatcher_token_takes_no_payload():
    src = _read(DISPATCH)
    assert "karum-audit)" in src
    seg = src.split("karum-audit)", 1)[1].split(";;", 1)[0]
    assert "/root/dune-karum-audit.py" in seg
    assert "takes no args" in seg
    # read-only, so unlike karum-op it needs no base64 payload guard at all
    assert "base64" not in seg


def test_audit_reports_orphans_but_never_acts_on_them():
    aud = _read(AUDIT)
    assert "unmarked_orphans" in aud
    assert "REPORT ONLY" in aud or "report only" in aud
    readme = _read(READMEF)
    assert "without excluding Karum-marked rows" in readme
    assert "acquisition_time" in readme, "the not-the-discriminator warning must be recorded"


def test_audit_knows_about_the_in_person_trade_window():
    """Found 2026-07-27 from P2pTradingInventoryStartingSize=10: type 20 holds zero rows at
    rest, so a row there is a LOADED session, which is the worst place an escrowed item could
    turn up."""
    aud = _read(AUDIT)
    assert "in_live_trade_window" in aud
    assert "actual_type = 20" in aud
    assert "IN-PERSON TRADE WINDOW" in aud
    assert "inventory_type = 20" in _read(READMEF)


def test_nightly_runner_lives_on_web_host_and_notifies():
    """<game-host> cannot resolve the web host, so the runner has to live where both the relay and
    the notifier are."""
    run = _read(RUNNER)
    assert "RUNS ON WEB_HOST" in run
    assert "/dune/karum/audit" in run
    # It notifies through a LOCAL notifier, not the shared cielago-notify.sh: that one exists
    # to ssh INTO the web host from a host with no token, so a the web host-resident runner using
    # it would ssh to itself.
    assert "karum-notify-local.sh" in run
    local = _read(os.path.join(REPO, "ops", "karum-audit", "karum-notify-local.sh"))
    assert "ssh " not in local, "the local notifier must not ssh anywhere"
    assert "WHY THIS IS NOT" in local, "the reason for not reusing the shared script must be recorded"
    assert "AllowedMentions.none()" in local, "an ops post must never ping a channel"
    # a broken audit notifies as loudly as a finding
    assert "COULD NOT RUN" in run
    assert "RC_BROKEN" in run
    timer = _read(UNIT)
    assert "OnCalendar=" in timer and "Persistent=true" in timer


def test_runner_authenticates_the_way_the_relay_expects():
    """Found on the first real run, 2026-07-27: the relay declares `x_api_key: str = Header()`,
    so a MISSING or wrong header is a 422 from FastAPI's validation layer, not a 401. A 422
    reads like a broken route and sends the next person hunting in the wrong place, so the
    header name and the key's source are both pinned here."""
    run = _read(RUNNER)
    relay = _read(RELAY)
    assert "x_api_key: str = Header()" in relay, \
        "the relay's auth signature changed; re-check what the runner must send"
    assert 'LASTSIETCH_RELAY_API_KEY' in relay
    assert '-H "X-API-Key: ${KEY}"' in run, "the runner must send X-API-Key"
    assert "X-Relay-Key" not in run, "X-Relay-Key is not a header the relay reads"
    assert "LASTSIETCH_RELAY_API_KEY" in run, "the runner must read the key the relay actually uses"
    # an absent key must be reported as an absent key, not left to surface as a 422
    assert "no relay API key" in run


def test_runner_does_not_go_quiet_forever_on_clean_runs():
    """Silence has to be unambiguous: a channel that says nothing for a month could mean
    clean, or could mean the timer died."""
    run = _read(RUNNER)
    assert "CLEAN_HEARTBEAT_DAYS" in run
    assert "last-clean-post" in run


# --------------------------------------------------------------------------- #
# The template actually renders
# --------------------------------------------------------------------------- #

def test_template_renders_with_real_rows():
    try:
        from jinja2 import Environment, FileSystemLoader
    except ImportError:  # pragma: no cover
        return
    env = Environment(loader=FileSystemLoader(os.path.join(BACKEND, "templates")),
                      autoescape=True)
    t = env.get_template("v2/karum.html")

    class FakeReq:
        scope = {"type": "http", "root_path": ""}
        url = type("U", (), {"path": "/admin/v2/portal/karum"})()

    rows = [dict(listing_id=4711, seller_name="Sandrider", seller_account_id=1001,
                 buyer_account_id=2002, template_id="IronBar", display_name="Iron Bar",
                 stack_size=500, quality_level=2, price=12500, status="paid_undelivered",
                 escrow_item_id=4242, escrow_corr_id="c1", sold_corr_id="c2",
                 created_at="x", updated_at="y", sold_at=None, hours_in_state=3),
            dict(listing_id=4712, seller_name="Nadia", seller_account_id=1002,
                 buyer_account_id=None, template_id="MelangeSpice", display_name="Melange",
                 stack_size=20, quality_level=0, price=900, status="active",
                 escrow_item_id=4243, escrow_corr_id="c3", sold_corr_id=None,
                 created_at="x", updated_at="y", sold_at=None, hours_in_state=5)]
    html = t.render(request=FakeReq(), user={"username": "d", "role": "admin"},
                    current_tab="portal", current_sub_tab="karum", listings=rows)
    for want in ("Force deliver", "Force return", "Refund buyer", "Run the audit",
                 "kr-badge--paid_undelivered", "12,500"):
        assert want in html, want
    # the guarded buttons render disabled: no buyer -> no deliver, no payment -> no refund
    assert html.count("disabled") >= 2

    # and the empty state is a sentence, not a blank table
    empty = t.render(request=FakeReq(), user={"username": "d", "role": "admin"},
                     current_tab="portal", current_sub_tab="karum", listings=[])
    assert "Nothing stuck" in empty


# --------------------------------------------------------------------------- #
# The deploy script's own safety properties
# --------------------------------------------------------------------------- #

def test_deploy_refuses_if_the_dark_default_was_edited():
    """🔴 A flag flip must be a deliberate, separate act. If someone edited the default to 1
    and then ran a deploy, the venue would open with no QA, no LT-0 and no self-test. The
    guard is a grep, so it only works while the pattern and the code agree: this test is what
    keeps them agreeing."""
    dep = _read(DEPLOY)
    guards = [
        ('LASTSIETCH_KARUM_ENABLED="${LASTSIETCH_KARUM_ENABLED:-0}"', WRITER),
        ('LASTSIETCH_KARUM_STATS_SENTINEL="${LASTSIETCH_KARUM_STATS_SENTINEL:-0}"', WRITER),
        ('LASTSIETCH_KARUM_ENABLED", "0"', PORTAL),
    ]
    for pattern, target in guards:
        assert pattern in dep, f"the deploy script no longer guards {pattern!r}"
        assert pattern in _read(target), (
            f"the deploy guard greps for {pattern!r} but {os.path.basename(target)} no longer "
            f"contains it, so the guard would abort every deploy (or worse, was silently "
            f"loosened)")
    # and it must actually abort, not warn
    seg = dep[dep.index("the DARK defaults must be intact"):]
    seg = seg[:seg.index("Preflight 3")]
    assert seg.count("|| die") >= 3, "the dark-default guards must die, not warn"


def test_deploy_never_touches_pods_bgd_or_k3s():
    dep = _read(DEPLOY)
    code = "\n".join(l for l in dep.splitlines() if not l.lstrip().startswith("#"))
    # COMMAND patterns only. Bare words like "k3s" and "battlegroup" appear legitimately in
    # the script's own safety prose and in its closing NEVER-do-this list, and asserting on
    # those made this test fail on a script that was correct.
    for forbidden in ("kubectl delete", "kubectl rollout", "kubectl scale", "kubectl apply",
                      "kubectl patch", "kubectl edit"):
        assert forbidden not in code, f"the deploy script reaches for `{forbidden}`"
    # The precise version of the same invariant: the ONLY services it restarts are safe ones.
    # Game pods, the BGD and k3s are not in this set and cannot be, so this is the assertion
    # that actually holds the line.
    import re as _re
    restarts = set(_re.findall(r"systemctl (?:restart|start) ([a-z0-9.-]+)", code))
    assert restarts <= {"lastsietch-relay", "lastsietch-admin", "lastsietch-karum-audit.service"}, \
        f"unexpected service restarts: {restarts}"
    assert "systemctl restart" in dep, "test premise wrong: nothing is restarted at all"


def test_deploy_ships_the_writer_and_its_library_together():
    """A writer without its shared take fails closed, but it fails, and the offline gate lives
    in that library."""
    dep = _read(DEPLOY)
    assert "scripts/dune-karum-op.sh" in dep
    assert "scripts/lib/dune-take-item.sh" in dep
    assert "<game-host>:/root/lib/" in dep
    # and it proves the writer can load it, on the box
    assert "writer resolves /root/lib/dune-take-item.sh" in dep


def test_deploy_verifies_through_the_executed_path():
    """⚠️ The /root-versus-/opt dual-copy trap produced a false-positive verify on 2026-06-10.
    'The file is there' is not 'the thing the dispatcher runs behaves'."""
    dep = _read(DEPLOY)
    assert "EXECUTED path" in dep
    assert "dispatcher execs the deployed paths" in dep
    # DARK is verified through the real entry point, not assumed from the source
    assert 'DARK verified through the writer' in dep
    # and the ledger ownership is proven, not trusted
    assert "tableowner" in dep and "OWNER dune" in dep


def test_deploy_has_a_dry_run_and_a_red_suite_stops_it():
    dep = _read(DEPLOY)
    assert '--dry-run' in dep
    assert "do not deploy past a red suite" in dep
    # the frontend cache key must change or clients keep the old shell
    assert "ALREADY LIVE" in dep


def test_deploy_leaves_everything_dark_and_says_so():
    dep = _read(DEPLOY)
    assert "STILL DARK" in dep or "still DARK" in dep
    # the un-dark checklist has to name all four gates
    tail = dep[dep.index("BEFORE UN-DARKING"):]
    for gate in ("LT-0", "LT-2", "two-account", "lastsietch-karum-audit"):
        assert gate in tail, f"the un-dark checklist does not mention {gate}"
    # and the mirror-the-flag warning, which has bitten twice
    assert "MIRROR BOTH DEFAULTS INTO THE REPO" in tail
    assert "next deploy silently reverts" in tail


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
