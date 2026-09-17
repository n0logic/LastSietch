<script>
  import { karum, loadCatalog, postRequest, uuidv4 } from '$lib/karum.svelte.js';
  import KarumItemSearch from './KarumItemSearch.svelte';
  import { GRADE_CHOICES, gradeLabel, tierGradeLabel, wantedGradeLabel } from './grade.js';

  let { onClose } = $props();

  let templateId = $state(null);
  let quantity = $state('1');
  let price = $state('');
  // null is ANY GRADE, which is the contract every order carried before 2026-08-25 and
  // still the default. 0 is Base and is a REAL request, so every check against this is
  // `== null` and never a falsy test.
  let grade = $state(null);
  let gradeMode = $state('exact');
  let busy = $state(false);
  let phase = $state('idle');
  let message = $state('');
  let uuid = uuidv4();

  let options = $derived((karum.catalog.items || []).map((item) => ({
    key: item.template_id,
    name: item.name,
    template: item.template_id,
    category: item.category,
    tier: item.tier,
    detail: `${item.tier != null ? `T${item.tier} | ` : ''}${item.is_gradeable ? 'Base-G5 | ' : ''}max stack ${Number(item.max_stack || 1).toLocaleString()}`,
  })));
  let chosen = $derived((karum.catalog.items || []).find(
    (item) => item.template_id === templateId
  ) || null);
  let gradeable = $derived(chosen?.is_gradeable === true);
  let quantityNum = $derived(Math.floor(Number(quantity)) || 0);
  let priceNum = $derived(Math.floor(Number(price)) || 0);
  let maxStack = $derived(Number(chosen?.max_stack) || 1);
  let quantityOk = $derived(quantityNum >= 1 && quantityNum <= maxStack);
  let priceOk = $derived(priceNum >= 1 && priceNum <= (karum.caps.max_price || 900000000));
  let funded = $derived(karum.bank == null || karum.bank >= priceNum);
  let capLeft = $derived(Math.max(
    0, (karum.caps.listings_per_day || 0) - (karum.caps.listed_today || 0)
  ));
  let canPost = $derived(!busy && chosen != null && quantityOk && priceOk && capLeft > 0);
  // A grade only travels for a template that HAS grades. The server refuses the other
  // case outright, so sending it would just be a 400 the player never asked for.
  let sentGrade = $derived(gradeable ? grade : null);
  // 'at least' is meaningless for Base (everything is at least Base) and for Any, so the
  // toggle only appears from G1 up, and the mode sent is pinned to exact below that.
  let modeApplies = $derived(gradeable && grade != null && grade > 0);
  let sentMode = $derived(modeApplies ? gradeMode : 'exact');

  $effect(() => { if (karum.catalog.status === 'idle') loadCatalog(); });

  // Reset on the SELECT handler rather than in an $effect watching templateId: an effect
  // that writes state it reads is how a rune loop starts, and the grade of the item you
  // just replaced is never the grade you meant for the new one.
  function pickTemplate(value) {
    templateId = value;
    grade = null;
    gradeMode = 'exact';
  }

  async function submit() {
    if (!canPost) return;
    busy = true;
    phase = 'idle';
    message = '';
    const result = await postRequest({
      templateId: chosen.template_id, stackSize: quantityNum, price: priceNum,
      qualityLevel: sentGrade, qualityMode: sentMode, uuid,
    });
    if (result.ok) {
      phase = 'ok';
      message = `Wanted order posted for ${chosen.name}${sentGrade == null ? '' : ` at ${wantedGradeLabel(sentGrade, sentMode)}`}.`;
      uuid = uuidv4();
      setTimeout(() => onClose?.(), 1000);
    } else if (result.deferred) {
      phase = 'deferred';
      message = result.message;
    } else {
      phase = 'failed';
      message = result.message;
    }
    busy = false;
  }
</script>

