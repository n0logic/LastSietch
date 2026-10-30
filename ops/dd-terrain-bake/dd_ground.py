"""Deep Desert ground layer: decode the SoS landscape tiles and assemble one Coriolis layout.

Each tile is one UE5 LandscapeStreamingProxy: 64 components x 127 quads, one 1024x1024 PF_B8G8R8A8 heightmap,
so 1017x1017 samples at 1 m per 1016 m tile. h_cm = (((R<<8)|G) - 32768) * RelativeScale3D.Z/128 + RelativeLocation.Z.

Heightmap source: the extractor's `landscape` PNG when present (SeaOfSand tiles name it Heightmap_0), else mip 0 of
the heightmap Texture2D read straight from the package .ubulk. The base presets (SoS_Arrakis_Dunes*) name it
Texture2D_N, so `landscape` exports nothing for them; `raw` the .ubulk instead (B,G,R,A byte order, uncompressed).

Layout rule (DA_DeepDesert_1_Layout_NN + DA_DeepDesert_1_TileLandcapeManager), cell (c, r), r = world Y index:
  reg  = RegionMap[r*24+c]                       0 DeepDesert, 1 ShieldWall (rows 21-23)
  tile = Override.Tiles[reg*576 + r*24 + c] or (DunesLow04 if reg else DunesMid02)_x{c%4}_y{r%4}
  z   += RegionInfo[reg].HeightZOffset           0 / 15000 cm: authored Shield Wall tier, kept
No rotation, flip or scale. Tile rows = world +Y, columns = world +X.

Weightmaps (landscape paint layers, used for the drape's sand tone): each component has 128x128 PF_B8G8R8A8
Weightmap textures in the same .ubulk; WeightmapLayerAllocations maps a layer to (texture index, RGBA channel).
"""
import glob, json, os
import numpy as np
from PIL import Image

Q = 127
N = 8 * Q + 1          # samples per tile edge (1 m)
CELLS = 24
TILE_M = 1016
LAYERS = ["Arrakis_SoS_Flat", "Arrakis_SoS_Wind", "Arrakis_SoS_Occluded", "ColorMask_R", "ColorMask_G", "ColorMask_B"]


