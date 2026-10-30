# Hagga terrain bake

Build-time pipeline for the Hagga Basin War-Table relief. Nothing here runs on a server. Output lands in
`admin-backend/portal-nextgen/static/terrain/hagga/` and ships with the next portal release.

## How Hagga is built in the game (verified 2026-10-06, client 25690505 = 1.5.3.6)

Two layers, and the War-Table needs both:

1. **Landscape.** `Maps/Arrakis/SOC_1/Tiles/SOS-MinorErg_x{0..7}_y{0..7}.umap`, one UE5 landscape streaming proxy
   per 1016 m tile: 64 components of 127 quads, one embedded 1024x1024 `Heightmap_0` (PF_B8G8R8A8, lossless).
   Height = `((R << 8) | G) - 32768) * scaleZ / 128 + Z` (scaleZ 100), 1 sample per metre, landscape origin
   -457200 cm. This is the dunes, drainage and low ridges. It is cut flat under every terrain block.
2. **Terrain blocks.** 706 `BP_TerrainBlockActor_C` actors in the regional sublevels
   `SOC_1/SubLevels/Survival_1_{ShieldWall,Saguaro,RedD,Vista,Start,Jabal_Eifrit,Sheol,Graben,Borders_Quicksand}`
   (the `*_MTXEvent` sublevels are event-only and excluded). Each points at a `TerrainBlockDataAsset`
   (`Systems/DynamicContent/TerrainBlocks/Collection/<asset>`) that names its content folder. The rock is the
   folder's collision meshes (`SM_Collision_CB_*_Art_N`) or its in-world LOD1 proxy
   (`SM_PROXY_CB_*_Art_LOD1` / `SM_CB_*_Final_LOD1`). Never the `_UIMap` variants.

The game's own UI-map blueprint (`Cartography/Survival_1/BP_WorldMap_Meshes_Survival_5`, `hagga_placements.py`
+ `hagga_bake.py`) is a low-detail copy of both layers that lacks about 60% of the blocks. It is kept only as a
cross-check; it is not the shipped relief.

## Run (DuneExtractor, branch `wip/landscape-extract` or later)

```bash
P=<your Steam library>/steamapps/common/DuneAwakening/DuneSandbox/Content/Paks
X="dotnet DuneExtractor.dll $P"          # from your DuneExtractor build (bin/Release/net8.0)
E=$DUNE_EXTRACT_DIR   # git-ignored bulk, never committed here

$X list > $E/client-<build>-file_list.txt
# 1. landscape: manifest of the 64 tile .umap paths
$X landscape tiles.json $E/hagga-landscape-<build>/hm
$X jsonlist  tiles.json $E/hagga-landscape-<build>/json
python3 stitch_landscape.py $E/hagga-landscape-<build> hagga_landscape_cm.npy
# 2. terrain blocks: the 9 regional sublevels, then their 126 data assets
$X jsonlist sublevels.json $E/cb-placements/json
$X jsonlist tb-assets.json $E/tb-assets/json            # Collection/<asset>.uasset for every asset seen
python3 collect_terrain_blocks.py $E/cb-placements/json $E/tb-assets/json $E/client-<build>-file_list.txt <blocks>
$X manifest <blocks>/tb-lod1-manifest.json      $E/cb-placements/meshes
$X manifest <blocks>/tb-collision-manifest.json $E/cb-placements/meshes-collision
# 3. bake (writes static/terrain/hagga/ by default; --out to bake elsewhere first)
python3 composite_blocks.py --landscape hagga_landscape_cm.npy --blocks <blocks> \
  --lod1-meshes $E/cb-placements/meshes --collision-meshes $E/cb-placements/meshes-collision \
  --client <build> --preview-dir <outside static/>
python3 preview_drape.py <preview-dir>/hagga_composite_cm.npy ../../website/assets/HaggaBasin.webp out.png 1
```

After a Funcom update: re-run everything and diff the landscape and block JSON against the last build. The
2026-10-06 re-extraction (25611704 -> 25690505) was byte-identical for every Hagga mesh.

## Shipped outputs

| File | What |
|---|---|
| `hagga.bin` | 1024x1024 Uint16 LE height raster (row 0 = north), same contract as the DD layouts |
| `hagga.json` | grid, worldOrigin -457200, worldSpan 812800, z range, heightScale 1.0, normal map name, source |
| `hagga_normal.webp` | 2048x2048 tangent-space normal map of the full-detail composite (lighting detail) |
| `manifest.json` | bakeStamp, layout 0, sha |

Heights are true scale (heightScale 1.0). The UI-map bake needed exaggeration because it was missing the rock;
the real composite does not. Outer Shield Wall vistas reach about 1 km and are clamped at `--clip-top-m` 450.

## Checks

- `hagga-nw-contour-check.png` (docs mockups): composite contours over the game map art in the northwest; rock
  outlines match. Whole-basin edge phase correlation is unreliable in rock (painted shading); use contour
  overlays and the owner's in-game screenshots for spot checks.
- Quicksand blocks (`CB_DeepD_QuickS_S_*`, 42 placed) carry no art and are intentionally not rasterized.
