# dd-terrain-bake: Deep Desert War-Table relief

Build-time pipeline for the 12 Coriolis Deep Desert layouts on the portal holo table (portal 2.26.0, full detail).
Nothing here runs on a server. Outputs land in `admin-backend/portal-nextgen/static/terrain/` and ship with the next
portal release (the static tree rides the SvelteKit build).

## How the Deep Desert is built in the game (verified 2026-10-06, client 25690505)

It uses the same two-layer model as Hagga (`ops/hagga-terrain-bake/README.md`). Full RE notes:
`~/dd-re/ground/FINDINGS.md` and `~/dd-re/rocks/FINDINGS.md`.

1. **Ground.** This is real UE5 landscape. There are 24x24 cells of 1016 m. Each cell is one
   `LandscapeStreamingProxy` tile: 64 components x 127 quads and one 1024² `PF_B8G8R8A8` heightmap, so
   1017² samples at 1 m. Height is `(((R<<8)|G) - 32768) * scaleZ/128 + Z`. The layout rule comes from
   `DeepDesert_1/Layouts/DA_DeepDesert_1_Layout_NN` and `DataTables/DA_DeepDesert_1_TileLandcapeManager`:
   - Each cell uses the DA override tile (`SeaOfSand/<Fam>/SoS_<Fam>_xI_yJ`). If there is none, it falls back to
     the 4x4 base preset `SoS_Arrakis_DunesMid02` (DeepDesert region), or `DunesLow04` in the ShieldWall region
     (rows 21-23).
   - The ShieldWall region carries `HeightZOffset` +15000 cm. That tier is authored relief and the bake keeps it.
   - Tiles have no rotation, flip or scale.
   - Assembled this way, a layout is seamless apart from that tier step.
2. **Terrain blocks (rock).** Every `BP_TerrainBlockActor_C` of the layout:
   - The cluster blocks come from `m_Clusters` (`ClusterAnchor`, `WLC_*`, `ClusterVariation`). The world position
     is `WLC.m_Variations[var].TerrainBlocks[].Location + anchor * 101600`.
   - The 497 static blocks are the same in every layout: `DeepDesert_ShieldWall_01` (490), `DeepDesert_GapFix_*`
     (4) and `DeepDesert_1_Travel` (3).
   - Meshes: the collision set (`SM_Collision_CB_*_Art_N`) when every piece is exported, else the LOD1 proxy.
     Never `_UIMap`.
   - `CB_Arrakis_Spice_Large_00` has no art and is skipped.

The ground is smoothed under the rock but not cut flat, so the surface is `max(ground, rocks)`.

Shipped layout N is DA layout 0N: the Cartography UI blueprint N matches DA 0N with a Jaccard score of 0.98+. DA
layouts 12/13/14/16/18 have no UI blueprint and are not shipped. The old July bake read the low-detail UI blueprint
(`BP_WorldMap_Meshes_DeepDesert_Layout_N`). That blueprint lacks 2-4 tiles per layout and about 4% of the blocks, and
carries only LOD1 dunes.

## Inputs: extraction (DuneExtractor, 2026-10-06 or later)

Keep all bulk on native WSL storage (`~/dd-re/...`, `$DUNE_EXTRACT_DIR`), never under `/mnt/c`
(9p is far too slow for thousands of tiles and meshes). None of it is committed here.

```bash
P=<your Steam library>/steamapps/common/DuneAwakening/DuneSandbox/Content/Paks
X="dotnet DuneExtractor.dll $P"          # from your DuneExtractor build (bin/Release/net8.0)
R=~/dd-re                                 # native WSL, not /mnt/c
$X list > $R/client-<build>-file_list.txt

# DD maps, data tables, the 17 DA layouts, the 3 static-block sublevels (+ the WLC_* cluster assets)
$X jsonlist m1.json $R/rocks/json         # Maps/Arrakis/DeepDesert_1/{DataTables,Layouts,SubLevels}/**
$X jsonlist m2.json $R/rocks/json         # Maps/Arrakis/SeaOfSand/WLC_*.uasset (+ the SoS tile .umaps)
$X jsonlist m3.json $R/rocks/tb-json      # Systems/DynamicContent/TerrainBlocks/Collection/<asset>.uasset (68)

# ground tiles: tiles.json = the 124 tile .umaps (SeaOfSand/** + Biomes/Arrakis/SoS/heightmap/SoS_Arrakis_Dunes*)
$X jsonlist  tiles.json $R/ground/tilejson
$X landscape tiles.json $R/ground/hm
$X raw Maps/Biomes/Arrakis/SoS/heightmap/SoS_Arrakis_Dunes $R/ground/raw
$X raw Maps/Arrakis/SeaOfSand/ $R/drape/raw-sos      # override-tile .ubulk: their weightmaps (drape sand tone), 1.7 GB
```

