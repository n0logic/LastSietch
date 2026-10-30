#!/usr/bin/env python3
"""FX inventory per overland location: every Niagara system, point light, decal, FX mesh, animated material and
FX blueprint the overmap levels (Overmap, Overmap_VFX and the 3 sublevels) place, with its decoded user parameters
and enough of the asset (emitters, renderers, materials, BP components, ticks and timelines) to re-create it as a
lightweight shader effect. Writes overmap-locations-fx.json. Reads the locations from overmap-places.json.
"""
import argparse
import json
import math
import os
import struct
import sys

import numpy as np

import overmap_bake as ob
import overmap_miniatures as om

LEVELS = ["Overmap.json", "Overmap_VFX.json"] + ob.SUBLEVELS
ASSIGN_RADIUS_CM = 6000.0
# Meshes that are terrain, miniatures or the travel disc, not FX (the erythrite crystals stay: their material glows).
NOT_FX_MESH = ("SM_OvermapOutput", "SM_OvermapInput", "SM_Overland_BackgroundPlane", "SM_OvermapPoi_")
FX_ACTOR_PREFIX = ("BP_Overland", "BP_VFX_", "BP_MeshFollowingSpline", "BP_Pointers", "BP_Decal_", "BP_SandDust")
ANIM_HINT_WORDS = ("speed", "pan", "time", "flicker", "glow", "rate", "wind", "scroll", "pulse", "rotation")

# Hand notes: how to fake each effect cheaply in the browser (the parameters come from the files).
RECIPES = {
    "NS_VFX_Overland_Fire": "flame cards (2-3 crossed quads, scrolling noise, additive orange) + rising ember "
                            "points; pair with the flickering point light",
    "NS_VFX_Overmap_Fire": "as NS_VFX_Overland_Fire",
    "NS_VFX_Overland_Electric": "short-lived lightning arcs: jittered line strips between random points on the box, "
                                "cyan-white, 3-6 Hz flashes",
    "NS_VFX_Overland_Radiation": "soft green haze sprites rising inside the box; slow pulse",
    "NS_VFX_Overmap_PoisonSpores": "two sprite layers: low fog billboards (FogColor) + drifting spores (SporeColor) "
                                   "in a sphere",
    "NS_VFX_Overland_WreckOfHephaestus_Wind": "wind streak ribbons circling the wreck at Ring Radius (streak "
                                             "texture scrolled along a ring of quads)",
    "NS_VFX_Overland_SpiceBlooms": "spice bloom puffs: orange dust sprites bursting at random points, fade 2-4 s",
    "NS_FlyingLights": "small glowing points travelling the BP_Pointers spline (see travelLines in "
                       "overmap-miniatures.json)",
    "NS_VFX_Env_Overmap_DustSpice": "ambient spice dust: large soft sprites drifting over the table",
    "SM_LightCone": "additive cone mesh with a vertical gradient (MI_Overland_Arena_Lightcone), searchlights",
    "M_Darkness": "dark plane with soft edges (unlit, multiply) over the miniature",
    "MI_Overland_Erythrite_Crystals": "emissive pink crystals, emissive = colour * power * (0.5 + 0.5 sin(t * "
                                      "glowing speed))",
    "SM_OverlandMap_TinySandbike": "tiny sandbike mesh moved along its spline (BP_MeshFollowingSpline)",
    "BP_OverlandMap_FlickeringLight": "point light, intensity jittered every tick (ReceiveTick)",
    "BP_Overland_PrimroseField": "instanced primrose flowers (MI_Overland_Foliage_Primrose); vertex sway",
    "BP_Overland_ToxicPool": "green emissive pool decal + spores",
    "BP_Decal_PointOfIntrest_04": "ground decal under the location (projected quad)",
    "PointLight": "one warm point light (or an additive glow sprite on weak tiers)",
    "BP_MeshFollowingSpline": "move the attached mesh along splineCm at constant speed, loop",
    "BP_VFX_Overland_SpiceBlooms": "as NS_VFX_Overland_SpiceBlooms",
}


