# Overmap bake (Sand Table data layer)

Build-time pipeline for the Sand Table world view (spec the design notes (not published) sections 2-5
and 10, plan section 4). Nothing here runs on a server. Output lands in `admin-backend/portal-nextgen/static/world-data/`
and ships with the next portal release. Raw GLBs, JSON dumps, the 16 MB texture and previews are never committed.

## How the overmap is built in the game (verified 2026-10-06, client 25690505 = 1.5.3.6)

- `Systems/Overmap/Overmap.umap` holds the places: 21 `BP_OvermapPoi_C` (localized name, description and category
  from `OvermapPoiComponent`), 34 `BP_TravelDestination_C` and 21 `BP_IngameText_C` labels. 76 in all.
- `Overmap_Geometry.umap` places the relief: 22 `SM_OvermapOutputCollisionMesh_*` tiles, 22 visual
  `SM_OvermapOutputMesh_*`, 19 `SM_OvermapOutputIsolineMesh_*`, 22 editor `SM_OvermapInputMesh_*` and 9
  `SM_Overland_BackgroundPlane`. Collision, visual and isoline tiles all sit at the identity transform (authored in
  overmap cm).
- The 3 `Overmap/Sublevels/*.umap` (ArrakeenSpacePort, OldQuarry, WindPass) place **no relief**. Each adds one POI
  with its miniature, one travel destination, one label and a travel-volume disc (Arrakeen Spaceport, The Old Quarry,
  Wind Pass), shipped as `sublevelPlaces` with no key. `Overmap_Audio`, `_Light` and `_VFX` place no relief either.
- **Sublevel transform = identity.** `Overmap.umap` streams nothing through `LevelStreaming*` (World.StreamingLevels is
  empty). It uses World Composition: the raw zen export `DuneWorldComposition_1` (not decoded by DuneExtractor's
  JSON) lists 7 tiles (Audio, Geometry, Light, VFX and the 3 sublevels), and every `FWorldTileInfo.Position` is
  (0,0,0). `LevelInstance_0` ("Overmap", at (66574,-52344,100), yaw -90) is an editor level instance embedded at cook
  time with no `WorldAsset`, so it moves nothing. The Old Quarry sits at (22656,-11266) and Wind Pass at
  (-34365,-15879), both on the south rim of the Hagga landmass, and the Spaceport sits 6 km from Arrakeen. Checked
  2026-10-06 against the owner's in-game screenshots: a top-down render of the placed visual terrace tiles around
  both points matches the terrain around the in-game markers (Old Quarry: a low shelf to the W and a terraced ridge
  running SE; Wind Pass: a narrow ridge just S, a big cliff to the N). They are **not** on the oblong bottom island.
- **The 23 locations** (`locations` in `overmap-places.json`): every POI from Overmap.umap and the sublevels, merged by
  localized name. Arsunt Garrison has two POI actors 352 cm apart (Atreides and Harkonnen faction outposts) and is one
  location. 2 regions, 2 settlements (Poi.Location.Cities), 6 story, 7 landmarks (Poi.PointsOfInterest), 6 dungeons.
  The bake fails on any other split.
- Collision tiles 001-038 in `OutputMeshes/` are placed by none of the 8 overmap levels. Rendered at identity they
  form an older layout that matches neither the art nor the placed relief, so they are skipped (listed in the
  manifest as `unplacedTilesSkipped`).
- The oblong island at the bottom of `T_UI_GlobalMap_D` (about X -26k..33k, Y 71k..95k cm) is in no placed
  collision tile and no placed visual mesh. Only the unplaced visual tiles 029-031 (the older 001-038 layout) cover
  it. In this build it exists only in the painted art. The isolines around it are placed, so the contours show it.
  It has **no geometry**: the Sand Table draws it as art + contours only (`relief.artOnlyIsland` in the manifest).
  No location sits on it. The owner confirmed on 2026-10-06 that Wind Pass and The Old Quarry are in the south, The Old
  Quarry the southernmost (Y -11266, the largest Y of all 23), which matches the identity sublevel placement.

## Transform and calibration

- glTF from DuneExtractor to UE local: `(x, z, y) * 100`, then each component's RelativeScale3D, RelativeRotation
  and RelativeLocation, following `AttachParent`.
