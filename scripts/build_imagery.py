#!/usr/bin/env python3
"""Builds a cloud-free satellite map from free Sentinel-2 images and packs it into one PMTiles file.

How it works
1. Finds recent, almost cloud-free Sentinel-2 scenes over the area (free, from AWS Earth Search).
2. For every map tile at the highest zoom, takes the median colour of the best scenes, which removes clouds.
3. Builds the lower zoom levels by shrinking the tiles below them.
4. Writes everything into a single .pmtiles file (JPEG tiles).

Credit required on the map: "Contains modified Copernicus Sentinel data <year>".
"""
import argparse
import datetime as dt
import io
import math
import os
import shutil
import sqlite3
import sys
import time
import urllib.request
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import write as pmtiles_write
from pystac_client import Client
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1"
BANDS = ("red", "green", "blue")  # Sentinel-2 bands 4, 3, 2 at 10 m
FILL = (205, 180, 140)  # sand colour for the few pixels no scene covers
TILE = 256


def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat = max(min(lat, 85.0511), -85.0511)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile_lonlat_bounds(x, y, z):
    n = 2 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def tiles_for_bbox(bbox, z):
    x0, y1 = lonlat_to_tile(bbox[0], bbox[1], z)  # south-west corner
    x1, y0 = lonlat_to_tile(bbox[2], bbox[3], z)  # north-east corner
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def find_scenes(bbox, days, max_cloud, per_grid):
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    catalog = Client.open(EARTH_SEARCH)
    search = catalog.search(
        collections=["sentinel-2-l2a"],
        bbox=bbox,
        datetime=f"{start.isoformat()}/{end.isoformat()}",
        query={"eo:cloud_cover": {"lt": max_cloud}},
        max_items=1000,
    )
    items = [i for i in search.items() if all(k in i.assets for k in BANDS)]
    groups = {}
    for item in items:
        key = item.properties.get("grid:code") or item.properties.get("s2:mgrs_tile") or item.id[:12]
        groups.setdefault(key, []).append(item)
    chosen = []
    for key, group in groups.items():
        group.sort(key=lambda i: i.properties.get("eo:cloud_cover", 100))
        chosen.extend(group[:per_grid])
    return chosen


def reflectance_params(item):
    """Scale and offset that turn the stored numbers into reflectance (0 to 1)."""
    band = (item.assets["red"].extra_fields.get("raster:bands") or [{}])[0]
    scale, offset = band.get("scale"), band.get("offset")
    if scale is None or offset is None:
        baseline = str(item.properties.get("s2:processing_baseline", "04.00"))
        scale, offset = 0.0001, (-0.1 if baseline >= "04.00" else 0.0)
    return float(scale), float(offset)