def decode_params(store):
    """Niagara parameter store -> {name: value}. Offsets are bytes into ParameterData."""
    data = bytes((store or {}).get("ParameterData") or [])
    out = {}
    for p in (store or {}).get("SortedParameterOffsets") or []:
        t = ((p.get("TypeDef") or {}).get("ClassStructOrEnum") or {}).get("ObjectName", "")
        t = t.split("'")[1] if "'" in t else t
        o = p.get("Offset", 0)
        n = p["Name"].replace("User.", "")
        size = {"NiagaraFloat": 4, "NiagaraInt32": 4, "NiagaraBool": 4, "Vector2f": 8, "Vector3f": 12,
                "NiagaraPosition": 12, "Vector4f": 16, "LinearColor": 16, "Quat4f": 16}.get(t)
        if size is None or o + size > len(data):
            out[n] = {"type": t, "value": None}
            continue
        if t in ("NiagaraInt32",):
            v = struct.unpack_from("<i", data, o)[0]
        elif t == "NiagaraBool":
            v = struct.unpack_from("<i", data, o)[0] != 0
        else:
            v = list(struct.unpack_from(f"<{size // 4}f", data, o))
            v = round(v[0], 4) if len(v) == 1 else [round(x, 4) for x in v]
        out[n] = v
    for p in (store or {}).get("UserParameterRedirects") or []:
        out.setdefault(p["Key"]["Name"], None)
    return out


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def summarize_niagara(path):
    exports = load_json(path)
    sysx = next((e for e in exports if e.get("Type") == "NiagaraSystem"), None)
    if sysx is None:
        return {"error": "no NiagaraSystem export"}
    p = sysx.get("Properties") or {}
    emitters = [{"name": h.get("Name"), "enabled": h.get("bIsEnabled", True)} for h in p.get("EmitterHandles", [])]
    renderers = []
    for e in exports:
        t = e.get("Type", "")
        if not t.endswith("RendererProperties"):
            continue
        rp = e.get("Properties") or {}
        r = {"emitter": ob.asset_name(e.get("Outer")).split(":")[-1], "type": t.replace("Niagara", "").replace(
            "RendererProperties", "").lower()}
        if rp.get("Material"):
            r["material"] = ob.asset_name(rp["Material"])
        if rp.get("Meshes"):
            r["meshes"] = [ob.asset_name(m.get("Mesh")) for m in rp["Meshes"]]
        for k in ("Alignment", "FacingMode", "SubImageSize", "bSubImageBlend"):
            if k in rp:
                r[k] = rp[k]
        renderers.append(r)
    fb = p.get("FixedBounds")
    return {"emitters": emitters, "renderers": renderers, "userDefaults": decode_params(p.get("ExposedParameters")),
            "fixedBoundsCm": fb if p.get("bFixedBounds") else None,
            "dataInterfaces": sorted({e["Type"] for e in exports if e.get("Type", "").startswith("NiagaraDataInterface")})}


def param_list(vals, key):
    out = {}
    for x in vals or []:
        n = (x.get("ParameterInfo") or {}).get("Name")
        v = x.get("ParameterValue")
        if isinstance(v, dict) and "R" in v:
            v = [round(v["R"], 4), round(v["G"], 4), round(v["B"], 4), round(v.get("A", 1.0), 4)]
        elif isinstance(v, dict):
            v = ob.asset_name(v) or v
        out[n] = v
    return out