- Map art (blueprint `BP_MapLayer_Global`, never the FFT fit): `px = (X/250 + 996.984) * 2.048`,
  `py = (Y/250 + 613.10) * 2.048` on the 4096x2048 art. The 2048 webp covers the overmap cm rect
  x -249246..250754, y -153275..96725 (`overmap-art.json`).

## Memory rules (read first)

On 2026-10-06 two agents died: the kernel OOM-killed a python process at 28.8 GB RSS on the 30 GB box (the old
`overmap_terraces.py` loaded all 22 visual tiles, 6.6M triangles, as float64 and rasterised them at once) and took
the agent's tmux pane with it. Every step below therefore:

- runs under a cgroup cap so only the job dies: `systemd-run --user --scope -q -p MemoryMax=12G -p MemorySwapMax=0
  -- <cmd>` (fallback `prlimit --as=12000000000 -- <cmd>`); check `free -g` first;
- never loads all overland tiles at once: `overmap_terraces.py` reads ONE visual tile, writes it, lets the gltfpack
  CLI simplify it, frees it, and only merges the already-decimated tiles (peak RSS about 0.3 GB);
- never builds dense full-overland rasters: previews crop a window and use float32;
- if a job is killed, note the size it reached and pick a smaller approach rather than retrying.

The same cap applies to DuneExtractor (`dotnet DuneExtractor.dll ...`).

## Run

```bash
E=$DUNE_EXTRACT_DIR/global-map-25690505   # DuneExtractor jsonlist + mesh + png dump
M=~/dd-re/overmap/meshes                                     # visual OutputMeshes + POI miniatures + materials
G=~/dd-re/overmap/tools/node_modules/.bin/gltfpack           # pinned: npm install --save-exact gltfpack@1.3.0
CAP="systemd-run --user --scope -q -p MemoryMax=12G -p MemorySwapMax=0 --"
# order matters: the later bakes read overmap-places.json and register in world-manifest.json
$CAP python3 overmap_bake.py --extract $E --client 25690505 --gltfpack $G --preview-dir ~/dd-re/overmap/previews
$CAP python3 overmap_miniatures.py --extract $E --meshes $M --textures ~/dd-re/overmap/tex --gltfpack $G --client 25690505
$CAP python3 overmap_fx.py --extract $E --json ~/dd-re/overmap/json --client 25690505
$CAP python3 overmap_terraces.py --extract $E --visual-meshes $M/DuneSandbox/Content/Dune/Systems/Overmap/Meshes/OutputMeshes \
  --gltfpack $G --client 25690505
$CAP python3 overmap_hires.py --extract $E --visual-meshes $M/DuneSandbox/Content/Dune/Systems/Overmap/Meshes/OutputMeshes \
  --work ~/dd-re/overmap/hires/work --client 25690505 --albedo-only   # ~50 s, peak RSS 1.6 GB; cached stages in --work. --albedo-only is what ships: no baked light
$CAP python3 overmap_contact_sheet.py --extract $E --meshes $M --textures ~/dd-re/overmap/tex \
  --out ~/dd-re/overmap/previews/miniatures-contact-sheet.png      # preview only, never under admin-backend/
python3 ../../scripts/tests/test_world_artifacts.py
```

Extraction for the new inputs (write to native WSL storage under `~/dd-re/overmap/`, never `~/Source`), e.g.
`cd <your DuneExtractor build>/bin/Release/net8.0 && $CAP dotnet DuneExtractor.dll
<your Steam library>/steamapps/common/DuneAwakening/DuneSandbox/Content/Paks png T_MLS_Metal_Atre_Bronze_0
~/dd-re/overmap/tex`. `~/dd-re/overmap/json` holds Overmap_VFX/Audio/Light and the FX assets (NS_*, M_*, MI_*,
BP_*), `~/dd-re/overmap/meshes` the 59 visual OutputMeshes, the SM_OvermapPoi_* miniatures and their materials,
`~/dd-re/overmap/raw` the raw zen `.umap` files (used once to read `DuneWorldComposition_1`).

