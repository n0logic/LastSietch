#!/usr/bin/env python3
"""Regression tests for the Karum frontend (Phase 1 UI): the route, the nav entry, the
store's outcome handling, and the copy that has to be there.

Prod-safe: source-scan only, no build, no browser. The build itself is the type/compile
check (`npm run build` in admin-backend/portal-nextgen), so this covers the invariants a
successful build cannot see:

  * the OFFLINE gate is on the LIST form and NOT on the buy flow, because listing takes and
    buying gives, and getting that backwards either blocks a legal purchase or opens the
    duplication path;
  * a 202 (`reconciling` / `paid_undelivered`) is treated as IN FLIGHT rather than as a
    failure, and does not re-arm the buy button;
  * the uuid is minted once per intended write and reused, which is what makes the
    server-side idempotency reachable;
  * the CANCELED render is explained wherever goods change hands, because the game will
    never say "purchase" and an unexplained CANCELED reads as a broken trade;
  * the service worker was bumped, which is a hard rule for any content change.

Run:  python3 scripts/tests/test_karum_ui.py     (also import-safe)
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
UI = os.path.join(REPO, "admin-backend", "portal-nextgen")
SRC = os.path.join(UI, "src")

STORE = os.path.join(SRC, "lib", "karum.svelte.js")
API = os.path.join(SRC, "lib", "api.js")
LAYOUT = os.path.join(SRC, "routes", "+layout.svelte")
PAGE = os.path.join(SRC, "routes", "karum", "+page.svelte")
SW = os.path.join(UI, "static", "sw.js")
CARD = os.path.join(SRC, "lib", "components", "karum", "KarumCard.svelte")
BOARD = os.path.join(SRC, "lib", "components", "karum", "KarumBoard.svelte")
SELL = os.path.join(SRC, "lib", "components", "karum", "KarumSellDialog.svelte")
BUY = os.path.join(SRC, "lib", "components", "karum", "KarumBuyDialog.svelte")
MINE = os.path.join(SRC, "lib", "components", "karum", "MyKarumListings.svelte")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def test_every_file_exists():
    for p in (STORE, PAGE, CARD, BOARD, SELL, BUY, MINE,
              os.path.join(SRC, "routes", "karum", "+page.js")):
        assert os.path.isfile(p), p


def test_nav_entry_present_and_v2():
    src = _read(LAYOUT)
    assert "{ label: 'Karum', href: `${base}/karum`, v2: true }," in src
    # next to Exchange, which is the point of the placement
    assert src.index("'Exchange'") < src.index("'Karum'") < src.index("'Landsraad'")


def test_api_namespace_covers_the_contract():
    src = _read(API)
    for path in ("'/portal/karum'", "/portal/karum/search", "/portal/karum/listing/",
                 "'/portal/karum/sellable'", "'/portal/karum/list'",
                 "'/portal/karum/buy'", "'/portal/karum/cancel'"):
        assert path in src, path
    karum = src[src.index("karum: {"):]
    karum = karum[:karum.index("\n  },")]
    # writes go through the CSRF sender, reads do not
    for w in ("list:", "buy:", "cancel:"):
        line = [l for l in karum.splitlines() if l.strip().startswith(w)][0]
        assert "sendCsrfJSON" in line, w


def test_service_worker_bumped():
    """Any content change gets a NEW cache key in the SAME deploy. Phase 0 took v46 and
    Phase 1 v47; v49 is the no_category copy fix (the page stopped promising that
    non-delivery is impossible, and the sell dialog explains a withheld item)."""
    m = re.search(r"ls-v2-shell-v(\d+)", _read(SW))
    assert m, "no SW cache version found"
    assert int(m.group(1)) >= 49, f"SW cache is v{m.group(1)}, expected v49 or later"


# --------------------------------------------------------------------------- #
# The offline gate is on LIST, and only on LIST
# --------------------------------------------------------------------------- #

def test_sell_form_is_offline_gated():
    src = _read(SELL)
    assert "karum.offlineOk" in src
    assert "karum.online == null" in src, "undetermined must read as locked"
    # the gate is folded into the submit condition, not just painted on
    assert re.search(r"canList\s*=\s*\$derived\([^)]*!locked", src), \
        "the List button must be disabled by the gate, not merely warned about"


def test_buy_flow_is_NOT_offline_gated():
    """Buying is a GIVE to the buyer and giving to an online player is safe. Gating it would
    block a legal purchase for no reason."""
    src = _read(BUY)
    assert "offlineOk" not in src, "the buy dialog must not gate on the offline flag"
    page = _read(PAGE)
    assert "canBuy={linked}" in page


# --------------------------------------------------------------------------- #
# The store's outcome handling: the reason this store is not optimistic
# --------------------------------------------------------------------------- #

def test_store_treats_202_as_in_flight_not_failure():
    src = _read(STORE)
    buy = src[src.index("export async function buyListing"):src.index("export async function cancelListing")]
    assert "e?.status === 202" in buy
    assert "reconciling" in buy and "paid_undelivered" in buy
    assert "inFlight: true" in buy
    # in-flight must refetch, because the server knows more than we do
    assert "loadOverview()" in buy


def test_buy_button_does_not_rearm_while_in_flight():
    """The server has accepted this correlation_id and is resolving it. Re-arming invites a
    click that can only replay, and tells the player to expect something a retry cannot give."""
    src = _read(BUY)
    assert re.search(r"settled\s*=\s*\$derived\(phase === 'ok' \|\| phase === 'inflight'\)", src)
    assert re.search(r"canBuy\s*=\s*\$derived\([^)]*!settled", src)
    assert "do not buy again" in src


def test_store_never_fakes_success_on_deferred():
    src = _read(STORE)
    for fn in ("listItem", "buyListing", "cancelListing"):
        body = src[src.index(f"export async function {fn}"):]
        body = body[:body.index("\n}\n")]
        assert "deferred: true" in body, fn
        assert body.index("deferred") < body.index("ok: true"), \
            f"{fn} must check deferred BEFORE reporting success"


def test_writes_reuse_one_uuid_per_intended_action():
    """Minting a fresh uuid per click would defeat the server-side idempotency the whole
    design leans on: a retry after a lost response must REPLAY, not execute again."""
    for path, label in ((SELL, "sell"), (BUY, "buy")):
        src = _read(path)
        assert "let uuid = uuidv4();" in src, label
        # not minted inside the submit handler
        submit = src[src.index("async function submit"):]
        assert "uuidv4()" not in submit.split("}")[0], f"{label} mints a uuid per click"
    # the pull path keys by listing so two rows cannot share one key
    mine = _read(MINE)
    assert "keys.set(row.listing_id, uuidv4())" in mine
    assert "keys.get(row.listing_id)" in mine


# --------------------------------------------------------------------------- #
# Copy that has to be there
# --------------------------------------------------------------------------- #

def test_canceled_render_is_explained_wherever_goods_move():
    """completion_type 3 is the only format the client renders and it reads as CANCELED. The
    game will never say 'purchase', so every surface that hands over goods says so."""
    for path, label in ((BUY, "buy dialog"), (MINE, "my listings"), (PAGE, "page")):
        assert "CANCELED" in _read(path), f"{label} does not explain the CANCELED render"


def test_page_states_the_structural_promise_without_overclaiming():
    """Escrow at listing time is the feature's strongest claim, and it is only true because
    of where the escrow happens: a seller cannot take the money and keep the goods.

    🔴 It must NOT say non-delivery is impossible. That is what this page said until
    2026-07-27 and it was false -- an item whose template has no CHOAM Exchange category
    could be escrowed and then handed to nobody, ~10% of the templates in player banks, and
    it stranded a real item on the first live cancel. The listing leg refuses those now, but
    the copy stays scoped to what is actually guaranteed rather than being re-broadened."""
    src = _read(PAGE)
    assert "escrow" in src.lower()
    assert "keep the item" in src, "the page no longer states the seller-side guarantee"
    promise = src[src.index('class="promise"'):]
    promise = promise[:promise.index("</p>")]
    assert "not possible" not in promise.lower() and "impossible" not in promise.lower(), \
        "the page is claiming non-delivery is impossible again; it is not"


def test_sell_dialog_explains_a_withheld_item():
    """An item the player can see in their bank in-game, absent from the sell list with no
    explanation, reads as a broken page rather than a property of the item."""
    src = _read(SELL)
    assert "hiddenNoCategory" in src, "the dialog cannot tell how many items were withheld"
    assert "CHOAM Exchange" in src and "give it back" in src
    assert "hiddenNoCategory" in _read(STORE), "the store drops the withheld count"


def test_my_listings_names_every_transient_state():
    """A player staring at a row that says nothing while their goods or Solari are mid-flight
    is the failure this copy prevents."""
    src = _read(MINE)
    for state in ("pending", "active", "selling", "reconciling", "sold", "returning",
                  "cancelled", "paid_undelivered", "failed"):
        assert f"{state}:" in src, f"no copy for the {state} state"
    # and the two that can look alarming say they are not losses
    assert "nothing lost" in src
    assert "nothing moved" in src


def test_board_is_per_listing_not_template_aggregated():
    """Forked from the bases MarketGallery, NOT the Exchange module. A Karum listing is ONE
    stack from ONE named seller at ONE price; aggregating by template would hide exactly what
    the buyer is choosing between."""
    src = _read(BOARD)
    assert "listing.listing_id" in src, "the grid must key by listing, not by template"
    assert "template-AGGREGATED" in src or "template-aggregated" in src.lower()


def test_card_emits_only_the_sellers_name_as_identity():
    src = _read(CARD)
    for leaked in ("account_id", "discord", "seller_ctrl", "buyer_ctrl", "escrow",
                   "correlation"):
        assert leaked not in src, f"the card references {leaked}"
    assert "seller_name" in src


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