def summarize_material(path):
    exports = load_json(path)
    m = next((e for e in exports if e.get("Type") in ("Material", "MaterialInstanceConstant")), None)
    if m is None:
        return {"error": "no material export"}
    p = m.get("Properties") or {}
    out = {"type": m["Type"], "parent": ob.asset_name(p.get("Parent")) or None,
           "blendMode": p.get("BlendMode"), "scalars": param_list(p.get("ScalarParameterValues"), "s"),
           "vectors": param_list(p.get("VectorParameterValues"), "v"),
           "textures": param_list(p.get("TextureParameterValues"), "t")}
    ced = m.get("CachedExpressionData") or {}
    if ced and m["Type"] == "Material":
        names = [x["Name"] for x in (ced.get("RuntimeEntries") or {}).get("ParameterInfoSet", [])]
        vals = ced.get("ScalarValues") or []
        out["scalars"].update({n: round(v, 4) for n, v in zip(names, vals)})
        vnames = [x["Name"] for x in (ced.get("RuntimeEntries[1]") or {}).get("ParameterInfoSet", [])]
        out["vectors"].update({n: [v["R"], v["G"], v["B"], v["A"]] for n, v in zip(vnames, ced.get("VectorValues") or [])})
        out["textures"].update({"streaming": [t["TextureName"] for t in p.get("TextureStreamingData", [])]})
    names = list(out["scalars"]) + list(out["vectors"])
    out["animatedHint"] = sorted(n for n in names if any(w in n.lower() for w in ANIM_HINT_WORDS))
    return out


def summarize_blueprint(path):
    exports = load_json(path)
    comps, funcs, timelines, defaults = [], [], [], {}
    for e in exports:
        t, name, p = e.get("Type", ""), e.get("Name", ""), e.get("Properties") or {}
        if name.endswith("_GEN_VARIABLE"):
            keep = {k: (ob.asset_name(v) if isinstance(v, dict) and "ObjectName" in v else v) for k, v in p.items()
                    if k in ("StaticMesh", "Asset", "Intensity", "LightColor", "AttenuationRadius", "DecalMaterial",
                             "DecalSize", "RelativeLocation", "RelativeScale3D", "OverrideMaterials", "RotationRate",
                             "Mobility")}
            comps.append({"type": t, "name": name.replace("_GEN_VARIABLE", ""), **keep})
        elif t == "Function":
            funcs.append(name)
        elif "Timeline" in t:
            timelines.append({"type": t, "name": name, "length": p.get("TimelineLength"),
                              "loop": p.get("bLoop")})
        elif name.startswith("Default__"):
            defaults = {k: v for k, v in p.items() if k not in ("UberGraphFrame",)}
    return {"components": comps, "functions": sorted(funcs), "ticks": "ReceiveTick" in funcs,
            "timelines": timelines, "defaults": defaults}


def instance_props(p):
    out = {}
    for k, v in p.items():
        if k in ("ActorLabel", "UCSSerializationIndex", "bNetAddressable", "CreationMethod"):
            continue
        if isinstance(v, (int, float, bool, str)):
            out[k] = v
        elif isinstance(v, dict) and set(v) >= {"X", "Y", "Z"}:
            out[k] = [round(v["X"], 2), round(v["Y"], 2), round(v["Z"], 2)]
        elif isinstance(v, dict) and set(v) >= {"R", "G", "B"}:
            out[k] = [v["R"], v["G"], v["B"], v.get("A", 1)]
    return out


def spline_world(comp, index, n=24):
    scale, rot, loc = ob.component_transform(comp, index)
    pts = (comp.get("Properties") or {}).get("SplineCurves", {}).get("Position", {}).get("Points", [])
    v = lambda d: np.array([d["X"], d["Y"], d["Z"]])
    out = []
    for a, b in zip(pts, pts[1:]):
        seg = om.hermite(v(a["OutVal"]), v(a["LeaveTangent"]), v(b["OutVal"]), v(b["ArriveTangent"]), n)
        out.extend(((seg * scale) @ rot.T + loc).round().astype(int).tolist())
    return out