<div class="form">
  <p class="head mono">Post a wanted order</p>

  <!-- The flags default to false and are only filled in by loadOverview, so this line
       waits for the overview to answer. Until then the gate state is unknown, and the
       stall renders (and this form opens) while the overview is still in flight: saying
       "not open yet" there is a false statement about a gate that is live. -->
  {#if karum.status === 'ready' && !karum.flags.karum_wtb_enabled}
    <p class="soon" role="note">Wanted orders are not open yet. Nothing will be posted while the gate is dark.</p>
  {/if}

  {#if karum.catalog.status === 'loading'}
    <p class="hollow mono">reading the item catalogue</p>
  {:else if karum.catalog.status === 'error'}
    <p class="hollow">The item catalogue could not be read. Try again shortly.</p>
  {:else}
    <KarumItemSearch id="karum-request-item" {options} selected={templateId}
                     onSelect={pickTemplate} label="Wanted item"
                     placeholder="Search the item catalogue" />

    {#if gradeable}
      <div class="grades" role="group" aria-label="Requested grade">
        <span class="fld-label">Grade</span>
        <div class="chips">
          <button class="chip" class:on={grade == null} type="button" aria-pressed={grade == null}
                  onclick={() => (grade = null)}>Any</button>
          {#each GRADE_CHOICES as choice (choice.value)}
            <button class="chip" class:on={grade === choice.value} type="button"
                    aria-pressed={grade === choice.value}
                    onclick={() => (grade = choice.value)}>{choice.label}</button>
          {/each}
        </div>
        {#if modeApplies}
          <div class="chips" role="group" aria-label="Grade rule">
            <button class="chip mode" class:on={gradeMode === 'exact'} type="button"
                    aria-pressed={gradeMode === 'exact'}
                    onclick={() => (gradeMode = 'exact')}>Exactly {gradeLabel(grade)}</button>
            <button class="chip mode" class:on={gradeMode === 'min'} type="button"
                    aria-pressed={gradeMode === 'min'}
                    onclick={() => (gradeMode = 'min')}>{gradeLabel(grade)} or better</button>
          </div>
        {/if}
        <small class="grade-note">
          {grade == null
            ? 'Any grade fills this order, including Base.'
            : gradeMode === 'min' && grade > 0
              ? `Only ${gradeLabel(grade)} and above can fill it.`
              : `Only ${gradeLabel(grade)} can fill it. Nothing higher or lower.`}
        </small>
      </div>
    {:else if chosen}
      <p class="grade-note plain">This item does not come in grades.</p>
    {/if}

    <div class="fields">
      <label class="fld"><span>Exact quantity</span>
        <input type="number" min="1" max={maxStack} step="1" inputmode="numeric"
               bind:value={quantity} disabled={!chosen} />
        <small>{chosen ? `One stack, maximum ${maxStack.toLocaleString()}` : 'Choose an item first'}</small>
      </label>
      <label class="fld"><span>Total offer</span>
        <input type="number" min="1" max={karum.caps.max_price} step="1" inputmode="numeric"
               bind:value={price} placeholder="Solari" />
        <small>The full amount for the whole stack</small>
      </label>
    </div>
  {/if}

  {#if chosen && priceNum > 0}
    <div class="preview" data-funded={funded}>
      <div>
        <span class="preview-name">{chosen.name}</span>
        <span class="preview-meta mono">
          wanted x{quantityNum.toLocaleString()}
          {#if gradeable}&middot; {tierGradeLabel(chosen.tier, null) || ''}{sentGrade == null ? ' any grade' : ` ${wantedGradeLabel(sentGrade, sentMode)}`}{/if}
        </span>
      </div>
      <div class="offer mono">{priceNum.toLocaleString()} <span>total</span></div>
      <span class="funding mono">{funded ? 'funded now' : 'low funds'}</span>
    </div>
  {/if}

  {#if !funded}
    <p class="gate" role="note">You can post this order, but it cannot be filled until your bank holds the full offer.</p>
  {/if}
  <p class="note">
    Posting reserves no Solari. A fill succeeds only if the full amount is in your bank.
    Version 1 accepts one exact stack of the grade you ask for.
  </p>
  <p class="note mono">{capLeft} of {karum.caps.listings_per_day} posts left today</p>

  <div class="row">
    <button class="btn primary" type="button" onclick={submit} disabled={!canPost}>
      {busy ? 'Posting' : 'Post wanted order'}
    </button>
    <button class="btn" type="button" onclick={() => onClose?.()}>Close</button>
  </div>

  {#if phase !== 'idle' && message}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
  {/if}
</div>

<style>
  .form { display: flex; flex-direction: column; gap: var(--space-3); }
  .head { margin: 0; font-size: var(--text-xs); color: var(--accent); text-transform: uppercase; letter-spacing: .1em; }
  .soon, .hollow, .note { margin: 0; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.45; }
  .soon { color: var(--accent-text); }
  .grades { display: flex; flex-direction: column; gap: var(--space-2); min-width: 0; }
  .fld-label, .fld > span { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .chips { display: flex; flex-wrap: wrap; gap: var(--space-1); }
  .chip {
    font-family: var(--font-mono); font-size: 10px; letter-spacing: .08em; text-transform: uppercase;
    padding: 3px var(--space-2); border-radius: var(--radius-sm); cursor: pointer;
    color: var(--text-muted); background: var(--metal-0); border: 1px solid var(--edge);
  }
  .chip:hover { border-color: var(--edge-hi); color: var(--text); }
  .chip.on { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .chip:active { transform: translateY(1px); }
  .chip.mode { text-transform: none; letter-spacing: .04em; }
  .grade-note { font-size: 10px; color: var(--text-muted); line-height: 1.45; }
  .grade-note.plain { margin: 0; }
  .fields { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: var(--space-3); }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .fld input { font-family: var(--font-mono); font-size: var(--text-sm); color: var(--text); background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: var(--space-2); }
  .fld input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .fld input:disabled { opacity: .5; }
  .fld small { font-size: 10px; color: var(--text-muted); text-transform: none; letter-spacing: normal; }
  .preview { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: var(--space-3); padding: var(--space-2) var(--space-3); background: var(--bg-deep); border: 1px solid var(--edge); border-radius: var(--radius-sm); }
  .preview[data-funded='true'] { border-color: color-mix(in srgb, var(--ls-ibad) 42%, var(--edge)); }
  .preview-name, .preview-meta { display: block; }
  .preview-name { font-size: var(--text-sm); color: var(--text); }
  .preview-meta { font-size: 10px; color: var(--text-muted); }
  .offer { color: var(--accent-bright); font-variant-numeric: tabular-nums; }
  .offer span, .funding { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; }
  .funding { color: var(--text-muted); }
  .preview[data-funded='true'] .funding { color: var(--ls-ibad); }
  .gate { margin: 0; padding: var(--space-2); font-size: var(--text-xs); color: var(--text-muted); border: 1px solid color-mix(in srgb, var(--ls-yellow) 45%, var(--edge)); border-radius: var(--radius-sm); background: color-mix(in srgb, var(--ls-yellow) 8%, var(--metal-0)); }
  .row { display: flex; gap: var(--space-2); }
  .btn { flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-2); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer; }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .btn:active { transform: translateY(1px); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .status { margin: 0; font-size: var(--text-xs); line-height: 1.4; }
  .status[data-phase='ok'] { color: var(--ls-green); }
  .status[data-phase='deferred'] { color: var(--accent-text); }
  .status[data-phase='failed'] { color: var(--ls-red); }
  @media (max-width: 40rem) {
    .fields { grid-template-columns: 1fr; }
    .preview { grid-template-columns: 1fr auto; }
    .funding { grid-column: 1 / -1; }
  }
</style>
