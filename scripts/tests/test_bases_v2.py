#!/usr/bin/env python3
"""Regression tests for the V2 Bases/Solido backend: the sibling JSON endpoints
in admin-backend/routers/solido.py (market/detail/overview reads + publish /
unpublish / rename / import writes).

Prod-safe: NO real DB, NO network, NO heavy-import of solido.py (host env only).
The endpoint layer is asserted by scanning the source so we catch a regression
that (a) drops a v2 route, (b) reads a client-supplied blueprint instead of the
server-side re-export invariant, (c) drops a CSRF/cap/online-offline gate, (d)
stops honoring the client-uuid idempotency key, or (e) narrows the all-linked
scope. The V2 write handlers must never fall back to request.form().

Every source-scan pin here was proven RED against a broken variant before it
landed: test_every_source_scan_pin_is_red_on_a_broken_variant mutates a scratch
COPY of the tree and re-runs this file, so the repo is never edited and there is
nothing to restore with `git checkout --`.

Run:  python3 scripts/tests/test_bases_v2.py     (also import-safe)
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)

# A broken-variant run points ROOT at a scratch COPY of the tree; every path
# below reads through it so the child run scans the mutated files.
ROOT = os.environ.get("BASESV2_ROOT", REPO)
CHILD = os.environ.get("BASESV2_CHILD") == "1"

SOLIDO = os.path.join(ROOT, "admin-backend", "routers", "solido.py")
LISTING_CARD = os.path.join(ROOT, "admin-backend", "portal-nextgen", "src",
                            "lib", "components", "bases", "ListingCard.svelte")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _v2_block(src):
    marker = "V2 MODULE PORT (2026-07-15)"
    assert marker in src, "V2 banner missing"
    return src.split(marker, 1)[1]


def _handler(src, defname):
    block = _v2_block(src)
    start = block.index(f"async def {defname}(")
    rest = block[start:]
    # up to the next route decorator (or EOF)
    idx = rest.find("\n@router.")
    return rest if idx < 0 else rest[:idx]


# --------------------------------------------------------------------------- #
# Route presence
# --------------------------------------------------------------------------- #

def test_all_v2_routes_present():
    src = _read(SOLIDO)
    for route in ('@router.get("/portal/bases/v2")',
                  '@router.get("/portal/solido/v2/market")',
                  '@router.get("/portal/solido/v2/{publish_id:int}")',
                  '@router.get("/portal/solido/v2/{publish_id:int}/blueprint")',
                  '@router.post("/portal/bases/v2/publish")',
                  '@router.post("/portal/bases/v2/unpublish")',
                  '@router.post("/portal/bases/v2/rename")',
                  '@router.post("/portal/bases/v2/import")',
                  '@router.get("/portal/solido/v2/tags")',
                  '@router.get("/portal/solido/v2/{publish_id:int}/download")',
                  '@router.get("/portal/bases/v2/imports")'):
        assert route in src, route


def test_v1_routes_untouched():
    src = _read(SOLIDO)
    for route in ('@router.post("/portal/solido/publish")',
                  '@router.post("/portal/solido/{publish_id:int}/unpublish")',
                  '@router.post("/portal/solido/import")',
                  '@router.get("/portal/my-bases")',
                  '@router.post("/portal/my-bases/{bp_id}/rename")',
                  '@router.get("/portal/solido/{publish_id:int}/download")',
                  '@router.get("/portal/solido/{publish_id:int}/thumb.png")'):
        assert route in src, route


# --------------------------------------------------------------------------- #
# V2 writes are JSON-only (never raw form) and CSRF-gated
# --------------------------------------------------------------------------- #

def test_v2_writes_never_use_raw_form():
    block = _v2_block(_read(SOLIDO))
    assert "request.form(" not in block
    assert "await _v2_json_body(request)" in block


def test_every_v2_write_is_csrf_gated():
    src = _read(SOLIDO)
    for defname in ("bases_v2_publish", "bases_v2_unpublish",
                    "bases_v2_rename", "bases_v2_import"):
        h = _handler(src, defname)
        assert "if not _v2_csrf_ok(request, body):" in h, defname
        assert 'return _v2_err("csrf"' in h, defname


def test_every_v2_write_requires_linked_session():
    src = _read(SOLIDO)
    for defname in ("bases_v2_publish", "bases_v2_unpublish",
                    "bases_v2_rename", "bases_v2_import", "bases_v2_overview"):
        h = _handler(src, defname)
        assert "accounts = _session_accounts(request)" in h, defname
        assert 'return _v2_err("unauthenticated"' in h, defname


# --------------------------------------------------------------------------- #
# Publish: server-side re-export invariant (never trusts a client blueprint)
# --------------------------------------------------------------------------- #

def test_publish_is_server_side_reexport():
    h = _handler(_read(SOLIDO), "bases_v2_publish")
    # takes only a numeric game_bp_id; the blueprint is re-exported via the
    # ownership-gated relay, never read from the request body.
    assert 'game_bp_id = str(body.get("game_bp_id"' in h
    assert "/blueprint/{game_bp_id}/export" in h
    assert 'body.get("blueprint")' not in h  # never a client-supplied blueprint


def test_publish_rolling_cap_enforced():
    src = _read(SOLIDO)
    h = _handler(src, "bases_v2_publish")
    assert "BLUEPRINT_PUBLISH_DAILY_CAP" in h
    assert "total_recent = _published_today(accounts)" in h
    assert 'return _v2_err("rate_limited"' in h
    # the shared helper is the durable publish-log COUNT.
    assert "recent_publish_count(aid, since)" in src


def test_publish_all_linked_scope():
    h = _handler(_read(SOLIDO), "bases_v2_publish")
    # tries every linked account until one owns the blueprint (V1 parity).
    assert "for acct in accounts:" in h


def test_publish_size_cap():
    h = _handler(_read(SOLIDO), "bases_v2_publish")
    assert "blueprint_market.PUBLISH_BLOB_MAX" in h
    assert 'return _v2_err("too_large"' in h


# --------------------------------------------------------------------------- #
# Import: recipient forced to linked account, offline gate, cap, client uuid
# --------------------------------------------------------------------------- #

def test_import_recipient_forced_to_linked_account():
    h = _handler(_read(SOLIDO), "bases_v2_import")
    assert "my_ids = _account_id_set(accounts)" in h
    assert "if account_id not in my_ids:" in h
    assert 'return _v2_err("bad_recipient"' in h


def test_import_backpack_online_gate():
    h = _handler(_read(SOLIDO), "bases_v2_import")
    assert 'if delivery == "backpack" and online:' in h
    assert 'return _v2_err(\n            "online",' in h or '_v2_err("online"' in h
    assert "status=409" in h


def test_import_rolling_cap_enforced():
    h = _handler(_read(SOLIDO), "bases_v2_import")
    assert "BLUEPRINT_IMPORT_DAILY_CAP" in h
    assert 'recent_action_count(\n        "portal_solido_import"' in h or \
           'recent_action_count("portal_solido_import"' in h
    assert 'return _v2_err("rate_limited"' in h


def test_import_honors_client_uuid_idempotency():
    src = _read(SOLIDO)
    h = _handler(src, "bases_v2_import")
    # the client idempotency key becomes the grant key.
    assert "idem_key = _v2_idem_key(body)" in h
    assert "idempotency_key=idem_key" in h
    # _v2_idem_key accepts BOTH `uuid` (frontend) and legacy `client_uuid`.
    helper = src[src.index("def _v2_idem_key("):]
    helper = helper[:helper.index("\n\n\n")] if "\n\n\n" in helper else helper[:400]
    assert 'body.get("uuid")' in helper and 'body.get("client_uuid")' in helper
    assert "_uuid.UUID(raw)" in helper


def test_import_server_side_recipient_never_arbitrary():
    h = _handler(_read(SOLIDO), "bases_v2_import")
    # recipient is forced from the session's linked set, never trusted from body
    # beyond membership check; the executor gets the validated account_id.
    assert "account_id=account_id" in h
    assert 'grant_type="import_blueprint"' in h


# --------------------------------------------------------------------------- #
# Rename: offline gate surfaced, all-linked, CSRF
# --------------------------------------------------------------------------- #

def test_rename_offline_gate_status():
    h = _handler(_read(SOLIDO), "bases_v2_rename")
    # player_online -> 409 (log out to rename); mirrors the V1 status mapping.
    assert 'status = 409 if last_err == "player_online"' in h
    assert "_RENAME_ERROR_TEXT" in h


def test_rename_name_cap():
    h = _handler(_read(SOLIDO), "bases_v2_rename")
    assert "RENAME_NAME_MAX" in h
    assert 'return _v2_err("name_too_long"' in h
    assert 'return _v2_err("empty_name"' in h


def test_rename_all_linked_scope():
    h = _handler(_read(SOLIDO), "bases_v2_rename")
    assert "for acct in accounts:" in h
    assert '"/dune/blueprint/rename"' in h


# --------------------------------------------------------------------------- #
# Reads: market paging + detail + overview
# --------------------------------------------------------------------------- #

def test_market_uses_page_and_card_shape():
    h = _handler(_read(SOLIDO), "solido_v2_market")
    assert 'q.get("page"' in h
    assert "offset = (page - 1) * limit" in h
    # rows[:limit], not rows: the limit+1 probe row is dropped before rendering.
    assert "[_card(r, listed) for r in rows[:limit]]" in h
    assert '"has_more":' in h


def test_detail_returns_canonical_card_plus_owner():
    h = _handler(_read(SOLIDO), "solido_v2_detail")
    assert "card = _card(row)" in h
    assert '"is_owner"' in h and "_my_publish_ids(accounts)" in h
    assert 'return _v2_err("not_found"' in h


def test_overview_shape_matches_frontend_contract():
    h = _handler(_read(SOLIDO), "bases_v2_overview")
    assert "/dune/player/{aid}/blueprints" in h
    # published is NESTED per-blueprint (null when unpublished), not a top-level map
    assert 'r["status"] == "published"' in h
    assert '"published": published_by_bp.get(str(bp_id))' in h
    # per-blueprint fields
    for key in ('"bp_id":', '"name":', '"piece_count":'):
        assert key in h, key
    # top-level shape
    assert '"linked": bool(accounts)' in h
    assert '"sections":' in h
    # caps -> publish_daily_cap + published_today + rename_name_max (input maxlength)
    assert '"publish_daily_cap": BLUEPRINT_PUBLISH_DAILY_CAP' in h
    assert '"published_today": _published_today(accounts)' in h
    assert '"rename_name_max": RENAME_NAME_MAX' in h
    # feature flags + renamed csrf
    assert '"publish_enabled": _publish_enabled()' in h
    assert '"import_enabled": _import_enabled()' in h
    assert '"csrf":' in h and '"csrf_token":' not in h


def test_publish_return_shape():
    h = _handler(_read(SOLIDO), "bases_v2_publish")
    # publish returns publish_id + download_count + refreshed published_today
    assert '"publish_id": publish_id' in h
    assert '"download_count":' in h
    assert '"published_today": _published_today(accounts)' in h


def test_publish_reads_the_clients_listing_copy():
    h = _handler(_read(SOLIDO), "bases_v2_publish")
    # the publish form's title/description/tags reach the data layer. Hardcoding
    # them back to "" is exactly the bug Wave 6 exists to fix.
    assert 'title = str(body.get("title", "") or "")' in h
    assert 'description = str(body.get("description", "") or "")' in h
    assert 'raw_tags = body.get("tags")' in h
    assert "user_tags=user_tags" in h
    assert 'title = ""' not in h and 'description = ""' not in h


def test_publish_tags_are_a_list_of_strings_or_nothing():
    h = _handler(_read(SOLIDO), "bases_v2_publish")
    # a client sending {"tags": "Outpost"} or [1, {}] must not reach the DB layer
    # as free text; the shape guard is the route's job, the vocabulary is not.
    assert "isinstance(raw_tags, list)" in h
    assert "isinstance(t, str)" in h


def test_publish_blank_title_still_falls_back_to_the_reexported_name():
    h = _handler(_read(SOLIDO), "bases_v2_publish")
    assert "if not title:" in h
    assert 'title = raw.get("name") or blueprint.get("name") or ""' in h


def test_write_kill_switches_enforced():
    src = _read(SOLIDO)
    ph = _handler(src, "bases_v2_publish")
    assert "if not _publish_enabled():" in ph
    assert 'return _v2_err("publish_disabled"' in ph
    ih = _handler(src, "bases_v2_import")
    assert "if not _import_enabled():" in ih
    assert 'return _v2_err("import_disabled"' in ih



# --------------------------------------------------------------------------- #
# Paging: has_more uses a limit+1 probe, so an exactly-full page is final
# --------------------------------------------------------------------------- #

def test_no_has_more_site_uses_the_old_full_page_comparison():
    src = _read(SOLIDO)
    # `len(rows) >= limit` reports more:true on an exactly-full last page, which
    # renders an empty "Load more".
    assert "len(rows) >= limit" not in src
    assert "len(listings) >= GALLERY_LIMIT_DEFAULT" not in src
    assert '"has_more": len(' not in src, "a has_more site still computes inline from len()"


def test_all_three_paging_sites_probe_one_row_past_the_page():
    src = _read(SOLIDO)
    # V1 gallery JSON + V2 market take limit from the query; the hub is page one.
    assert src.count("list_published(sort, tag, limit + 1, offset)") == 2
    assert "list_published(sort, None, GALLERY_LIMIT_DEFAULT + 1, 0)" in src
    assert src.count("has_more = len(rows) > limit") == 2
    assert "has_more = len(rows) > GALLERY_LIMIT_DEFAULT" in src


def test_the_probe_row_is_never_returned_to_the_client():
    src = _read(SOLIDO)
    assert src.count("[_card(r, listed) for r in rows[:limit]]") == 2
    assert "[_card(r, listed) for r in rows[:GALLERY_LIMIT_DEFAULT]]" in src


def test_paged_response_shapes_are_unchanged():
    src = _read(SOLIDO)
    for key in ('"ok": True', '"listings": listings', '"has_more": has_more',
                '"page": page'):
        assert key in src, key


# --------------------------------------------------------------------------- #
# Market card renders the census the payload already carries
# --------------------------------------------------------------------------- #

def test_card_payload_still_carries_every_census_field():
    src = _read(SOLIDO)
    card = src[src.index("def _card(row: dict"):]
    card = card[:card.index("\ndef ")]
    for key in ('"instance_count"', '"placeable_count"', '"pentashield_count"',
                '"created_at"', '"faction"', '"has_paid_pieces"'):
        assert key in card, f"_card() no longer emits {key}"


def test_listing_card_renders_every_census_field():
    src = _read(LISTING_CARD)
    for field in ("instance_count", "placeable_count", "pentashield_count",
                  "created_at", "faction", "has_paid_pieces"):
        assert f"listing.{field}" in src, f"ListingCard drops {field}"


def test_listing_card_skips_null_census_fields():
    src = _read(LISTING_CARD)
    census = src[src.index("let census"):src.index("let added")]   # the census block only
    assert "filter((c) => c.value)" in census and "c.value != null" not in census, \
        "null and zero counts must not render as stats"


# --------------------------------------------------------------------------- #
# Wave 6: author line honours the player-directory opt-out (ruling 9.2)
# --------------------------------------------------------------------------- #

def test_card_author_line_honours_the_directory_opt_out():
    src = _read(SOLIDO)
    card = src[src.index("def _card(row: dict"):]
    card = card[:card.index("\ndef ")]
    # the opt-out is read at CARD time, not snapshotted at publish time.
    assert "_is_listed_cached(row.get(\"account_id\"), listed_cache)" in card
    assert "else AUTHOR_STAND_IN" in card
    assert 'AUTHOR_STAND_IN = "a Fremen builder"' in src
    # the stored name is untouched: the audit trail still says who published it.
    assert "author_name = " not in card


def test_opt_out_lookup_is_cached_per_request_not_per_row():
    src = _read(SOLIDO)
    helper = src[src.index("def _is_listed_cached("):]
    helper = helper[:helper.index("\ndef ")]
    assert "if cache is not None and aid in cache:" in helper
    assert "cache[aid] = listed" in helper
    # a lookup failure must fall to the stand-in, never to the name.
    assert "listed = False" in helper
    # every gallery page hands _card one cache for the whole page.
    assert src.count("listed = {}") == 3


def test_card_carries_the_share_url_and_the_player_tags():
    src = _read(SOLIDO)
    card = src[src.index("def _card(row: dict"):]
    card = card[:card.index("\ndef ")]
    assert '"user_tags": row.get("user_tags") or []' in card
    assert '"share_url": f"{PORTAL_V2_SITE}bases/' in card
    assert 'PORTAL_V2_SITE = "https://portal.lastsietch.com/"' in src


# --------------------------------------------------------------------------- #
# Wave 6: download, tags and import-history routes
# --------------------------------------------------------------------------- #

def test_download_counts_and_audits_under_the_v1_action_name():
    h = _handler(_read(SOLIDO), "solido_v2_download")
    assert "blueprint_market.increment_download(publish_id)" in h
    # the action name is shared with V1: renaming it orphans every existing row.
    assert '"portal_blueprint_download"' in h
    assert "_slug_filename(row.get(\"title\") or \"\"" in h
    assert "Content-Disposition" in h and "attachment; filename=" in h


def test_download_missing_blob_404s_without_counting():
    h = _handler(_read(SOLIDO), "solido_v2_download")
    body = h[:h.index("blueprint_market.increment_download")]
    # both refusals sit ABOVE the increment: a download nobody received is not
    # a download, and a hidden/removed row must not move the counter either.
    assert body.count('return _v2_err("not_found"') == 2
    assert "status=404" in body


def test_download_bucket_is_checked_before_the_db_read():
    src = _read(SOLIDO)
    h = _handler(src, "solido_v2_download")
    assert "if not _check_download_rate(" in h
    assert 'return _v2_err("rate_limited"' in h and "status=429" in h
    gate = h.index("_check_download_rate")
    assert gate < h.index("blueprint_market.get_public"), \
        "the bucket must refuse before the row read, not after"
    assert "DOWNLOAD_RATE_LIMIT = 30" in src
    assert "DOWNLOAD_RATE_WINDOW = 60" in src


def test_tags_route_serves_the_data_layer_catalog():
    h = _handler(_read(SOLIDO), "solido_v2_tags")
    # one source of truth: the chip rail and the stored tags cannot drift.
    assert "return _v2_ok(blueprint_market.tag_catalog())" in h


def test_imports_route_is_session_gated_and_scoped_to_own_accounts():
    h = _handler(_read(SOLIDO), "bases_v2_imports")
    assert "accounts = _session_accounts(request)" in h
    assert 'return _v2_err("unauthenticated"' in h and "status=401" in h
    assert "sorted(_account_id_set(accounts))" in h
    assert 'recent_action_rows("portal_solido_import"' in h
    assert "IMPORT_HISTORY_MAX" in h


def test_imports_route_never_returns_the_source_or_the_grant_id():
    h = _handler(_read(SOLIDO), "bases_v2_imports")
    rows = h[h.index("rows.append("):]
    for leak in ('"source"', "grant_id", "detail.get(\"source\")"):
        assert leak not in rows, f"import history leaks {leak}"
    for key in ('"at":', '"delivery":', '"source_type":', '"result":', '"message":'):
        assert key in rows, key


def test_overview_caps_carry_the_import_pair():
    h = _handler(_read(SOLIDO), "bases_v2_overview")
    assert '"import_daily_cap": BLUEPRINT_IMPORT_DAILY_CAP' in h
    assert '"imported_today": _imported_today(accounts)' in h
    # additive: the publish pair and the rename cap stay exactly where they were.
    assert '"publish_daily_cap": BLUEPRINT_PUBLISH_DAILY_CAP' in h
    assert '"rename_name_max": RENAME_NAME_MAX' in h


def test_overview_published_block_prefills_the_edit_form():
    h = _handler(_read(SOLIDO), "bases_v2_overview")
    block = h[h.index('published_by_bp[str(r["game_bp_id"])] = {'):]
    block = block[:block.index("}")]
    # additive: the Edit affordance prefills from this one read, no second fetch.
    for key in ('"publish_id":', '"download_count":', '"title":',
                '"description":', '"user_tags":'):
        assert key in block, f"overview published block drops {key}"


def test_imported_today_counts_the_same_rows_the_cap_gates_on():
    src = _read(SOLIDO)
    helper = src[src.index("def _imported_today("):]
    helper = helper[:helper.index("\ndef ")]
    assert 'recent_action_count("portal_solido_import", since, targets=targets)' in helper
    assert "hours=24" in helper


# --------------------------------------------------------------------------- #
# The pins themselves: each one watched to fail
# --------------------------------------------------------------------------- #

MUTATIONS = (
    ("publish drops the client title",
     "admin-backend/routers/solido.py",
     'title = str(body.get("title", "") or "")',
     'title = ""'),
    ("publish drops the player tags",
     "admin-backend/routers/solido.py",
     "blueprint=blueprint, meta=meta, user_tags=user_tags)",
     "blueprint=blueprint, meta=meta)"),
    ("author line ignores the opt-out",
     "admin-backend/routers/solido.py",
     '"author_name": (row.get("author_name")\n'
     '                        if _is_listed_cached(row.get("account_id"), listed_cache)\n'
     '                        else AUTHOR_STAND_IN),',
     '"author_name": row.get("author_name"),'),
    ("opt-out lookup uncached",
     "admin-backend/routers/solido.py",
     "    if cache is not None and aid in cache:\n        return cache[aid]\n",
     ""),
    ("opt-out failure shows the name",
     "admin-backend/routers/solido.py",
     "        listed = False\n",
     "        listed = True\n"),
    ("share url dropped",
     "admin-backend/routers/solido.py",
     '        "share_url": f"{PORTAL_V2_SITE}bases/{row.get(\'publish_id\')}",\n',
     ""),
    ("download counts a missing blob",
     "admin-backend/routers/solido.py",
     '    body = blueprint_market.read_blob(row)\n'
     '    if body is None:\n'
     '        return _v2_err("not_found", "Blueprint file unavailable", status=404)\n\n'
     '    blueprint_market.increment_download(publish_id)',
     '    blueprint_market.increment_download(publish_id)\n'
     '    body = blueprint_market.read_blob(row)\n'
     '    if body is None:\n'
     '        return _v2_err("not_found", "Blueprint file unavailable", status=404)\n'),
    ("download bucket after the read",
     "admin-backend/routers/solido.py",
     '    if not _check_download_rate(ip or "unknown"):\n'
     '        return _v2_err("rate_limited",\n'
     '                       "Too many downloads. Try again in a minute.", status=429)\n\n'
     '    row = blueprint_market.get_public(publish_id)',
     '    row = blueprint_market.get_public(publish_id)\n'
     '    if not _check_download_rate(ip or "unknown"):\n'
     '        return _v2_err("rate_limited",\n'
     '                       "Too many downloads. Try again in a minute.", status=429)'),
    ("download audit action renamed",
     "admin-backend/routers/solido.py",
     # anchored on the V2 handler: V1's download audits under the same name.
     '    blueprint_market.increment_download(publish_id)\n'
     '    audit_log(\n'
     '        None, "public", "portal_blueprint_download"',
     '    blueprint_market.increment_download(publish_id)\n'
     '    audit_log(\n'
     '        None, "public", "portal_blueprint_download_v2"'),
    ("import history leaks the source",
     "admin-backend/routers/solido.py",
     '            "source_type": detail.get("source_type") or "",',
     '            "source_type": detail.get("source_type") or "",\n'
     '            "source": detail.get("source") or "",'),
    ("import history ungated",
     "admin-backend/routers/solido.py",
     '    if not accounts:\n'
     '        return _v2_err("unauthenticated",\n'
     '                       "Link your character to see your imports.", status=401)\n',
     ""),
    ("overview drops the import cap",
     "admin-backend/routers/solido.py",
     '            "import_daily_cap": BLUEPRINT_IMPORT_DAILY_CAP,\n',
     ""),
    ("overview published block drops the listing copy",
     "admin-backend/routers/solido.py",
     '                "title": r.get("title") or "",\n',
     ""),
    ("tags route stops using the catalog",
     "admin-backend/routers/solido.py",
     "return _v2_ok(blueprint_market.tag_catalog())",
     'return _v2_ok({"tags": [], "size_bands": [], "purposes": []})'),
)

_SCRATCH_TREE = (
    "admin-backend/routers/solido.py",
    "admin-backend/portal-nextgen/src/lib/components/bases/ListingCard.svelte",
)


def _scratch_copy(tmp):
    for rel in _SCRATCH_TREE:
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), dst)


def _run_child(root):
    env = dict(os.environ, BASESV2_ROOT=root, BASESV2_CHILD="1")
    return subprocess.run([sys.executable, os.path.abspath(__file__)],
                          capture_output=True, text=True, env=env, timeout=300)


def test_every_source_scan_pin_is_red_on_a_broken_variant():
    """A pin nobody has watched fail is not a pin. Each mutation lands on a
    scratch COPY of the tree; the repo is never edited, so there is nothing to
    restore with `git checkout --` and no way for a failed run to leave the
    working tree broken."""
    if CHILD:
        return
    tmp = tempfile.mkdtemp(prefix="basesv2-broken-",
                           dir=os.environ.get("BASESV2_SCRATCH") or None)
    try:
        _scratch_copy(tmp)
        clean = _run_child(tmp)
        assert clean.returncode == 0, (
            "the unmutated scratch copy already fails: "
            + " | ".join(l for l in clean.stdout.splitlines() if l.startswith("FAIL")))

        for label, rel, find, replace in MUTATIONS:
            path = os.path.join(tmp, rel)
            original = _read(path)
            assert find in original, f"[{label}] the mutation target is gone from {rel}"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original.replace(find, replace, 1))
            try:
                broken = _run_child(tmp)
                assert broken.returncode != 0, \
                    f"[{label}] the suite stayed GREEN on a broken variant"
                print(f"  broken-variant RED: {label}")
            finally:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(original)

        again = _run_child(tmp)
        assert again.returncode == 0, "the scratch copy was not restored between mutations"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_opt_out_cache_fails_toward_the_stand_in_on_a_missing_account_id():
    src = _read(SOLIDO)
    body = src[src.index("def _is_listed_cached("):]
    body = body[:body.index("\ndef ")]
    assert "if not aid:\n        return False" in body, \
        "an unknown publisher must get the stand-in, not the stored name"


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