class TileSource:
    """Locates and decodes tiles. tile_json: jsonlist root of the tile .umaps; hm: `landscape` PNG root;
    raw: `raw` root(s) holding the .ubulk next to the same package path; cache: decoded 1 m tiles (.npy)."""

    def __init__(self, tile_json, hm, raw, cache):
        self.hm, self.cache = hm, cache
        self.raw = [raw] if isinstance(raw, str) else list(raw)
        os.makedirs(cache, exist_ok=True)
        self.index = {}
        for p in glob.glob(tile_json + "/**/*.json", recursive=True):
            self.index.setdefault(os.path.basename(p)[:-5], []).append(p)
        self.tile_json = tile_json

    def _heightmap(self, name, d, jpath):
        comps = [e for e in d if e["Type"] == "LandscapeComponent"]
        tex = {c["Properties"]["HeightmapTexture"]["ObjectName"].split(".")[-1].rstrip("'") for c in comps}
        if len(tex) != 1:
            raise ValueError(f"{name}: {len(tex)} heightmap textures")
        tex = tex.pop()
        png = f"{self.hm}/{name}/{tex}.png"
        if os.path.exists(png):
            rgba = np.asarray(Image.open(png).convert("RGBA")).astype(np.int32)
            return (rgba[..., 0] << 8) | rgba[..., 1]
        raw = self._bgra(name, d, jpath, tex).astype(np.int32)
        return (raw[..., 2] << 8) | raw[..., 1]

    def _bgra(self, name, d, jpath, tex):
        """Mip 0 of an uncompressed B8G8R8A8 Texture2D of the tile package, read from its .ubulk."""
        t = next(e for e in d if e["Name"] == tex and e["Type"] == "Texture2D")
        if t["PixelFormat"] != "PF_B8G8R8A8":
            raise ValueError(f"{name}: {tex} pixel format {t['PixelFormat']}")
        mip = t["Mips"][0]
        w, h = mip["SizeX"], mip["SizeY"]
        rel = os.path.relpath(jpath, self.tile_json)[:-5] + ".ubulk"
        hits = [os.path.join(r, rel) for r in self.raw if os.path.exists(os.path.join(r, rel))]
        if not hits:
            raise FileNotFoundError(f"{name}: no {rel} under {self.raw} (run DuneExtractor raw on the tile)")
        raw = np.fromfile(hits[0], dtype=np.uint8, count=w * h * 4, offset=int(mip["BulkData"]["OffsetInFile"], 16))
        return raw.reshape(h, w, 4)

    def decode(self, name):
        hits = self.index.get(name, [])
        if len(hits) != 1:
            raise FileNotFoundError(f"{name}: {len(hits)} tile json hits under {self.tile_json}")
        d = json.load(open(hits[0]))
        raw = self._heightmap(name, d, hits[0])
        roots = [e for e in d if e["Type"] == "SceneComponent" and e.get("Properties")
                 and "LandscapeStreamingProxy" in str((e.get("Outer") or {}).get("ObjectName", ""))]
        root = roots[0]["Properties"] if roots else {}
        sz = root.get("RelativeScale3D", {}).get("Z", 100.0)
        z0 = root.get("RelativeLocation", {}).get("Z", 0.0)
        comps = [e["Properties"] for e in d if e["Type"] == "LandscapeComponent"]
        gx0 = min(int(p.get("SectionBaseX", 0)) for p in comps)
        gy0 = min(int(p.get("SectionBaseY", 0)) for p in comps)
        H = np.full((N, N), np.nan, np.float32)
        for p in comps:
            gx, gy = int(p.get("SectionBaseX", 0)) - gx0, int(p.get("SectionBaseY", 0)) - gy0
            b = p["HeightmapScaleBias"]
            u, v = int(round(b.get("Z", 0) * raw.shape[1])), int(round(b.get("W", 0) * raw.shape[0]))
            H[gy:gy + Q + 1, gx:gx + Q + 1] = (raw[v:v + Q + 1, u:u + Q + 1] - 32768) * sz / 128.0 + z0
        if np.isnan(H).any():
            raise ValueError(f"{name}: {int(np.isnan(H).sum())} samples not covered by its components")
        return H

    def decode_weights(self, name):
        """Paint layers (LAYERS order) as uint8 0..255, shape (len(LAYERS), N, N). Missing layer = 0."""
        hits = self.index.get(name, [])
        if len(hits) != 1:
            raise FileNotFoundError(f"{name}: {len(hits)} tile json hits under {self.tile_json}")
        d = json.load(open(hits[0]))
        comps = [e["Properties"] for e in d if e["Type"] == "LandscapeComponent"]
        gx0 = min(int(p.get("SectionBaseX", 0)) for p in comps)
        gy0 = min(int(p.get("SectionBaseY", 0)) for p in comps)
        W = np.zeros((len(LAYERS), N, N), np.uint8)
        texcache = {}
        for p in comps:
            gx, gy = int(p.get("SectionBaseX", 0)) - gx0, int(p.get("SectionBaseY", 0)) - gy0
            texs = [t["ObjectName"].split(".")[-1].rstrip("'") for t in p.get("WeightmapTextures", [])]
            b = p["WeightmapScaleBias"]
            for al in p.get("WeightmapLayerAllocations", []):
                layer = al["LayerInfo"]["ObjectName"].split("'")[1].replace("_LayerInfo", "")
                if layer not in LAYERS:
                    continue
                tex = texs[al["WeightmapTextureIndex"]]
                if tex not in texcache:
                    texcache[tex] = self._bgra(name, d, hits[0], tex)
                img = texcache[tex]
                h, w = img.shape[:2]
                if abs(b["X"] * w - 1) > 1e-6 or abs(b["Y"] * h - 1) > 1e-6:
                    raise ValueError(f"{name}: weightmap scale {b} is not one texel per vertex")
                u0, v0 = int(round(b["Z"] * w - 0.5)), int(round(b["W"] * h - 0.5))
                ch = (2, 1, 0, 3)[al["WeightmapTextureChannel"]]          # RGBA channel in BGRA storage
                W[LAYERS.index(layer), gy:gy + Q + 1, gx:gx + Q + 1] = img[v0:v0 + Q + 1, u0:u0 + Q + 1, ch]
        return W

    def weights(self, name):
        p = os.path.join(self.cache, name + "_w.npy")
        if os.path.exists(p):
            return np.load(p)
        W = self.decode_weights(name)
        np.save(p, W)
        return W

    def tile(self, name):
        """1 m decoded tile in cm, cached."""
        p = os.path.join(self.cache, name + ".npy")
        if os.path.exists(p):
            return np.load(p)
        H = self.decode(name)
        np.save(p, H)
        return H


