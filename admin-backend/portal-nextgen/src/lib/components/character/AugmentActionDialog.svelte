<script>
  // Per-item augment reroll/swap popup. Modelled on karum/KarumBuyDialog.svelte
  // (the closest existing "high-stakes, one clear write" modal in this
  // codebase): focus-trapped, background inert while open, Escape closes,
  // focus restores to the opener on close, the server envelope is surfaced
  // honestly rather than reinterpreted. Only ever mounted for an item that
  // already carries at least one augment (EquippedList gates the entry
  // point) - this is a rare, high-stakes screen (279 items server-wide), not
  // a casual popover.
  //
  // WIRE FORMAT (see augments.svelte.js for the full note): `augments` sent to
  // the server is always the FULL resulting list for the item, in slot order,
  // never a partial one - the writer replaces the whole block. `reroll_only`
  // is a SEPARATE field naming which of those slots get fresh rolls; every
  // slot not named there keeps its exact current rolls AND grade.
  //   - REROLL can target one slot or the whole item (name one slot, or omit
  //     the field for everything). Both are FREE. On a single-augment item
  //     they are the same action, so only one Reroll button shows; on a
  //     multi-augment item a small scope toggle picks "all" vs "one", and
  //     picking "one" reveals the same slot picker swap uses.
  //   - SWAP still targets one slot (pick which installed augment to
  //     replace, pick an owned augment to replace it with) and names ONLY
  //     the incoming augment in `reroll_only` - that is what keeps the
  //     item's other augments completely untouched, rolls and grade both.
  //
  // REROLL is free and a single click (current rolls are shown right above
  // the button, so the player can see exactly what they are gambling - it
  // can land WORSE, never framed as an upgrade). SWAP costs a permanently-
  // consumed owned augment and needs an explicit second click ("Swap" arms,
  // "Confirm swap" executes) with the destructive consequence spelled out.
  import Modal from '$lib/components/Modal.svelte';
  import AugmentChips from './AugmentChips.svelte';
  import { iconUrl } from '$lib/icons.js';
  import { uuidv4 } from '$lib/api.js';
  import { submitAugmentAction, augmentErrorMessage, slotCap } from '$lib/augments.svelte.js';

  let { item, ownedAugments = [], playerOnline = null, onClose } = $props();

  // OFFLINE GATE, the single most important fact here: item stats live in
  // memory while a player is online, so a write only survives once they are
  // logged out and past the reconnect grace window. Fail-closed like every
  // other gate in this codebase (storage.offlineOk, TransferDialog.locked):
  // undetermined counts as locked, not as "probably fine".
  let locked = $derived(playerOnline !== false);

  let installed = $derived(Array.isArray(item?.augments) ? item.augments : []);

  let mode = $state('reroll'); // 'reroll' | 'swap'

  // SWAP: which installed slot is being replaced.
  // svelte-ignore state_referenced_locally -- one-time seed; `item` does not
  // change identity while this dialog is open (SellDialog does the same for
  // its stack-count field).
  let slotName = $state(installed.length === 1 ? installed[0].name : null);
  let slot = $derived(installed.find((a) => a.name === slotName) || null);
  // The server needs the slot INDEX, not the name: on a same-name grade swap the
  // name is identical on both sides and identifies nothing. An item can never
  // carry the same augment twice, so name -> index is still unambiguous here.
  let slotIndex = $derived(installed.findIndex((a) => a.name === slotName));

  // REROLL: 'all' redraws every installed augment, 'one' redraws just
  // rerollSlotName. Only surfaced as a choice when there is more than one
  // installed augment to choose between; a single-augment item has no scope
  // toggle and the two are identical anyway.
  let rerollScope = $state('all'); // 'all' | 'one'
  // svelte-ignore state_referenced_locally -- one-time seed, same as slotName above.
  let rerollSlotName = $state(installed.length === 1 ? installed[0].name : null);
  let rerollTarget = $derived(installed.find((a) => a.name === rerollSlotName) || null);

  // CONTRACT: dropped the client-side "likely compatible" heuristic per the
  // team lead - there is now a real server-side item-tag map, so
  // `ownedAugments` is trusted as-is (assumed already scoped to this item, or
  // at minimum the future authoritative source). Showing everything with the
  // server's incompatible_augment error as backstop is the agreed fallback
  // until the contract confirms per-item scoping.
  //
  // Keyed by (name, GRADE), never name alone. `owned_augments` is grouped by
  // template AND quality_level upstream, so a player holding the same augment at
  // two grades gets two rows - and six pawns on live do right now. Matching on
  // name collapsed those two rows onto whichever came first, so picking the G5
  // could spend the G4.
  const ownedKey = (o) => (o ? `${o.name}@${o.grade}` : null);
  let chosenOwnedKey = $state(null);
  let chosenOwned = $derived(ownedAugments.find((o) => ownedKey(o) === chosenOwnedKey) || null);
  // 🔴 There is deliberately NO client-side no-op check here any more. A
  // same-name/same-grade row used to be greyed out on the theory that it could
  // only ever be the augment already fitted -- but `ownedAugments` lists SPARE
  // STANDALONE items only. A fitted augment is absorbed into the weapon's stats
  // and is not an item, so every row shown here is a genuine second copy, and
  // two copies of the same augment at the same grade have DIFFERENT rolls. That
  // is exactly the swap the player wants and the one the picker was refusing
  // (player report, 2026-08-15), which forced a workaround that lost rolls.
  //
  // Whether a given copy actually improves the item is a question about ROLLS,
  // and the payload is grouped by (name, grade) and carries none -- so the
  // client cannot answer it and must not pretend to. The server decides in the
  // same transaction that consumes, and refuses with `no_improvement` when
  // nothing carried beats what is fitted. Do not reintroduce a name/grade
  // comparison here; it can only ever be wrong in the direction that hides a
  // legitimate swap. Same-name/DIFFERENT-grade rows stay selectable, as before.
  let swapConfirm = $state(false);

  let busy = $state(false);
  let phase = $state('idle'); // idle | ok | failed
  let message = $state('');
  let resultAugments = $state(null);
  // WHICH augments actually got fresh rolls, straight from the server. A
  // single-slot reroll leaves the others byte-identical, and listing them all
  // the same way in the result implied we had rerolled things we had not.
  let resultRerolled = $state([]);
  // The rolls as they stood BEFORE the write. `installed` is derived from
  // item.augments and a successful write replaces that array in place (so the
  // card behind the dialog updates without a refetch), which means the
  // "current" panel silently re-renders as the RESULT. Two identical panels
  // read as "the reroll did nothing" - a player reported exactly that after a
  // reroll that had in fact redrawn every slot. Snapshot before submitting so
  // the before/after comparison the panel promises is actually visible.
  let beforeAugments = $state(null);
  // Which slot the swap landed in, SNAPSHOTTED at submit. A successful write
  // replaces item.augments in place (so the card behind the dialog updates
  // without a refetch), which means `slotIndex` -- derived from the OUTGOING
  // augment's name -- can no longer find it afterwards and reads -1.
  let resultSlotIndex = $state(-1);
  // The swapped-in augment as the server READ IT BACK, for the result readout.
  let swappedResult = $derived(
    phase === 'ok' && mode === 'swap' && resultAugments && resultSlotIndex >= 0
      ? resultAugments[resultSlotIndex] || null
      : null
  );
  // WHICH slots the result panel presents as changed.
  //
  // 🔴 `rerolled` and "changed" are DIFFERENT FACTS and this is where they part
  // company. `rerolled` means "drew new numbers", and on a transplant swap the
  // answer is NONE: the incoming augment's placeholder is overwritten by the
  // consumed copy's real rolls, so nothing was ever drawn. The writer reports []
  // for that, exactly as it already does on a replay. That is correct -- do not
  // "fix" it by putting the incoming augment back into `rerolled`, which would
  // make the server claim a draw that did not happen.
  //
  // But "nothing was rerolled" is not "nothing changed". A swap always changes
  // the slot it targeted, and the client knows WHICH slot independently of the
  // roll mode -- it is the one it just asked to swap. Driving this panel off
  // `rerolled` on a swap therefore collapses to an undifferentiated whole-item
  // list, leaving the player to diff two nearly identical panels to find what
  // moved. That is the exact complaint this panel exists to answer, and on a
  // same-name/same-grade swap (the case the transplant feature enables) the two
  // panels differ ONLY in roll values, so it is at its worst there.
  //
  // 🔴 The case that has NO other signal, and the reason this cannot be left to
  // the `resolved` readout below: `_resolve_augment_effect` returns null for any
  // MULTI-effect augment by design, so on a multi-effect swap that block does not
  // render at all. If this list were driven by `rerolled` (empty under transplant)
  // the panel would show three augments with nothing marking which one changed.
  // Single-effect swaps would look fine and hide it.
  //
  // Deliberately does not read `resultRerolled` on a swap at all, so this stays
  // correct whether or not the writer reports [] for a transplant.
  //
  // Indexes, not names. A swap targets one SLOT, and an item that already carries
  // the same augment name in two slots would otherwise mark both as changed --
  // `duplicate_augment` stops us CREATING that state but does not unmake a
  // pre-existing one. The reroll branch is still name-keyed because the server's
  // `rerolled` is a name list and there is nothing else to key it on.
  let changedIndexes = $derived(
    !resultAugments
      ? []
      : mode === 'swap'
        ? (resultSlotIndex >= 0 && resultAugments[resultSlotIndex] ? [resultSlotIndex] : [])
        : resultAugments.map((a, i) => i)
                        .filter((i) => resultRerolled.includes(resultAugments[i].name))
  );

  let settled = $derived(phase === 'ok');
  let readyToSubmit = $derived(
    !busy && !settled && !locked &&
    (mode === 'reroll'
      ? (installed.length > 0 && (installed.length === 1 || rerollScope === 'all' || rerollTarget != null))
      : (slot != null && chosenOwned != null))
  );

  function pickMode(m) {
    mode = m;
    slotName = installed.length === 1 ? installed[0].name : null;
    rerollScope = 'all';
    rerollSlotName = installed.length === 1 ? installed[0].name : null;
    chosenOwnedKey = null; swapConfirm = false; phase = 'idle'; message = '';
  }
  function pickRerollScope(s) {
    rerollScope = s; phase = 'idle'; message = '';
  }
  function pickRerollSlot(name) {
    rerollSlotName = name; phase = 'idle'; message = '';
  }
  function pickSlot(name) {
    slotName = name; chosenOwnedKey = null; swapConfirm = false; phase = 'idle'; message = '';
  }
  function pickOwned(o) {
    chosenOwnedKey = ownedKey(o); swapConfirm = false; phase = 'idle'; message = '';
  }

  function onPrimaryClick() {
    if (settled || !readyToSubmit) return;
    if (mode === 'swap' && !swapConfirm) { swapConfirm = true; return; }
    submit();
  }

  // Full resulting augment-name list for the item, in slot order - the
  // writer replaces the whole block, so this always has to be complete.
  // Reroll never changes WHICH augments are installed, only their rolls, so
  // this is the unchanged installed list regardless of reroll scope.
  function targetAugments() {
    if (mode === 'reroll') return installed.map((a) => a.name);
    return installed.map((a, i) => (i === slotIndex ? chosenOwned.name : a.name));
  }

  // Which slot(s) should get FRESH rolls, mirroring the writer's own
  // --reroll-only flag - applies to BOTH modes (see the wire-format note
  // above). Returns undefined for "reroll everything", never an explicit
  // full list: the writer's contract for that case is an OMITTED field, not
  // a redundant complete one.
  function rerollOnlyNames() {
    if (mode === 'swap') return chosenOwned ? [chosenOwned.name] : undefined;
    if (installed.length === 1 || rerollScope === 'all') return undefined;
    return rerollTarget ? [rerollTarget.name] : undefined;
  }

  // One idempotency key per intended write, reused across a retry of the
  // SAME intent; a fresh intent (different slot/owned pick, or a mode
  // change) mints a new one on the next submit. Mirrors the uuid-per-
  // logical-write pattern every other write path in this app already uses
  // (storage move/transfer, market, karum, rewards) - the backend writer for
  // this endpoint does not exist yet, so this is REQUIRED-ON-THE-WRITER, not
  // yet something the server can dedupe against.
  let idemKey = '';
  let idemFor = '';

  async function submit() {
    busy = true; phase = 'idle'; message = '';
    // Copy the rolls out, not just the array: the result panel and this one
    // must not end up pointing at the same objects.
    beforeAugments = installed.map((a) => ({
      ...a, rolls: Array.isArray(a.rolls) ? [...a.rolls] : a.rolls,
    }));
    resultSlotIndex = mode === 'swap' ? slotIndex : -1;
    const augments = targetAugments();
    const rerollOnly = rerollOnlyNames();
    // The slot and grade belong in the signature: a same-name grade swap leaves
    // `augments` byte-identical, so without them switching G3 -> G5 would reuse
    // the previous key and REPLAY the earlier write instead of performing this one.
    const swapSig = mode === 'swap' ? `${slotIndex}@${chosenOwned.grade}` : '';
    const sig = `${item.item_id}:${mode}:${augments.join(',')}:${(rerollOnly || []).join(',')}:${swapSig}`;
    if (idemKey === '' || idemFor !== sig) { idemKey = uuidv4(); idemFor = sig; }
    try {
      const r = await submitAugmentAction({
        itemId: item.item_id,
        mode,
        augments,
        rerollOnly,
        grade: mode === 'swap' ? chosenOwned.grade : undefined,
        swapSlot: mode === 'swap' ? slotIndex : undefined,
        swapGrade: mode === 'swap' ? chosenOwned.grade : undefined,
        idempotencyKey: idemKey,
      });
      phase = 'ok';
      resultAugments = Array.isArray(r?.augments) ? r.augments : null;
      resultRerolled = Array.isArray(r?.rerolled) ? r.rerolled : [];
      message = mode === 'reroll'
        ? (installed.length === 1 || rerollScope === 'all' ? 'Rerolled all augments.' : `Rerolled ${rerollTarget?.label || rerollTarget?.name}.`)
        : `Swapped in ${chosenOwned.label || chosenOwned.name}.`;
      // Mutate the SAME objects the row behind this dialog already holds a
      // reference to (item is the live element of character.equipped.items;
      // ownedAugments is the live equipped.owned_augments array), so the card
      // updates without a refetch - mirrors "mutate the shared proxy's
      // properties in place" from character.svelte.js / storage.svelte.js.
      if (resultAugments) item.augments = resultAugments;
      if (mode === 'swap' && chosenOwned) {
        chosenOwned.count = Math.max(0, (Number(chosenOwned.count) || 1) - 1);
        if (chosenOwned.count <= 0) {
          const idx = ownedAugments.indexOf(chosenOwned);
          if (idx >= 0) ownedAugments.splice(idx, 1);
        }
      }
      idemKey = ''; idemFor = ''; // logical write is done; a fresh action mints a new key
    } catch (e) {
      phase = 'failed';
      // e.message is the TOKEN (sendCsrfJSON puts data.error there); the whole
      // envelope, including the server's own player-facing sentence, is on e.data.
      message = augmentErrorMessage(e?.message, e?.data?.message);
    } finally { busy = false; }
  }

  // MODAL ADOPTION NOTE. The scrim, the <body> portal, the focus trap, the
  // background inert and the Escape key now all come from
  // $lib/components/Modal.svelte, which was distilled from this dialog and
  // carries BOTH mitigations below: it portals both nodes to <body> and keeps
  // the `!el.contains(panelEl)` backstop. It inerts `.shell` rather than
  // `.page`, which routes/maps/[key] does not have. The original note is kept
  // verbatim because it is the record of the two live incidents that shaped the
  // primitive, and of why neither mitigation may be simplified away.
  //
  // 🔴 PORTAL TO <body>. `.scrim` and `.modal` are position:fixed with a z-index
  // above the sticky `.topbar`, which only holds if NO ancestor creates a
  // containing block or a stacking context. This dialog is mounted DEEP inside
  // `.page` (EquippedList, AugmentedItemsPanel), so it inherited whatever its
  // ancestors imposed and was not reliably viewport-positioned.
  //
  // That mount depth has now caused TWO live bugs:
  //   * 2026-08-03 - it inerted itself, killing its own buttons (see the effect
  //     below and the defensive contains() filter added to survive it).
  //   * 2026-08-14 - it opened ~47px too high with its header clipped under the
  //     topbar and NO scrim dim, then snapped into place ~3.5s later.
  // KarumBuyDialog has BYTE-IDENTICAL .scrim/.modal CSS and neither bug, purely
  // because its route mounts it at top level as a sibling of `.page`. Same CSS,
  // different mount depth, different behaviour => the mount point WAS the bug.
  //
  // Moving the nodes to <body> makes position and stacking independent of where
  // the component is used, so this cannot recur from a third mount site. It also
  // makes the effect below inert the REAL background: previously `.page`
  // contained the dialog and was therefore filtered out, leaving the page behind
  // an open modal still interactive.
  //
  // 🔴 NEVER inert an ancestor of this dialog. `inert` is INHERITED by every
  // descendant, so inerting a container that CONTAINS the modal makes the
  // modal itself unclickable and unfocusable -- including its own X and
  // Cancel buttons, leaving the player stuck with a dead overlay (live
  // 2026-08-03, when this component was mounted inside `.page`).
  //
  // KEPT DELIBERATELY after the 2026-08-14 portal fix, even though the modal
  // now lives on <body> and so no longer matches this filter. It is the
  // backstop that makes the dialog correct wherever it is mounted, which is
  // exactly the property whose absence caused both bugs. Do not "simplify"
  // it away on the grounds that the portal makes it redundant -- the portal
  // is what makes it redundant, and this is what catches the portal
  // regressing. Post-portal it is also no longer a no-op in the useful
  // direction: `.page` is now genuinely inerted, so the page behind an open
  // modal is finally non-interactive, which it was not before.

  function primaryLabel() {
    if (busy) return 'Working';
    if (settled) return 'Done';
    if (mode === 'reroll') {
      return installed.length > 1 && rerollScope === 'all' ? 'Reroll all' : 'Reroll';
    }
    return swapConfirm ? 'Confirm swap' : 'Swap';
  }