**Heightmap gotcha.** In the SeaOfSand tiles the heightmap export is named `Heightmap_0`, so `landscape` writes a PNG
for it. In the 32 base presets (`SoS_Arrakis_Dunes*`) it is an unnamed `Texture2D_N`
(`LODGroup TEXTUREGROUP_Terrain_Heightmap`), and `landscape` exports nothing. `dd_ground.py` handles this case:
- It finds the texture through `LandscapeComponent.HeightmapTexture`.
- It reads mip 0 straight from the package `.ubulk` (`raw` output) at `Mips[0].BulkData.OffsetInFile`, in B,G,R,A
  order.
- This is validated bit-for-bit against the PNG path on `SoS_Basic_01_x0_y0`.

The proper extractor fix is to match on the LODGroup instead of the `Heightmap*` name.

Blocks and meshes:

```bash
python3 collect_dd_blocks.py --json-root $R/rocks/json --tb-json $R/rocks/tb-json \
  --file-list $R/client-<build>-file_list.txt --out $R/rocks/blocks
$X manifest $R/rocks/blocks/tb-collision-manifest.json $R/rocks/meshes-collision   # 1688 meshes
$X manifest $R/rocks/blocks/tb-lod1-manifest.json      $R/rocks/meshes-lod1
```

The mesh export writes each GLB's material instance (`MI_PROXY_*.json`) and textures (`T_PROXY_CB_*_Art_LOD1_D.png`)
next to it; the drape colours rock from those. Three LOD1 proxies in the Hagga export (`Arrakis_Generic_S_08`,
`Arrakis_Generic_S_20`, `ShieldW_Slope_S_01`) came without their material JSON. Re-export them to
`$R/drape/lod1-extra` (`$X manifest extra.json $R/drape/lod1-extra`). They use `M_SimpleVertexColor` with all-zero
vertex colours, so the drape leaves them uncoloured and fills them from the nearest textured rock.

The Hagga run's `extracted/cb-placements/meshes*` already holds 955 of these meshes, and `--mesh-dirs` searches both.
The file list must come from the same build: the 2026-05 `extracted/file_list.txt` resolves no meshes.

## Run

```bash
python3 bake_dd_full.py --client <build> --layouts 0 --preview-dir ~/dd-re/bake-previews    # check one first
python3 bake_dd_full.py --client <build> --layouts 0-11 --preview-dir ~/dd-re/bake-previews
```

- The defaults point at `~/dd-re/{ground,rocks}` and `$DUNE_EXTRACT_DIR/cb-placements`. See
  `--help`.
- Deps: numpy, scipy, Pillow (with WebP). Height GLBs are read with `ops/hagga-terrain-bake/hagga_bake.py`; the
  drape reads UVs and vertex colours with `dd_drape.read_glb`.
- `--cache` (`~/dd-re/bake-cache`) holds the decoded 1 m tiles (`tiles/<name>.npy`, about 4 MB each) and the
  static-block height and colour rasters. With a warm cache each layout takes about 30 s (heights, normals, drape).
- Each layout prints one JSON stats line: ground range, raw composite max, clip fraction, basin max (rows 0-19),
  block counts and file sizes.
- `manifest.json` is rebuilt at the end from every `layout_N.bin` present. It warns if raster grids are mixed, so
  bake all 12 before shipping.

Main options:

| Flag | Default | Why |
|---|---|---|
| `--ground-px-m` | 4 | Work grid 6096², 4 m/px. The ground is a box mean of the 1 m samples. Rocks are splatted at the same grid. |
| `--clip-top-m` | 600 | Basin rock tops are about 200 m, the Shield Wall face (rows 21-22) reaches about 600 m, and the row-23 vistas reach 1.76 km. 600 keeps the wall face and flattens only the vista strip (2.7% of pixels). The u16 step is about 0.95 cm. |
| `--grid` | 1024 | Shipped height raster. |
| `--normal-grid` | 2048 | Normal map. |
| `--height-scale` | 1.0 | True scale. The client adds `RELIEF_BOOST` 2 (`?holoRelief=X` to try others). The basin rocks land at about 16 board units and the clipped wall at about 51. |
| `--drape-grid` | 4096 | Map-art drape, about 6 m/px. |
| `--drape-gain` | 1.0 | `meta.drapeGain`, the shader exposure (Hagga uses 1.0 too). |
| `--hagga-art` | `website/assets/HaggaBasin.webp` | The sand palette is sampled from its open southern dunes, so DD and Hagga read as the same game map. |
| `--save-npy` | off | Writes the unclipped 6096² composite (cm, 150 MB) to the preview dir. |

## Outputs (`static/terrain/`, the terrain.js contract)