Every bake hard-fails on a missing input mesh, a GLB whose buffer does not fill its BIN chunk, a place count other
than 76, a location split other than 2/2/6/7/6, or a location with no miniature of its own. Check every exported GLB
anyway. **SM_OvermapOutputMesh_055:** on 2026-10-06 DuneExtractor's export under `~/Source` (the Windows-mounted
drive) truncated it at exactly 6 MiB, on every re-run. Re-exported alone to native ext4 (`~/dd-re/overmap/meshes55`;
the copy in `meshes/` is byte-identical) it is whole: 10,775,516 bytes, buffers == BIN, 90,560 verts. Keep exporting to native
storage, and if a tile ever reads short, export it alone with `mesh SM_OvermapOutputMesh_055`.

After a Funcom update: re-extract, re-run, and diff `overmap-places.json` and the manifest. Every shipped file is
byte-deterministic for the same input (checked 2026-10-06 by re-running the whole chain).

## Shipped outputs (`static/world-data/`)

| File | What |
|---|---|
| `overmap-relief.glb` | 22 placed collision tiles merged, welded per tile, positions + indices only. gltfpack `-c` (EXT_meshopt_compression + KHR_mesh_quantization); load with three's `MeshoptDecoder`. Units overmap metres: x = X east, y = Z up, z = Y south. No normals: compute after applying vertical exaggeration. **Fallback** for weak tiers. |
| `overmap-terrace.glb` | The terraced relief the in-game overland draws: the 22 placed visual `SM_OvermapOutputMesh_*` tiles, each simplified alone by gltfpack (`-noq -slb -si 0.06 -se 0.0005`; `-slb` locks tile borders so no cracks), merged, then `-c -vp 16`. 6.6M -> 880k triangles, 2.66 MB. Same frame as the relief; positions + indices, compute flat normals for the pale terrace edges and long shadows. `-se 0.0008` (1.6 MB) visibly erases the low steps; `0.0003` is 3.7 MB. |
| `overmap-terrace.json` | Its stats, sha, gltfpack args and per-tile triangle counts |
| `overmap-contours.json` | Isoline ribbon (20 cm) centrelines, Douglas-Peucker 120 cm, flat `[x0, y0, x1, y1, ...]` int arrays in overmap cm |
| `overmap-art-2048.webp` | `T_UI_GlobalMap_D` downscaled to 2048x1024 |
| `overmap-art.json` | the blueprint transform for the webp |
| `overmap-places.json` | 76 places (kind, label, id, name, category, description, travelId, position cm, key), the `stones` map-key table, `sublevelPlaces`, `sublevelTransform` (identity) and the 23 `locations` |
| `overmap-miniatures.glb` | Every location's own in-game miniature: root node `loc_<id>` at the location anchor (overmap metres), children are the game's mesh placements relative to it (instance node `<id>_<k>_<mesh>`). 19 unique meshes, 69k triangles, the game's base-colour textures (Atre bronze for M_OverlandMap_POI, Choam aluminium x FlatColorTint for MI_OverlandMap_POI_02, crystal + pink emissive for the erythrite) as 256 px JPEG on UV0. gltfpack `-c -kn -km`. 0.96 MB. |
| `overmap-miniatures.json` | Per location: meshes (with `sharedWith`), source actors, travel disc (centre, radius from the `BP_POI_SphericalTravel` plane), label (in-game text, position, style `gold3d` for regions/settlements, `caption` otherwise), plus the disc colours (`MPC_POI_TravelVolumes`), label styles, materials, the 4 `BP_Pointers` travel splines and the POI meshes no level places |
| `overmap-locations-fx.json` | Per location (offset from its anchor): Niagara systems with decoded per-instance user parameters, point lights, decals, FX meshes (arena light cones, darkness planes, ornithopters, sandbike), FX blueprints (flickering light, spice blooms, primrose field, toxic pool, mesh-following splines with their paths), the erythrite glow; summaries of every system (emitters, renderers, materials, user defaults), material and blueprint (ticks, timelines); a re-creation note per effect. No skeletal animation or rotating component exists in any overmap level or FX blueprint. |
| `overmap-hires.webp` | **Shipped as `--albedo-only`: no Lambert term, no cast shadows, no crease; the scene's own lights (time-of-day presets) do all the shading, so only the paragraph's art colour and pale rims apply.** Full-lit bake (not shipped): 8192x4096 top-down overland over the exact art frame (61.04 cm/px, 2x the 4096 art). The 22 placed visual tiles (6.6M tris) rasterised ONE AT A TIME, max-z, 2x supersampled (30.5 cm/px), then shaded in 1024 px tiles with a halo: Lambert from a low NW sun (az 315, alt 22, ambient 0.52, sunlit risers capped at 1.6x), soft cast shadows (horizon march), pale terrace rims (a pixel standing 25 cm above a neighbour; the step is 63.45 cm) faded where they crowd, a faint crease at each riser foot. Colour is `T_UI_GlobalMap_D` upsampled; where the geometry has relief (15 px height range above 1.2-5 m) the art is reduced to a 6 px blurred tint, because its painted rocks do not always sit where the placed geometry is (Wind Pass). Flat covered sand keeps the art's own detail, so there is no seam at the coverage edge, which is feathered 12 px into the plain art. Uncovered: the oblong south island and open sand (art only). Byte-deterministic. |
| `overmap-hires-4096.webp` | the same, Lanczos to 4096x2048, for weak tiers |
| `overmap-hires.json` | its transform (same `worldRectCm`, layer offset and plane as `overmap-art.json`), cm/px, low tier, shading constants, coverage |
| `world-manifest.json` | bakeStamp, client build, bytes + sha256 per file (all 13), calibration, relief/contour/places stats, `extraBakes` (miniatures, fx, terrace, hires) |