</script>

<Modal title={`Augments · ${item?.name || item?.template || 'Item'}`} size="lg" {onClose}>
  {#if installed.length === 0}
    <p class="note">This item has no installed augments.</p>
  {:else}
    <div class="ifacts">
      <img class="iicon" src={iconUrl(item.icon)} alt="" aria-hidden="true" loading="lazy" />
      <div class="itext">
        <span class="iname">{item.name || item.template}</span>
        <span class="islot mono">{item.slot || ''}{item.category ? ` · ${item.category}` : ''}</span>
      </div>
      <span class="icap mono" title="Augment slots installed">{installed.length}/{slotCap(item)}</span>
    </div>

    {#if locked}
      <p class="gate" role="note">
        Augments only update while you are logged out, and only once the game has
        finished saving your session. Log out, wait a short moment, then come back
        here to reroll or swap. Acting while online, or right after logging out,
        will not go through.
      </p>
    {/if}

    <p class="permanote">
      In the live game, installed augments are permanent and can never be removed.
      This tool is a Last Sietch-only capability, not a restored game feature.
    </p>

    <div class="current">
      <p class="plabel mono">
        {settled && beforeAugments ? 'Before' : 'Current'}, {installed.length} installed
      </p>
      <AugmentChips augments={settled && beforeAugments ? beforeAugments : installed} />
    </div>

    <div class="modes">
      <button
        type="button" class="modebtn" class:active={mode === 'reroll'}
        aria-pressed={mode === 'reroll'} onclick={() => pickMode('reroll')} disabled={locked || settled}
      >
        <span class="mtitle">Reroll</span>
        <span class="mcost free">Free</span>
        <span class="mdesc">Redraw one augment's rolls, or all of them at once.</span>
      </button>
      <button
        type="button" class="modebtn" class:active={mode === 'swap'}
        aria-pressed={mode === 'swap'} onclick={() => pickMode('swap')} disabled={locked || settled}
      >
        <span class="mtitle">Swap</span>
        <span class="mcost cost">Costs an owned augment</span>
        <span class="mdesc">Replace one augment with one you own. Both are used up for good.</span>
      </button>
    </div>

    {#if mode === 'reroll'}
      {#if installed.length > 1}
        <div class="scope">
          <button
            type="button" class="scopebtn" class:active={rerollScope === 'all'}
            aria-pressed={rerollScope === 'all'} onclick={() => pickRerollScope('all')} disabled={locked || settled}
          >
            Reroll all ({installed.length})
          </button>
          <button
            type="button" class="scopebtn" class:active={rerollScope === 'one'}
            aria-pressed={rerollScope === 'one'} onclick={() => pickRerollScope('one')} disabled={locked || settled}
          >
            Reroll one augment
          </button>
        </div>
      {/if}

      {#if installed.length > 1 && rerollScope === 'one'}
        <div class="picker">
          <p class="plabel mono">Choose the augment to reroll</p>
          <ul class="plist" role="list">
            {#each installed as a (a.name)}
              <li>
                <button
                  type="button" class="prow" class:active={a.name === rerollSlotName}
                  aria-pressed={a.name === rerollSlotName} onclick={() => pickRerollSlot(a.name)}
                  disabled={locked || settled}
                >
                  <span class="pname">{a.label || a.name}</span>
                  {#if a.grade != null}<span class="pgrade mono">G{a.grade}</span>{/if}
                </button>
              </li>
            {/each}
          </ul>
        </div>
      {/if}

      {#if installed.length === 1 || rerollScope === 'all'}
        <p class="warn" role="note">
          This redraws rolls for {installed.length === 1 ? 'this augment' : `all ${installed.length} augments on this item`}
          at once. It is a true reroll, not an upgrade - the new rolls are drawn at
          random and can land worse than what you have now. Your current rolls are
          shown above so you can judge the gamble.
        </p>
      {:else if rerollTarget}
        <p class="warn" role="note">
          This redraws rolls for {rerollTarget.label || rerollTarget.name} only; your
          other installed augments keep their current rolls untouched. It is a true
          reroll, not an upgrade - the new rolls are drawn at random and can land
          worse than what you have now.
        </p>
      {/if}
    {:else}
      {#if installed.length > 1}
        <div class="picker">
          <p class="plabel mono">Choose the augment to replace</p>
          <ul class="plist" role="list">
            {#each installed as a (a.name)}
              <li>
                <button
                  type="button" class="prow" class:active={a.name === slotName}
                  aria-pressed={a.name === slotName} onclick={() => pickSlot(a.name)}
                  disabled={locked || settled}
                >
                  <span class="pname">{a.label || a.name}</span>
                  {#if a.grade != null}<span class="pgrade mono">G{a.grade}</span>{/if}
                </button>
              </li>
            {/each}
          </ul>
        </div>
      {/if}

      <div class="ownedpick">
        <p class="plabel mono">Owned augments you could swap in</p>
        {#if ownedAugments.length === 0}
          <p class="note">You do not own an augment that could go here yet.</p>
        {:else}
          <!-- Keyed by name AND grade: the same augment at two grades is two
               distinct things to spend, and keying on name alone made them one
               row that could not be told apart. -->
          <ul class="plist" role="list">
            {#each ownedAugments as o (ownedKey(o))}
              <li>
                <button
                  type="button" class="prow" class:active={ownedKey(o) === chosenOwnedKey}
                  aria-pressed={ownedKey(o) === chosenOwnedKey} onclick={() => pickOwned(o)}
                  disabled={locked || settled || !slot}
                >
                  <span class="pname">{o.label || o.name}</span>
                  {#if o.grade != null}<span class="pgrade mono">G{o.grade}</span>{/if}
                  <span class="pcount mono">&times;{o.count ?? 1}</span>
                </button>
              </li>
            {/each}
          </ul>
          <p class="note">The server checks whether this augment actually fits the item when you submit.</p>
        {/if}
        {#if slot && chosenOwned}
          <p class="warn" role="note">
            <!-- Name alone is ambiguous on a grade swap ("Damage1 comes off and
                 Damage1 is consumed" reads as a no-op), so both sides carry their
                 grade whenever the names match. -->
            This destroys both augments for good: {slot.label || slot.name}{#if slot.name === chosenOwned.name && slot.grade != null}&nbsp;G{slot.grade}{/if}
            comes off this item and is gone, and {chosenOwned.label || chosenOwned.name}{#if slot.name === chosenOwned.name && chosenOwned.grade != null}&nbsp;G{chosenOwned.grade}{/if}
            is consumed from your inventory to replace it. Neither can be recovered.
            {#if installed.length > 1}
              Your other installed augments on this item keep their exact current rolls and
              grades; only this one slot changes.
            {/if}
            <!-- The picker is grouped by (name, grade) and carries no rolls, so a
                 row can stand for two or three distinct copies. The server is what
                 chooses between them, in the same transaction that consumes -- say
                 so at the moment of consent rather than leaving the player to
                 discover it from `consumed_item_ids` afterwards.
                 🔴 Deliberately does NOT promise the BEST copy. Two copies are
                 often just different rather than one being better, and the only
                 thing that could rank them is a scoring scheme that was measured
                 picking the wrong copy. Claiming "best" here would be a promise
                 the server cannot keep. -->
            If you're carrying more than one of these at this grade, the server spends
            one that improves this item - not necessarily the strongest, since two
            copies can simply be different rather than one being better. If none of
            them improves it, nothing is destroyed and nothing changes.
          </p>
        {/if}
      </div>
    {/if}

    <div class="row">
      <button class="btn primary" type="button" onclick={onPrimaryClick} disabled={!readyToSubmit}>
        {primaryLabel()}
      </button>
      <button class="btn" type="button" onclick={() => onClose?.()}>{settled ? 'Close' : 'Cancel'}</button>
    </div>

    {#if phase !== 'idle' && message}
      <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
    {/if}

    {#if phase === 'ok' && resultAugments}
      <div class="after">
        <p class="plabel mono">Result</p>
        {#if changedIndexes.length > 0 && changedIndexes.length < resultAugments.length}
          <!-- Partial change: name the slots that actually changed and mark the
               rest untouched, rather than showing an undifferentiated list that
               reads as "we rerolled all of these". Applies to a single-slot
               reroll and to every swap; see changedIndexes for why a swap must
               not be derived from `rerolled`. -->
          <AugmentChips augments={resultAugments.filter((a, i) => changedIndexes.includes(i))} />
          <p class="untouched mono">
            Untouched: {resultAugments
              .filter((a, i) => !changedIndexes.includes(i))
              .map((a) => a.label || a.name)
              .join(', ')}
          </p>
        {:else}
          <AugmentChips augments={resultAugments} />
        {/if}
        <!-- The swapped-in augment's ACTUAL value at its post-swap rolls,
             computed server-side from the rolls the writer read back out of the
             row it just wrote. Present ONLY for a single-effect augment: the
             game's roll order is its own asset order and does not match the
             catalogue's, so a per-stat number on a multi-effect augment would be
             confidently wrong. Those degrade to the roll chips above, which are
             true at any order.
             This is the direct answer to "the new one I put on, its stats have
             changed" -- that was confusion about the OUTCOME, and the outcome is
             the one place we can state it honestly. -->
        {#if swappedResult?.resolved}
          <p class="resolved">
            {swappedResult.label || swappedResult.name} is now
            <strong>{swappedResult.resolved}</strong> on this item.
          </p>
        {/if}
      </div>
    {/if}
  {/if}
</Modal>

<style>
  .note { margin: 0; font-size: var(--text-sm); color: var(--text-muted); }

  .ifacts { display: flex; align-items: center; gap: var(--space-2); }
  .iicon { width: 32px; height: 32px; object-fit: contain; flex: 0 0 auto; }
  .itext { display: flex; flex-direction: column; min-width: 0; flex: 1 1 auto; }
  .iname { font-size: var(--text-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .islot { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .icap { font-size: var(--text-xs); color: var(--accent-text); flex: 0 0 auto; }

  .gate {
    margin: 0; font-size: var(--text-xs); line-height: 1.4; color: var(--text-muted);
    padding: var(--space-2); border-radius: var(--radius-sm);
    border: 1px solid color-mix(in srgb, var(--ls-yellow) 45%, var(--edge));
    background: color-mix(in srgb, var(--ls-yellow) 8%, var(--metal-0));
  }
  .permanote { margin: 0; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.4; }

  .plabel { margin: 0 0 var(--space-1); font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  .picker, .current, .ownedpick, .after { display: flex; flex-direction: column; gap: 0; }
  .untouched { margin: var(--space-1) 0 0; font-size: var(--text-xs); color: var(--text-muted); }
  .resolved { margin: var(--space-1) 0 0; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.4; }
  .resolved strong { color: var(--accent-text); font-weight: 700; }

  .plist { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); max-height: 11rem; overflow-y: auto; }
  .prow {
    width: 100%; display: flex; align-items: center; gap: var(--space-2);
    text-align: left; font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .prow:hover:not(:disabled) { border-color: var(--accent); }
  .prow.active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 14%, var(--metal-1)); }
  .prow:disabled { opacity: .5; cursor: not-allowed; }
  .pname { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pgrade { color: var(--accent-text); font-size: var(--text-xs); flex: 0 0 auto; }
  .pcount { color: var(--text-muted); font-size: var(--text-xs); flex: 0 0 auto; }

  .scope { display: flex; gap: var(--space-2); }
  .scopebtn {
    flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .06em;
    text-transform: uppercase; color: var(--text-muted); background: var(--metal-1);
    border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2);
    cursor: pointer; transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .scopebtn:hover:not(:disabled) { border-color: var(--accent); color: var(--text); }
  .scopebtn.active { border-color: var(--accent); color: var(--accent-text); background: color-mix(in srgb, var(--accent) 14%, var(--metal-1)); }
  .scopebtn:disabled { opacity: .5; cursor: not-allowed; }

  .modes { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-2); }
  .modebtn {
    display: flex; flex-direction: column; gap: 2px; text-align: left;
    background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2); cursor: pointer; min-width: 0;
    transition: border-color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .modebtn:hover:not(:disabled) { border-color: var(--accent); }
  .modebtn.active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 14%, var(--metal-1)); }
  .modebtn:disabled { opacity: .5; cursor: not-allowed; }
  .mtitle { font-family: var(--font-display); font-size: var(--text-sm); letter-spacing: .04em; text-transform: uppercase; color: var(--text); }
  .mcost { font-size: var(--text-xs); font-weight: 700; letter-spacing: .04em; }
  .mcost.free { color: var(--ls-green); }
  .mcost.cost { color: var(--ls-yellow); }
  .mdesc { font-size: var(--text-xs); color: var(--text-muted); line-height: 1.35; }

  .warn {
    margin: 0; font-size: var(--text-xs); line-height: 1.4; color: var(--text-muted);
    padding: var(--space-2); border-radius: var(--radius-sm);
    border: 1px solid color-mix(in srgb, var(--ls-red) 40%, var(--edge));
    background: color-mix(in srgb, var(--ls-red) 8%, var(--metal-0));
  }

  .row { display: flex; gap: var(--space-2); }
  .btn { flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-2); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer; }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn:focus-visible, .prow:focus-visible, .modebtn:focus-visible, .scopebtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .status { margin: 0; font-size: var(--text-xs); line-height: 1.5; }
  .status[data-phase='ok'] { color: var(--ls-green); }
  .status[data-phase='failed'] { color: var(--ls-red); }

  @media (max-width: 480px) {
    .modes { grid-template-columns: 1fr; }
  }
  @media (prefers-reduced-motion: reduce) {
    .prow, .modebtn, .scopebtn { transition: none; }
  }
</style>