def ensure_local(href, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    name = "_".join(href.split("/")[-6:])
    path = os.path.join(cache_dir, name)
    if os.path.exists(path):
        return path
    part = path + ".part"
    print(f"  downloading {name}", flush=True)
    request = urllib.request.Request(href, headers={"User-Agent": "trail-maps-builder/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, open(part, "wb") as out:
        shutil.copyfileobj(response, out, 1024 * 1024)
    os.replace(part, path)
    return path


def read_scene_tile(scene, x, y, z):
    planes = []
    for path in scene["bands"]:
        try:
            with Reader(path) as src:
                image = src.tile(x, y, z, tilesize=TILE)
        except TileOutsideBounds:
            return None
        except Exception as error:  # a broken or unreachable scene must not stop the whole build
            print(f"  warning: {os.path.basename(path)} tile {z}/{x}/{y}: {error}", flush=True)
            return None
        planes.append(np.ma.filled(image.array[0].astype("float32"), np.nan))
    data = np.stack(planes)
    data[data <= 0] = np.nan  # 0 means "no data"
    invalid = np.isnan(data).any(axis=0)
    data = data * scene["scale"] + scene["offset"]
    data[:, invalid] = np.nan
    return data


def render_tile(job):
    x, y, z, scenes, look = job
    layers = [a for a in (read_scene_tile(s, x, y, z) for s in scenes) if a is not None]
    if not layers:
        return x, y, None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        median = np.nanmedian(np.stack(layers), axis=0)
    missing = np.isnan(median[0])
    if missing.all():
        return x, y, None
    black, white, gamma, saturation = look
    stretched = np.clip((median - black) / (white - black), 0.0, 1.0) ** (1.0 / gamma) * 255.0
    luma = 0.299 * stretched[0] + 0.587 * stretched[1] + 0.114 * stretched[2]
    stretched = luma + saturation * (stretched - luma)  # below 1 makes the colours calmer
    for band in range(3):
        stretched[band][missing] = FILL[band]
    rgb = np.clip(stretched, 0, 255).astype("uint8").transpose(1, 2, 0)
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, "JPEG", quality=85, optimize=True)
    return x, y, buffer.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bbox", help="minLon,minLat,maxLon,maxLat (default: the BBOX environment variable)")
    parser.add_argument("--out", default="/data/imagery.pmtiles")
    parser.add_argument("--work", default="/work")
    parser.add_argument("--minzoom", type=int, default=6)
    parser.add_argument("--maxzoom", type=int, default=14)
    parser.add_argument("--scenes", type=int, default=5, help="best scenes to use per Sentinel-2 grid square")
    parser.add_argument("--black", type=float, default=0.03, help="reflectance shown as black")
    parser.add_argument("--white", type=float, default=0.42, help="reflectance shown as white")
    parser.add_argument("--gamma", type=float, default=1.6, help="above 1 makes the picture brighter")
    parser.add_argument("--saturation", type=float, default=0.65, help="1 keeps the colours, lower is calmer")
    parser.add_argument("--max-cloud", type=float, default=5.0, help="largest cloud cover percent allowed")
    parser.add_argument("--days", type=int, default=365, help="how far back to look for scenes")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--remote", action="store_true", help="read scenes over the internet instead of downloading them (small areas only)")
    parser.add_argument("--preview", help="write one tile as a PNG/JPG to this path and stop (to try the colour settings)")
    parser.add_argument("--keep-cache", action="store_true", help="keep the downloaded scenes")
    parser.add_argument("--yes", action="store_true", help="do not stop for big jobs")
    args = parser.parse_args()

    bbox_text = args.bbox or os.environ.get("BBOX")
    if not bbox_text:
        sys.exit("Give --bbox or set BBOX (see area.sh).")
    bbox = [float(v) for v in bbox_text.split(",")]
    maxz, minz = args.maxzoom, args.minzoom

    top_tiles = tiles_for_bbox(bbox, maxz)
    print(f"Area {bbox}: {len(top_tiles)} tiles at zoom {maxz}", flush=True)
    if len(top_tiles) > 60000 and not args.yes:
        sys.exit("That is a very large area. Use a smaller AREA or add --yes.")

    print("Looking for Sentinel-2 scenes ...", flush=True)
    scenes = find_scenes(bbox, args.days, args.max_cloud, args.scenes)
    if not scenes:
        sys.exit("No cloud-free scenes found. Raise --max-cloud or --days.")
    print(f"Using {len(scenes)} scenes", flush=True)
    if len(scenes) > 60 and not args.yes and not args.remote:
        sys.exit("That needs many downloads (about 300 MB per scene). Use a smaller AREA or add --yes.")

    cache = os.path.join(args.work, "scenes")
    paths = {}
    for item in scenes:
        hrefs = [item.assets[b].href for b in BANDS]
        scale, offset = reflectance_params(item)
        paths[item.id] = {
            "bands": hrefs if args.remote else [ensure_local(h, cache) for h in hrefs],
            "scale": scale,
            "offset": offset,
        }

    os.makedirs(args.work, exist_ok=True)
    db_path = os.path.join(args.work, "tiles.sqlite")
    if os.path.exists(db_path):
        os.remove(db_path)
    db = sqlite3.connect(db_path)
    db.execute("create table tiles (z integer, x integer, y integer, data blob, primary key (z, x, y))")

    # which scenes can cover which tile
    footprints = [(item.id, item.bbox) for item in scenes]

    def candidates(x, y, z):
        west, south, east, north = tile_lonlat_bounds(x, y, z)
        return [paths[i] for i, b in footprints if not (b[2] < west or b[0] > east or b[3] < south or b[1] > north)]

    look = (args.black, args.white, args.gamma, args.saturation)
    jobs = [(x, y, maxz, candidates(x, y, maxz), look) for x, y in top_tiles]
    jobs = [j for j in jobs if j[3]]
    if args.preview:
        middle = jobs[len(jobs) // 2]
        _, _, data = render_tile(middle)
        with open(args.preview, "wb") as out:
            out.write(data)
        sys.exit(f"Preview tile {maxz}/{middle[0]}/{middle[1]} written to {args.preview}")
    print(f"Rendering {len(jobs)} tiles at zoom {maxz} ...", flush=True)
    started = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for x, y, data in pool.map(render_tile, jobs):
            done += 1
            if data is not None:
                db.execute("insert into tiles values (?,?,?,?)", (maxz, x, y, data))
            if done % 200 == 0 or done == len(jobs):
                rate = done / max(time.time() - started, 1e-6)
                print(f"  {done}/{len(jobs)} tiles ({rate:.1f}/s)", flush=True)
                db.commit()
    db.commit()

    for z in range(maxz - 1, minz - 1, -1):
        parents = {(x // 2, y // 2) for (x, y) in db.execute("select x, y from tiles where z=?", (z + 1,))}
        for px, py in sorted(parents):
            canvas = Image.new("RGB", (TILE * 2, TILE * 2), FILL)
            for dx in (0, 1):
                for dy in (0, 1):
                    row = db.execute(
                        "select data from tiles where z=? and x=? and y=?", (z + 1, px * 2 + dx, py * 2 + dy)
                    ).fetchone()
                    if row:
                        canvas.paste(Image.open(io.BytesIO(row[0])).convert("RGB"), (dx * TILE, dy * TILE))
            buffer = io.BytesIO()
            canvas.resize((TILE, TILE), Image.LANCZOS).save(buffer, "JPEG", quality=85, optimize=True)
            db.execute("insert into tiles values (?,?,?,?)", (z, px, py, buffer.getvalue()))
        db.commit()
        print(f"  zoom {z}: {len(parents)} tiles", flush=True)

    total = db.execute("select count(*) from tiles").fetchone()[0]
    print(f"Writing {total} tiles to {args.out} ...", flush=True)
    order = sorted(
        (zxy_to_tileid(z, x, y), z, x, y) for z, x, y in db.execute("select z, x, y from tiles")
    )
    year_first = min(i.datetime.year for i in scenes if i.datetime)
    year_last = max(i.datetime.year for i in scenes if i.datetime)
    years = str(year_first) if year_first == year_last else f"{year_first}-{year_last}"
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    part = args.out + ".part"
    with pmtiles_write(part) as writer:
        for tileid, z, x, y in order:
            writer.write_tile(tileid, db.execute("select data from tiles where z=? and x=? and y=?", (z, x, y)).fetchone()[0])
        center_lon = (bbox[0] + bbox[2]) / 2
        center_lat = (bbox[1] + bbox[3]) / 2
        writer.finalize(
            {
                "tile_type": TileType.JPEG,
                "tile_compression": Compression.NONE,
                "min_zoom": minz,
                "max_zoom": maxz,
                "min_lon_e7": int(bbox[0] * 1e7),
                "min_lat_e7": int(bbox[1] * 1e7),
                "max_lon_e7": int(bbox[2] * 1e7),
                "max_lat_e7": int(bbox[3] * 1e7),
                "center_zoom": min(maxz, 10),
                "center_lon_e7": int(center_lon * 1e7),
                "center_lat_e7": int(center_lat * 1e7),
            },
            {
                "name": "Sentinel-2 cloudless mosaic",
                "description": "Median of the clearest Sentinel-2 scenes",
                "attribution": f"Contains modified Copernicus Sentinel data {years}",
                "type": "baselayer",
                "format": "jpg",
            },
        )
    os.replace(part, args.out)
    db.close()
    os.remove(db_path)
    if not args.remote and not args.keep_cache:
        shutil.rmtree(cache, ignore_errors=True)
    print(f"Done: {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB). Credit: Contains modified Copernicus Sentinel data {years}")


if __name__ == "__main__":
    main()