def collect(levels_root, extract):
    found = []
    for rel in LEVELS:
        path = os.path.join(extract, ob.OVERMAP_DIR, rel)
        if not os.path.isfile(path):
            path = os.path.join(levels_root, ob.OVERMAP_DIR.split("json/", 1)[1], rel)
        if not os.path.isfile(path):
            ob.fail(f"level {rel} not found under --extract or --json")
        level = ob.load_level(path)
        index = ob.by_object_name(level)
        for e in level:
            t, p = e.get("Type", ""), e.get("Properties") or {}
            actor = ob.asset_name(e.get("Outer")).split(".")[-1]
            rec = None
            if t == "NiagaraComponent" and p.get("Asset"):
                rec = {"kind": "niagara", "asset": ob.asset_name(p["Asset"]),
                       "params": decode_params(p.get("OverrideParameters"))}
            elif t == "PointLightComponent":
                rec = {"kind": "light", "asset": "PointLight", "params": instance_props(p)}
            elif t == "DecalComponent":
                mat = ob.asset_name(p.get("DecalMaterial"))
                if "MaterialInstanceDynamic" in mat:
                    mat = "MaterialInstanceDynamic (built at runtime by the owning blueprint)"
                rec = {"kind": "decal", "asset": mat or "(blueprint default DecalMaterial)",
                       "params": instance_props(p)}
            elif t in ("StaticMeshComponent", "InstancedStaticMeshComponent"):
                mesh = ob.asset_name(p.get("StaticMesh"))
                mats = [ob.asset_name(m) for m in (p.get("OverrideMaterials") or []) if m]
                if not mesh or mesh.startswith(NOT_FX_MESH):
                    continue
                if mesh == "Plane" and mats in (["M_POI_TravelVolume_Circular"], []):
                    continue
                if mesh.startswith("SM_OvermapPoi_Gate"):
                    continue
                rec = {"kind": "instanced-mesh" if t.startswith("Instanced") else "mesh", "asset": mesh,
                       "materials": mats, "params": {}}
                if t.startswith("Instanced"):
                    rec["params"]["instances"] = len(p.get("PerInstanceSMData") or e.get("PerInstanceSMData") or [])
            elif t.startswith(FX_ACTOR_PREFIX) and t.endswith("_C"):
                root = index.get(ob.asset_name(p.get("RootComponent")))
                if root is None:
                    continue
                rec = {"kind": "blueprint", "asset": t[:-2], "params": instance_props(p), "comp": root}
                splines = [c for c in level if c.get("Type") == "SplineComponent"
                           and ob.asset_name(c.get("Outer")).split(".")[-1] == e["Name"]]
                if splines:
                    rec["splineCm"] = spline_world(splines[0], index)
            if rec is None:
                continue
            comp = rec.pop("comp", e)
            try:
                scale, rot, loc = ob.component_transform(comp, index)
            except SystemExit:
                continue
            rec.update({"level": rel[:-5].split("/")[-1], "actor": actor,
                        "positionCm": [round(float(v)) for v in loc],
                        "scale": [round(float(v), 3) for v in scale]})
            found.append(rec)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extract", required=True, help="DuneExtractor dump with Overmap.json and the sublevels")
    ap.add_argument("--json", required=True, help="DuneExtractor json root with Overmap_VFX.json and the FX assets "
                                                 "(e.g. ~/dd-re/overmap/json)")
    ap.add_argument("--client", required=True)
    ap.add_argument("--out", default=ob.DEFAULT_OUT)
    args = ap.parse_args()

    locations = load_json(os.path.join(args.out, "overmap-places.json"))["locations"]
    found = collect(os.path.expanduser(args.json), args.extract)
    assets = {}
    for root in (os.path.expanduser(args.json), os.path.join(args.extract, "json")):
        for d, _, files in os.walk(root):
            for f in files:
                if f.endswith(".json"):
                    assets.setdefault(f[:-5], os.path.join(d, f))

    per = {l["id"]: [] for l in locations}
    glob = []
    for r in found:
        best = min(locations, key=lambda l: math.hypot(l["position"]["x"] - r["positionCm"][0],
                                                        l["position"]["y"] - r["positionCm"][1]))
        dist = math.hypot(best["position"]["x"] - r["positionCm"][0], best["position"]["y"] - r["positionCm"][1])
        r["recipe"] = RECIPES.get(r["asset"]) or next((RECIPES[m] for m in r.get("materials", []) if m in RECIPES),
                                                       None)
        if r["asset"] == "BP_Pointers":
            r["note"] = "travel pointer; its spline endpoints are in overmap-miniatures.json travelLines"
            glob.append(r)
        elif dist <= ASSIGN_RADIUS_CM:
            r["offsetCm"] = [round(r["positionCm"][0] - best["position"]["x"]),
                             round(r["positionCm"][1] - best["position"]["y"]),
                             round(r["positionCm"][2] - best["position"]["z"])]
            per[best["id"]].append(r)
        else:
            glob.append(r)
        if r in glob:
            r["nearest"] = {"id": best["id"], "distCm": round(dist)}

    referenced = ({r["asset"] for r in found} | {m for r in found for m in r.get("materials", [])}) - {None}
    systems, materials, blueprints, missing = {}, {}, {}, []
    for name in sorted(referenced):
        path = assets.get(name)
        if path is None:
            if not name.startswith(("SM_", "Plane", "PointLight", "MaterialInstanceDynamic", "(")):
                missing.append(name)
            continue
        if name.startswith("NS_"):
            systems[name] = summarize_niagara(path)
            for rr in systems[name]["renderers"]:
                if rr.get("material") and rr["material"] in assets and rr["material"] not in materials:
                    materials[rr["material"]] = summarize_material(assets[rr["material"]])
        elif name.startswith(("M_", "MI_")):
            materials[name] = summarize_material(path)
        elif name.startswith("BP_"):
            blueprints[name] = summarize_blueprint(path)
    for name in ("NS_FlyingLights", "NS_VFX_Overland_SpiceBlooms", "NS_VFX_Env_Overmap_DustSpice"):
        if name in assets and name not in systems:
            systems[name] = summarize_niagara(assets[name])
    for name in ("MI_Overland_Erythrite_Crystals", "MI_VFX_Overland_StreakWind", "MI_FlyingPoint",
                 "MI_OverlandMap_WayPointer", "MI_Overland_Arena_Lightcone", "M_Darkness"):
        if name in assets and name not in materials:
            materials[name] = summarize_material(assets[name])
    # Erythrite glow belongs to Blushing Cavern even though its mesh is a miniature part
    if "MI_Overland_Erythrite_Crystals" in materials:
        per["blushing-cavern"].append({"kind": "animated-material", "asset": "MI_Overland_Erythrite_Crystals",
                                       "params": materials["MI_Overland_Erythrite_Crystals"]["scalars"],
                                       "recipe": RECIPES["MI_Overland_Erythrite_Crystals"],
                                       "on": "SM_Env_Mineral_Neut_Ore_Erythrite_D_02 (miniature part)"})

    doc = {
        "client": args.client, "units": "overmap cm; offsetCm is relative to the location anchor in "
                                        "overmap-places.json",
        "levels": LEVELS, "assignRadiusCm": ASSIGN_RADIUS_CM,
        "kinds": {"niagara": "NiagaraComponent (params = decoded per-instance user parameter overrides)",
                  "light": "PointLightComponent", "decal": "DecalComponent", "mesh": "FX static mesh",
                  "instanced-mesh": "InstancedStaticMeshComponent", "blueprint": "FX blueprint actor",
                  "animated-material": "material with time-driven parameters on a miniature part"},
        "skeletalAnimations": "none: no SkeletalMeshComponent in any overmap level",
        "rotatingComponents": "none: no RotatingMovementComponent in any overmap level or FX blueprint",
        "locations": {lid: per[lid] for lid in per},
        "global": glob, "systems": systems, "materials": materials, "blueprints": blueprints,
        "missingAssetJson": missing,
    }
    ob.write_json(os.path.join(args.out, "overmap-locations-fx.json"), doc)
    ob.register(args.out, ["overmap-locations-fx.json"], "fx",
                {"files": ["overmap-locations-fx.json"], "script": "overmap_fx.py",
                 "placed": len(found), "systems": len(systems), "materials": len(materials),
                 "blueprints": len(blueprints)})
    for lid, fx in per.items():
        print(f"  {lid:26s} {[(f['kind'], f['asset']) for f in fx]}")
    print(f"  global: {[(f['kind'], f['asset'], f['nearest']['id']) for f in glob]}")
    print(f"  missing asset json: {missing}")
    print(f"overmap-locations-fx.json {os.path.getsize(os.path.join(args.out, 'overmap-locations-fx.json'))} bytes")


if __name__ == "__main__":
    sys.exit(main())
