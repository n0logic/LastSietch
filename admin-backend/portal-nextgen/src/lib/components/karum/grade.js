// TIER and GRADE are two different facts and the Karum rendered them as one until
// 2026-08-25: KarumBuyDialog, KarumFillDialog, MyKarumListings and KarumCard all printed
// `T{quality_level}`, so a Grade 3 Perforator read as "T3" when the item is T6 G3. The
// exchange components next door had it right (`G{quality}`) the whole time, which is
// exactly how the two spellings drifted. This module is the ONE place either label is
// built, so the next component cannot invent a third spelling.
//
// Grades run Base(0) .. 5. Verified live 2026-08-25 against dune.items: no row anywhere
// carries a quality_level above 5, and item-data's material_cost_per_grade is 6 slots
// wide for every gradeable template. Every gradeable template in the catalogue is T6.
export const MAX_GRADE = 5;

/** Every selectable grade, Base first. Used by the wanted-order picker. */
export const GRADE_CHOICES = Array.from({ length: MAX_GRADE + 1 }, (_, g) => ({
  value: g,
  label: g === 0 ? 'Base' : `G${g}`,
}));

function num(value) {
  if (value == null || value === '' || typeof value === 'boolean') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** 'Base' | 'G3'. Null for a grade we could not read, so a caller can omit the chip
 *  entirely rather than printing a confident 'Base' over missing data. */
export function gradeLabel(grade) {
  const g = num(grade);
  if (g == null || g < 0) return null;
  return g === 0 ? 'Base' : `G${g}`;
}

/** 'T6', or null when the tier is unknown. */
export function tierLabel(tier) {
  const t = num(tier);
  return t == null ? null : `T${t}`;
}

/** The combined chip: 'T6 · G3', or whichever half we actually know. */
export function tierGradeLabel(tier, grade) {
  return [tierLabel(tier), gradeLabel(grade)].filter(Boolean).join(' · ') || null;
}

/** What a WANTED order is asking for.
 *
 *  A null level is the legacy any-grade contract and must not be flattened to 0: 'any
 *  grade' and 'Base only' are opposite promises, and rows posted before grades existed
 *  carry null forever. 'min' renders with a trailing + so a filler can tell at a glance
 *  that a better item is accepted. */
export function wantedGradeLabel(level, mode) {
  const g = num(level);
  if (g == null) return 'any grade';
  if (mode === 'min') return g === 0 ? 'any grade' : `${gradeLabel(g)}+`;
  return gradeLabel(g);
}

/** Does a held item's grade satisfy a wanted order? Mirrors _karum_quality_ok in
 *  portal.py. This is a UI convenience for filtering and preview copy ONLY; the portal
 *  refuses at the edge and the writer pins the grade inside the take transaction. */
export function gradeSatisfies(itemGrade, wantedLevel, wantedMode) {
  const want = num(wantedLevel);
  if (want == null) return true;
  const have = num(itemGrade);
  if (have == null) return false;
  return wantedMode === 'min' ? have >= want : have === want;
}
