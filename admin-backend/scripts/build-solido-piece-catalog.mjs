#!/usr/bin/env node
/*
 * build-solido-piece-catalog.mjs — emit static/js/data/solido-piece-catalog.json
 *
 * CLEAN-ROOM derivation (team-lead ruling #2, 2026-06-11):
 *   This catalog copies NO data from the icehunter dune-base-market repo
 *   (which is unlicensed). It is built from:
 *     (a) factual Dune: Awakening building-grid constants — FOUNDATION_SIZE 512,
 *         FLOOR_HEIGHT 384, WALL_OFFSET 256 (UE centimetres, game facts, not
 *         copyrightable);
 *     (b) name-pattern inference over the building_type string (Foundation /
 *         Wall / Floor / Ramp / Pillar / Door / ... substrings);
 *     (c) OUR own design-system palette (faction-tinted ambers + neutrals,
 *         derived from static/css/portal/portal.css accent tokens).
 *
 *   dune.layout.tools (by icehunter) is credited as the blueprint DATA SOURCE
 *   on every Solido portal surface — that is data attribution, not code reuse.
 *
 * The viewer (static/js/portal-solido-viewer.js) consumes this JSON: for each
 * building_type it infers a category from `patterns`, looks up the category box
 * size + shade, picks a faction tint by name prefix, and multiplies grid units
 * by the grid constants to get box extents. Unknown types fall back to the
 * `default` category (a grey unit box) so the viewer never crashes.
 *
 * Run:  node scripts/build-solido-piece-catalog.mjs
 */
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(here, '../static/js/data/solido-piece-catalog.json');

// (a) Factual grid constants (UE centimetres).
const grid = { foundation_size: 512, floor_height: 384, wall_offset: 256 };

// (b) Name-pattern inference. First matching substring wins; order matters
//     (most specific first). All matched lower-cased against building_type.
const patterns = [
  ['foundation_wedge', 'foundation_wedge'],
  ['wedge', 'foundation_wedge'],
  ['triangle', 'foundation_wedge'],
  ['foundation', 'foundation'],
  ['floorlight', 'floor'],
  ['floor', 'floor'],
  ['railing_inclined', 'railing'],
  ['railing', 'railing'],
  ['ramp', 'ramp'],
  ['stairs', 'ramp'],
  ['passageway', 'wall'],
  ['window', 'wall'],
  ['wall_half', 'wall_half'],
  ['wall', 'wall'],
  ['door', 'door'],
  ['gate', 'door'],
  ['frame', 'door'],
  ['rooftop', 'roof'],
  ['roof', 'roof'],
  ['cover', 'roof'],
  ['corner', 'corner'],
  ['pillar', 'pillar'],
  ['column', 'pillar'],
  ['cistern', 'prop_large'],
  ['windtrap', 'prop_large'],
  ['refinery', 'prop_large'],
  ['extraction', 'prop_large'],
  ['fabricator', 'prop_large'],
  ['generator', 'prop'],
  ['turbine', 'prop'],
  ['deathstill', 'prop'],
  ['container', 'prop'],
  ['storage', 'prop'],
  ['placeable', 'prop'],
];

// Category box size in GRID UNITS {x,y,z} (x=UE north, y=UE east, z=UE up) +
// a `shade` brightness multiplier (0..1) applied to the faction tint so pieces
// read apart from each other in the massing view.
const categories = {
  foundation:        { size: { x: 1.0, y: 1.0, z: 0.18 }, shade: 0.72 },
  foundation_wedge:  { size: { x: 1.0, y: 0.6, z: 0.18 }, shade: 0.72 },
  floor:             { size: { x: 1.0, y: 1.0, z: 0.08 }, shade: 0.86 },
  wall:              { size: { x: 1.0, y: 0.10, z: 1.0 }, shade: 1.00 },
  wall_half:         { size: { x: 1.0, y: 0.10, z: 0.5 }, shade: 1.00 },
  door:              { size: { x: 1.0, y: 0.12, z: 1.0 }, shade: 0.92 },
  roof:              { size: { x: 1.0, y: 1.0, z: 0.10 }, shade: 0.80 },
  ramp:              { size: { x: 1.0, y: 1.0, z: 0.50 }, shade: 0.78 },
  stairs:            { size: { x: 1.0, y: 1.0, z: 0.50 }, shade: 0.78 },
  railing:           { size: { x: 1.0, y: 0.08, z: 0.30 }, shade: 1.05 },
  pillar:            { size: { x: 0.22, y: 0.22, z: 1.0 }, shade: 0.95 },
  corner:            { size: { x: 0.30, y: 0.30, z: 1.0 }, shade: 0.98 },
  prop:              { size: { x: 0.8, y: 0.8, z: 0.8 }, shade: 1.10 },
  prop_large:        { size: { x: 1.4, y: 1.4, z: 1.6 }, shade: 1.10 },
  default:           { size: { x: 0.6, y: 0.6, z: 0.6 }, shade: 0.90 },
};

// (c) OUR design-system palette. Faction tint chosen by building_type prefix.
//     Bases are sand/amber neutrals (portal accent family) + faction hints:
//     Atreides cool gold, Harkonnen rust-red, Choam bronze-tan, MTX/event
//     violet, everything else neutral sand. These are our colours, hand-picked
//     from the portal accent ramp — none copied from icehunter.
const factions = {
  atreides:  { prefixes: ['atreides', 'atre'],            color: '#c9a14e' },
  harkonnen: { prefixes: ['harkonnen', 'hark'],           color: '#b5532f' },
  choam:     { prefixes: ['choam'],                       color: '#b89055' },
  mtx:       { prefixes: ['mtx'],                          color: '#8a76b8' },
  neutral:   { prefixes: [],                               color: '#9c8a68' },
};

const catalog = {
  _meta: {
    generator: 'scripts/build-solido-piece-catalog.mjs',
    derivation: 'clean-room: factual grid constants + name inference + portal design-system palette',
    data_source_attribution: 'Blueprints sourced from dune.layout.tools (by icehunter)',
    copies_icehunter_values: false,
  },
  grid,
  patterns,
  categories,
  factions,
};

writeFileSync(OUT, JSON.stringify(catalog, null, 2) + '\n');
console.log('wrote', OUT);
