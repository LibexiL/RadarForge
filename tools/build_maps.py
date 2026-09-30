"""Build radarforge/assets/maps.npz from public-domain map sources.

Sources (downloaded separately into a working folder):
  * US Census 2010 500k county boundaries (gz_2010_us_050_00_500k.json)
  * Natural Earth 10m: admin-1 lines, admin-0 land boundaries, coastline,
    lakes, roads, populated places
  * GeoNames cities (via the geonamescache and reverse_geocoder packages)

Usage: python tools/build_maps.py <source_dir> <output.npz>
"""
import csv
import json
import os
import sys

import numpy as np
from shapely.geometry import LineString

BBOX = (-172.0, 12.0, -50.0, 72.0)   # lon0, lat0, lon1, lat1 (North America + territories)


def in_bbox(coords):
    a = np.asarray(coords)
    return (a[:, 0].max() >= BBOX[0] and a[:, 0].min() <= BBOX[2]
            and a[:, 1].max() >= BBOX[1] and a[:, 1].min() <= BBOX[3])


def lines_of(geom):
    t = geom["type"]
    c = geom["coordinates"]
    if t == "LineString":
        return [c]
    if t == "MultiLineString":
        return c
    if t == "Polygon":
        return c
    if t == "MultiPolygon":
        return [r for p in c for r in p]
    return []


def simplify(line, tol):
    if tol <= 0 or len(line) < 4:
        return np.asarray(line, np.float64)[:, :2]
    s = LineString([p[:2] for p in line]).simplify(tol, preserve_topology=False)
    return np.asarray(s.coords, np.float64)


def pack(lines):
    lines = [l for l in lines if len(l) >= 2]
    pts = np.concatenate(lines).astype(np.float32) if lines else np.zeros((0, 2), np.float32)
    starts = np.cumsum([0] + [len(l) for l in lines]).astype(np.int32)
    return pts, starts


def load(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return json.load(fh)


def main(src, out):
    arrays = {}

    # counties (+ fips for watch shading)
    d = load(os.path.join(src, "counties500k.json"))
    lines, fips = [], []
    for f in d["features"]:
        p = f["properties"]
        code = p["STATE"] + p["COUNTY"]
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for poly in polys:
            ring = simplify(poly[0], 0.0003)
            if len(ring) >= 3:
                lines.append(ring)
                fips.append(int(code))
    arrays["counties_pts"], arrays["counties_starts"] = pack(lines)
    arrays["counties_fips"] = np.asarray(fips, np.int32)
    print("counties", len(lines), len(arrays["counties_pts"]))

    def ne_lines(name, tol, keep=lambda p: True):
        d = load(os.path.join(src, name))
        res = []
        for f in d["features"]:
            if not keep(f["properties"]):
                continue
            for l in lines_of(f["geometry"]):
                if len(l) >= 2 and in_bbox(l):
                    res.append(simplify(l, tol))
        return res

    st = ne_lines("ne_10m_admin_1_states_provinces_lines.geojson", 0.001,
                  lambda p: (p.get("adm0_a3") or p.get("ADM0_A3")) in ("USA", "CAN", "MEX"))
    arrays["states_pts"], arrays["states_starts"] = pack(st)
    print("states", len(st))
    co = ne_lines("ne_10m_admin_0_boundary_lines_land.geojson", 0.001)
    co += ne_lines("ne_10m_coastline.geojson", 0.001)
    arrays["countries_pts"], arrays["countries_starts"] = pack(co)
    print("countries/coast", len(co))
    lk = ne_lines("ne_10m_lakes.geojson", 0.001, lambda p: (p.get("scalerank") or 0) <= 8)
    arrays["lakes_pts"], arrays["lakes_starts"] = pack(lk)
    print("lakes", len(lk))
    major = ne_lines("ne_10m_roads.geojson", 0.0005,
                     lambda p: p.get("continent") == "North America"
                     and p.get("type") in ("Major Highway", "Beltway", "Bypass"))
    minor = ne_lines("ne_10m_roads.geojson", 0.0005,
                     lambda p: p.get("continent") == "North America"
                     and p.get("type") in ("Secondary Highway", "Road"))
    arrays["roads_pts"], arrays["roads_starts"] = pack(major)
    arrays["roads2_pts"], arrays["roads2_starts"] = pack(minor)
    print("roads", len(major), len(minor))

    # cities
    import geonamescache
    gc = geonamescache.GeonamesCache()
    admin1 = {}
    try:
        for k, v in gc.get_us_states().items():
            admin1[("US", k)] = k
    except Exception:
        pass
    seen = {}
    by_name: dict = {}

    def add(name, lat, lon, pop):
        for (la, lo, idx) in by_name.get(name, []):
            if abs(la - lat) < 0.15 and abs(lo - lon) < 0.15:
                if pop > seen[idx][3]:
                    seen[idx] = (name, lat, lon, pop)
                return
        idx = len(seen)
        seen[idx] = (name, lat, lon, pop)
        by_name.setdefault(name, []).append((lat, lon, idx))

    for c in gc.get_cities().values():
        if c["countrycode"] in ("US", "CA", "MX", "PR"):
            add(c["name"], c["latitude"], c["longitude"], int(c["population"]))
    rg = os.path.join(src, "rg_cities1000.csv")
    if os.path.exists(rg):
        with open(rg) as fh:
            for r in csv.DictReader(fh):
                if r["cc"] in ("US", "CA", "MX", "PR"):
                    add(r["name"], float(r["lat"]), float(r["lon"]), 1000)
    ne = load(os.path.join(src, "ne_10m_populated_places_simple.geojson"))
    for f in ne["features"]:
        p = f["properties"]
        if (p.get("adm0_a3") or p.get("ADM0_A3")) in ("USA", "CAN", "MEX"):
            add(p["name"], p["latitude"], p["longitude"], int(p.get("pop_max") or 0))
    cities = sorted(seen.values(), key=lambda c: -c[3])
    arrays["city_lat"] = np.asarray([c[1] for c in cities], np.float32)
    arrays["city_lon"] = np.asarray([c[2] for c in cities], np.float32)
    arrays["city_pop"] = np.asarray([c[3] for c in cities], np.int32)
    arrays["city_name"] = np.asarray([c[0] for c in cities])
    print("cities", len(cities))
    np.savez_compressed(out, **arrays)
    print("wrote", out, os.path.getsize(out) / 1e6, "MB")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