### Miniatures: shared meshes (evidence, not stand-ins)

Each location's miniature is whatever its own POI actor (`BP_OvermapPoi_C` component `SM_OvermapPoi*`, or the
sublevel's `StaticMesh`) and the StaticMeshActors within 30 m place. `BP_OvermapPoi`'s default mesh is
SM_OvermapPoi_HarkoVillage, but every instance's `StaticMesh` component overrides it to null, so nothing inherits it.

- The five Testing Stations (24, 89, 136, 152, 195) all place `SM_OvermapPoi_Dungeon_02`; in game they differ only by
  their FX (darkness plane, radiation, fire + flickering light, electric arcs, poison spores + toxic pool). The Old
  Quarry has its own `Dungeon_01`.
- Blushing Cavern and The Broodworks both place `SM_OvermapPoi_RockIsland_01`; Blushing Cavern adds 9
  `SM_Env_Mineral_Neut_Ore_Erythrite_D_02` crystals with the glowing `MI_Overland_Erythrite_Crystals`.
- Arsunt Garrison has no mesh on its two POI actors; its miniature is the two coincident `StaticMeshActor_20/22`
  (`SM_OvermapPoi_StoryOutpost_01`, one per faction outpost).
- Unplaced in this build: `SM_OvermapPoi_Dome_01, FallenLight_02, NamePlaque, NamePlaqueLong, Outpost_01,
  RefuelingMap, Shipwreck_01`.
- The game's POI materials are procedural (world-aligned bronze patina / windsack metal), so the windsack-metal pieces
  (Hagga, Arrakeen, Harko, Smuggler's Run, Water Shipper's Mansion, Hephaestus) look darker grey on UV0 than in game.

## Previews (outside the repo)

- `hires-*-vs-art.png` (Hagga, Hagga detail, polar cap, south island) and `hires-overview.png`: the game art 4096 vs
  `overmap-hires.webp` over the same world rect. Known: where two placed tiles overlap at slightly different heights
  the max-z raster shows a faint straight seam (e.g. west of the polar cap); and a few cliff edges sit over the art's
  dark sand (geometry vs painting disagree), so the lit cliff face reads warm pink there.

- `overmap-relief-hillshade.png`: relief hillshade (3x) with contours, places and sublevel places.
- `overmap-relief-vs-art.png`: the art with the relief rock edge (cyan), contours (red), POIs, and the unplaced tile
  bounds (magenta). Rock edges follow the painted rock.
- `miniatures-contact-sheet.png`: the 23 miniatures, 3/4 view, textured, on disc + ring, labelled.
- `terrace-vs-ingame.png`: full visual tiles vs the shipped terrace GLB vs the owner's in-game screenshots (Old Quarry,
  Wind Pass). Screenshots are private and never enter the repo.
- `sublevel-placement-evidence.png`, `sublevel-places-on-art.png`: the identity sublevel placement check.