def layout_rule(arrakis, lid):
    """(tile name, region) per (c, r) and the region Z offsets for DA layout `lid`."""
    mgr = json.load(open(f"{arrakis}/DeepDesert_1/DataTables/DA_DeepDesert_1_TileLandcapeManager.json"))
    sw, deep = mgr[0]["Properties"], mgr[1]["Properties"]
    region = sw["RegionMap"]
    zoff = [r["HeightZOffset"] for r in sw["RegionInfo"]]
    bases = {0: deep["BiomeInfo"][0]["Name"], 1: sw["BiomeInfo"][0]["Name"]}
    da = json.load(open(f"{arrakis}/DeepDesert_1/Layouts/DA_DeepDesert_1_Layout_{lid:02d}.json"))[0]["Properties"]
    tiles = da["m_TiledLandscapeBiomeDataOverride"]["Tiles"]
    if len(region) != CELLS * CELLS or len(tiles) != 2 * CELLS * CELLS:
        raise ValueError(f"layout {lid}: unexpected RegionMap/override sizes {len(region)}/{len(tiles)}")
    cells = {}
    for r in range(CELLS):
        for c in range(CELLS):
            reg = region[r * CELLS + c]
            t = tiles[reg * CELLS * CELLS + r * CELLS + c]["Preset"]["AssetPathName"]
            cells[(c, r)] = (t.split(".")[-1] if t else f"{bases[reg]}_x{c % 4}_y{r % 4}", reg)
    return cells, zoff


def assemble(src, arrakis, lid, px_m):
    """Ground for one layout at px_m metres per pixel (box mean of the 1 m samples), cm, row 0 = min world Y.
    Returns (grid, {"c,r": tile name})."""
    if TILE_M % px_m:
        raise ValueError(f"--ground px {px_m} m must divide {TILE_M}")
    S = TILE_M // px_m
    cells, zoff = layout_rule(arrakis, lid)
    out = np.empty((CELLS * S, CELLS * S), np.float32)
    for (c, r), (name, reg) in cells.items():
        t = src.tile(name)[:TILE_M, :TILE_M].reshape(S, px_m, S, px_m).mean(axis=(1, 3))
        out[r * S:(r + 1) * S, c * S:(c + 1) * S] = t + zoff[reg]
    return out, {f"{c},{r}": v[0] for (c, r), v in cells.items()}


def assemble_weights(src, arrakis, lid, px_m):
    """Paint layers for one layout at px_m metres per pixel, float32 0..1, shape (len(LAYERS), G, G)."""
    S = TILE_M // px_m
    cells, _ = layout_rule(arrakis, lid)
    out = np.empty((len(LAYERS), CELLS * S, CELLS * S), np.float32)
    for (c, r), (name, _reg) in cells.items():
        w = src.weights(name)[:, :TILE_M, :TILE_M].reshape(len(LAYERS), S, px_m, S, px_m).mean(axis=(2, 4)) / 255.0
        out[:, r * S:(r + 1) * S, c * S:(c + 1) * S] = w
    return out


def region_map(arrakis):
    """24x24 region per cell (0 DeepDesert, 1 ShieldWall), row = world Y index."""
    mgr = json.load(open(f"{arrakis}/DeepDesert_1/DataTables/DA_DeepDesert_1_TileLandcapeManager.json"))
    return np.array(mgr[0]["Properties"]["RegionMap"]).reshape(CELLS, CELLS)