| File | What |
|---|---|
| `layout_N.bin` | 1024x1024 Uint16 LE, row 0 = min world Y (north), `z = zMinCm + u16/65535*(zMaxCm-zMinCm)`, 2.1 MB |
| `layout_N.json` | `grid, worldOrigin [-1270000]*2, worldSpan [2438400]*2, zMinCm, zMaxCm, floorCm (= zMin), heightScale, normalMap, normalGrid, source{client, daLayout, blocks, clipTopM, ...}` |
| `layout_N_normal.webp` | 2048² tangent-space normal map of the clipped 4 m composite: R=-dh/dx, G=-dh/dy, B=nz, rows = world +Y, q90, about 1.15 MB |
| `layout_N_drape.webp` | 4096² RGB unshaded map art, q88, about 0.8 MB (see "Drape" below) |
| `manifest.json` | `bakeStamp` (terrain.js `?v=`), `raster [1024,1024]`, `heightScale`, `fingerprints`, `layouts[{id,bin,meta,sha}]` |
| `fingerprints.json` | **Never written by this bake.** `admin-backend/dd_layout_match.py` identifies the live layout from it. Its sha256 is pinned in `scripts/tests/test_dd_terrain_artifacts.py` and `test_dd_layout_rotation.py`. |

- The frame is the frozen `map_model` deep-desert calibration, so markers align.
- There is no coverage mask any more: the full 24x24 is landmass, and the loader never applied the old
  `layout_N.mask`.
- On the client, DD is draped like Hagga: `terrain.js` gives the deep-desert source `drape: true` +
  `bakeDrapeOnly` (only `meta.drape`, never the flat 2D islands backdrop; a bake without `meta.drape` keeps the
  hologram). The material lights the drape with the normal map and keeps the 9x9 sector grid and labels.
  `RELIEF_BOOST` stays 2: true scale keeps slopes true, but the 24.4 km board is framed ~3x wider than Hagga's
  8.1 km, so 2x is what makes the Shield Wall read as a cliff. Meshes 511 segments on high, 383 mid, 95 low.

## Drape (`dd_drape.py`)

There is no flat DD map texture in the game: its map is drawn from the Cartography blueprint meshes. The bake
synthesizes unshaded albedo from game data instead:

- **Rock.** Every block's LOD1 proxy is rasterized top-down with its baked diffuse texture: barycentric UV per splat
  sample, and the topmost surface wins (the same max-z rule as the heights). Collision-only geometry takes the
  colour of the block's LOD1 proxy, which shares the transform. Rock is painted only where the rock is the visible
  surface (rock z >= ground). In client 25690505 about 98% of the visible rock gets its texture colour; the rest
  takes the nearest coloured rock within 240 m, else the median rock colour.
- **Sand.** A shadow / mid / light palette is sampled from the in-game Hagga art's southern dunes. It is toned by the
  SoS landscape paint layers, decoded from each tile's `Weightmap_*` textures in the same `.ubulk` as the heightmap
  (`dd_ground.TileSource.weights`): `Arrakis_SoS_Flat` (lighter flats), `Arrakis_SoS_Wind`, `Arrakis_SoS_Occluded`
  (darker troughs), and a faint `ColorMask_R/G/B` hue drift.
- **Shield Wall ground.** The ShieldWall-region ground gets a stone tint (`STONE_RGB`).

The client adds the lighting, so nothing in the drape is shaded.

Never committed: tiles, raw `.ubulk`, JSON dumps, meshes, the cache, `.npy` composites and previews. Previews go to
`--preview-dir` (default `~/dd-re/bake-previews`), outside `static/`.

After a Funcom update:
1. Re-extract everything.
2. Diff the DA layouts, the manager, the WLC and sublevel JSON against the last build.
3. Re-bake all 12.
4. Re-run `scripts/tests/test_dd_terrain_artifacts.py` and `npm run smoke`.

A new `bakeStamp` is the cache bust.

## Checks

- Ground: `dd_ground.assemble` at 4 m matches the RE prototype `~/dd-re/ground/proto/layout_0_2m_cm.npy` (same 78
  tiles, corr 0.99999).
- Blocks: the `collect_dd_blocks.py` port reproduces `~/dd-re/rocks/blocks` exactly. Layout 0 uses 855 collision
  meshes and 31 LOD1, has 4 art-less blocks, and 53 vista blocks are more than 1 km out of frame. These numbers match
  rocks/FINDINGS.
- Orientation: the Shield Wall lands at the bottom (cells 21-23) and the rock clusters sit on the old island
  footprints (rocks/FINDINGS overlay).

## History: `bake_islands.py` and `base-july/`

`bake_islands.py` (2026-09-02) still renders the 2D map backdrops (`admin-backend/static/img/maps/dd-islands/`).
Its `--holo` mode folded 2D-mask islands into the 384² July LOD1 field (`base-july/`). It is retired and now
hard-fails, because it would overwrite this bake. `base-july/` is kept only as the record of that field.
