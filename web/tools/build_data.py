"""Build the static data files for Map in a Box Web from the desktop data.

Run from the repository root:

    python web/tools/build_data.py

Writes into web/public/data/ and copies the country and region sounds into
web/public/sounds/. Both folders are generated and are not committed.

Lookup tables (country aliases, sound fallbacks, continent overrides, ocean
boxes) are read straight out of the desktop source with ``ast`` so the web
edition cannot drift from them.
"""

from __future__ import annotations

import ast
import csv
import gzip
import io
import json
import math
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from shapely.geometry import mapping, shape

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "web" / "public" / "data"
SOUNDS_OUT = ROOT / "web" / "public" / "sounds"

FEATURE_TILE_DEG = 5  # GeoFeatures are served in 5 x 5 degree tiles
SIMPLIFY_DEG = 0.005  # about 500 m; keeps coastlines recognisable
COORD_DP = 3


def literal(path: Path, name: str, cls: str | None = None):
    """Return the literal value assigned to *name* in *path* (optionally in a class)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = tree.body
    if cls:
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == cls:
                nodes = node.body
                break
    for node in nodes:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise KeyError(f"{name} not found in {path.name}")


def write_json(name: str, data) -> None:
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    print(f"  {name}: {len(text) / 1024:.0f} KB")


def round_coords(coords):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], COORD_DP), round(coords[1], COORD_DP)]
    return [round_coords(c) for c in coords]


def build_countries() -> None:
    with gzip.open(ROOT / "countries.geojson.gz", "rt", encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for feature in data["features"]:
        props = feature.get("properties", {})
        # Same name preference as the desktop _country_at_point.
        name = (props.get("NAME") or props.get("name") or props.get("ADMIN") or "").strip()
        if not name:
            continue
        geom = shape(feature["geometry"])
        simple = geom.simplify(SIMPLIFY_DEG, preserve_topology=True)
        if simple.is_empty:
            simple = geom
        gj = mapping(simple)
        polys = gj["coordinates"] if gj["type"] == "MultiPolygon" else [gj["coordinates"]]
        minx, miny, maxx, maxy = geom.bounds
        # Where a jump to the country lands. Natural Earth's LABEL_X/Y can be
        # offshore (New Zealand's is in the Tasman Sea), so use a point that is
        # guaranteed to be inside the simplified outline of the largest part.
        parts = list(simple.geoms) if simple.geom_type == "MultiPolygon" else [simple]
        inside = max(parts, key=lambda p: p.area).representative_point()
        out.append({
            "name": name,
            "iso2": props.get("ISO_A2_EH") or props.get("ISO_A2") or "",
            "continent": props.get("CONTINENT") or "",
            "bbox": [round(minx, 3), round(miny, 3), round(maxx, 3), round(maxy, 3)],
            "label": [round(inside.y, 4), round(inside.x, 4)],
            "polys": round_coords(polys),
        })
    write_json("countries.json", out)


def build_cities() -> None:
    names, admins, countries = [], [], []
    admin_index, country_index = {}, {}
    rows = {"n": [], "a": [], "c": [], "lat": [], "lon": [], "pop": []}
    with gzip.open(ROOT / "worldcities.csv.gz", "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                lat, lon = float(r["lat"]), float(r["lng"])
            except (TypeError, ValueError):
                continue
            admin = r.get("admin_name") or ""
            country = r.get("country") or ""
            if admin not in admin_index:
                admin_index[admin] = len(admins)
                admins.append(admin)
            if country not in country_index:
                country_index[country] = len(countries)
                countries.append(country)
            try:
                pop = int(float(r.get("population") or 0))
            except ValueError:
                pop = 0
            rows["n"].append(r["city"])
            rows["a"].append(admin_index[admin])
            rows["c"].append(country_index[country])
            rows["lat"].append(round(lat * 1e4))
            rows["lon"].append(round(lon * 1e4))
            rows["pop"].append(pop)
    write_json("cities.json", {"admins": admins, "countries": countries, **rows})


def build_features() -> None:
    tiles = defaultdict(list)
    count = 0
    for path in sorted((ROOT / "GeoFeatures").glob("*.csv.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    lat, lon = float(r["lat"]), float(r["lon"])
                except (TypeError, ValueError):
                    continue
                tx = math.floor((lon + 180) / FEATURE_TILE_DEG)
                ty = math.floor((lat + 90) / FEATURE_TILE_DEG)
                tiles[f"{tx}_{ty}"].append(
                    [r["name"], round(lat, 4), round(lon, 4), r.get("feature_code", ""),
                     r.get("country_code", "")])
                count += 1
    feat_dir = OUT / "features"
    if feat_dir.exists():
        shutil.rmtree(feat_dir)
    feat_dir.mkdir(parents=True)
    for key, items in tiles.items():
        (feat_dir / f"{key}.json").write_text(
            json.dumps(items, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    write_json("features/index.json", {"tile": FEATURE_TILE_DEG, "tiles": sorted(tiles)})
    print(f"  {count} features in {len(tiles)} tiles")


def build_tables() -> None:
    core = ROOT / "core.py"
    sound = ROOT / "sound_engine.py"
    oceans = literal(core, "KNOWN_OCEANS")
    write_json("tables.json", {
        "countryAliases": literal(sound, "COUNTRY_ALIASES"),
        "soundFallbacks": literal(sound, "_SOUND_FALLBACKS", cls="SoundEngine"),
        "continentOverrides": literal(core, "CONTINENT_OVERRIDES"),
        # Order matters: the desktop checks the boxes top to bottom.
        "oceans": [[name, boxes] for name, boxes in oceans.items()],
        "placeNameCloseKm": literal(core, "PLACE_NAME_CLOSE_KM"),
        "nearestPlaceFallbackKm": literal(core, "NEAREST_PLACE_FALLBACK_KM"),
    })
    for name in ("facts.json", "languages_data.json", "currency_data.json"):
        src = ROOT / name
        if src.exists():
            write_json(name.replace("_data", ""), json.loads(src.read_text(encoding="utf-8")))


def copy_sounds() -> None:
    src = ROOT / "sounds"
    listing = {}
    for sub in ("countries", "regions"):
        dest = SOUNDS_OUT / sub
        dest.mkdir(parents=True, exist_ok=True)
        stems = []
        for f in sorted((src / sub).iterdir()):
            if f.suffix.lower() in (".ogg", ".mp3"):
                target = dest / f.name
                if not target.exists() or target.stat().st_size != f.stat().st_size:
                    shutil.copy2(f, target)
                stems.append(f.name)
        listing[sub] = stems
    credits = src / "credits.txt"
    if credits.exists():
        shutil.copy2(credits, SOUNDS_OUT / "credits.txt")
    write_json("sounds.json", listing)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("Tables"); build_tables()
    print("Countries"); build_countries()
    print("Cities"); build_cities()
    print("GeoFeatures"); build_features()
    print("Sounds"); copy_sounds()
    return 0


if __name__ == "__main__":
    sys.exit(main())
