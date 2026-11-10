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


def scene_years(scenes):
    year_first = min(i.datetime.year for i in scenes if i.datetime)
    year_last = max(i.datetime.year for i in scenes if i.datetime)
    return str(year_first) if year_first == year_last else f"{year_first}-{year_last}"


def finish_pmtiles(db, out, bbox, minz, maxz, years, name, description):
    """Builds the lower zoom levels from the tiles at maxz, then writes everything to one .pmtiles file."""
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
    print(f"Writing {total} tiles to {out} ...", flush=True)
    order = sorted(
        (zxy_to_tileid(z, x, y), z, x, y) for z, x, y in db.execute("select z, x, y from tiles")
    )
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    part = out + ".part"
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
                "name": name,
                "description": description,
                "attribution": f"Contains modified Copernicus Sentinel data {years}",
                "type": "baselayer",
                "format": "jpg",
            },
        )
    os.replace(part, out)


# A rough outline of Saudi Arabia (lon, lat), used with --clip ksa so the sea and the neighbouring countries
# are not built. Tiles within CLIP_MARGIN degrees of it are kept, so the borders are not cut off.
KSA_OUTLINE = [
    (34.95, 29.35), (36.07, 29.19), (37.5, 30.0), (38.8, 31.5), (39.2, 32.15), (41.0, 31.4), (42.5, 30.5),
    (44.0, 29.2), (46.55, 29.1), (48.4, 28.55), (48.9, 27.7), (49.6, 27.1), (50.15, 26.65), (50.1, 25.9),
    (50.8, 24.7), (51.6, 24.3), (52.0, 23.0), (55.2, 22.7), (55.7, 22.0), (55.0, 20.0), (52.0, 19.0),
    (49.0, 18.2), (47.0, 17.4), (44.5, 17.4), (43.5, 17.2), (42.75, 16.4), (42.55, 16.9), (41.1, 19.1),
    (39.15, 21.5), (39.0, 22.8), (38.0, 24.1), (37.3, 25.0), (36.45, 26.25), (35.7, 27.35),
]
CLIPS = {"ksa": KSA_OUTLINE}
CLIP_MARGIN = 0.4


def near_polygon(lon, lat, polygon, margin):
    """True when the point is inside the polygon or within `margin` degrees of its edge."""
    inside = False
    close = False
    count = len(polygon)
    for i in range(count):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % count]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((lon - x1) * dx + (lat - y1) * dy) / length2))
        if math.hypot(lon - (x1 + t * dx), lat - (y1 + t * dy)) < margin:
            close = True
    return inside or close


def split_bbox(bbox, step):
    """Cuts the area into blocks of `step` degrees, north to south, so a huge area can be built one block at a time."""
    cells = []
    north = bbox[3]
    while north > bbox[1] + 1e-9:
        south = max(bbox[1], north - step)
        west = bbox[0]
        while west < bbox[2] - 1e-9:
            east = min(bbox[2], west + step)
            cells.append([west, south, east, north])
            west = east
        north = south
    return cells


def owned_tiles(cell, z, strict, clip):
    """Tiles of the area. With strict, only tiles whose centre is inside the block, so no tile is built twice."""
    tiles = []
    for x, y in tiles_for_bbox(cell, z):
        west, south, east, north = tile_lonlat_bounds(x, y, z)
        lon, lat = (west + east) / 2, (south + north) / 2
        if strict and not (cell[0] <= lon < cell[2] and cell[1] <= lat < cell[3]):
            continue
        if clip and not near_polygon(lon, lat, clip, CLIP_MARGIN):
            continue
        tiles.append((x, y))
    return tiles


def scenes_over_tiles(scenes, tiles, z):
    """Keeps only the scenes that cover at least one of the tiles (saves downloading scenes that are only over the sea)."""
    boxes = [tile_lonlat_bounds(x, y, z) for x, y in tiles]
    return [
        s for s in scenes
        if any(not (s.bbox[2] < w or s.bbox[0] > e or s.bbox[3] < so or s.bbox[1] > n) for w, so, e, n in boxes)
    ]


def download_with_retry(href, cache, tries=4):
    for attempt in range(1, tries + 1):
        try:
            return ensure_local(href, cache)
        except Exception as error:
            print(f"  download failed ({error}), try {attempt}/{tries}", flush=True)
            if attempt == tries:
                raise
            time.sleep(10 * attempt)


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
    parser.add_argument("--chunk-deg", type=float, default=0.0,
                        help="build in blocks of this many degrees (3 is good for a whole country). Needs little disk, "
                             "and a stopped build continues where it left off when you start it again")
    parser.add_argument("--clip", choices=sorted(CLIPS), help="only build tiles near this country's outline")
    parser.add_argument("--download-workers", type=int, default=4, help="scenes downloaded at the same time")
    args = parser.parse_args()

    bbox_text = args.bbox or os.environ.get("BBOX")
    if not bbox_text:
        sys.exit("Give --bbox or set BBOX (see area.sh).")
    bbox = [float(v) for v in bbox_text.split(",")]
    maxz, minz = args.maxzoom, args.minzoom
    clip = CLIPS[args.clip] if args.clip else (CLIPS[os.environ["CLIP"]] if os.environ.get("CLIP") in CLIPS else None)
    look = (args.black, args.white, args.gamma, args.saturation)
    chunked = args.chunk_deg > 0 and not args.preview
    cells = split_bbox(bbox, args.chunk_deg) if chunked else [bbox]

    os.makedirs(args.work, exist_ok=True)
    cache = os.path.join(args.work, "scenes")
    db_path = os.path.join(args.work, "tiles.sqlite")
    if os.path.exists(db_path) and not chunked:
        os.remove(db_path)
    elif os.path.exists(db_path):
        print(f"Continuing the earlier build in {db_path} (delete that file to start over)", flush=True)
    db = sqlite3.connect(db_path)
    db.execute("create table if not exists tiles (z integer, x integer, y integer, data blob, primary key (z, x, y))")
    db.execute("create table if not exists done_cells (name text primary key)")
    db.execute("create table if not exists years (y integer primary key)")

    skipped = []
    for index, cell in enumerate(cells, 1):
        name = ",".join(f"{v:.3f}" for v in cell)
        label = f"[{index}/{len(cells)}] " if chunked else ""
        if chunked and db.execute("select 1 from done_cells where name=?", (name,)).fetchone():
            print(f"{label}{name}: already done", flush=True)
            continue
        top_tiles = owned_tiles(cell, maxz, chunked, clip)
        print(f"{label}Area {name}: {len(top_tiles)} tiles at zoom {maxz}", flush=True)
        if len(top_tiles) > 60000 and not args.yes:
            sys.exit("That is a very large area. Use a smaller AREA, add --chunk-deg 3, or add --yes.")
        if not top_tiles:
            if chunked:
                db.execute("insert or replace into done_cells values (?)", (name,))
                db.commit()
            continue

        search_box = [cell[0] - 0.03, cell[1] - 0.03, cell[2] + 0.03, cell[3] + 0.03] if chunked else cell
        print("Looking for Sentinel-2 scenes ...", flush=True)
        try:
            scenes = find_scenes(search_box, args.days, args.max_cloud, args.scenes)
        except Exception as error:
            if not chunked:
                raise
            print(f"  warning: scene search failed ({error}); this block is skipped for now", flush=True)
            skipped.append(name)
            continue
        if not scenes:
            if not chunked:
                sys.exit("No cloud-free scenes found. Raise --max-cloud or --days.")
            print("  warning: no cloud-free scenes here; this block is skipped for now", flush=True)
            skipped.append(name)
            continue
        scenes = scenes_over_tiles(scenes, top_tiles, maxz)
        print(f"Using {len(scenes)} scenes", flush=True)
        if len(scenes) > 60 and not args.yes and not args.remote and not chunked:
            sys.exit("That needs many downloads (about 300 MB per scene). Use a smaller AREA, add --chunk-deg 3, or add --yes.")

        paths = {}
        try:
            if args.remote:
                for item in scenes:
                    scale, offset = reflectance_params(item)
                    paths[item.id] = {"bands": [item.assets[b].href for b in BANDS], "scale": scale, "offset": offset}
            else:
                wanted = [(item, b) for item in scenes for b in BANDS]
                with ThreadPoolExecutor(max_workers=max(1, args.download_workers)) as pool:
                    files = list(pool.map(lambda w: download_with_retry(w[0].assets[w[1]].href, cache), wanted))
                for item in scenes:
                    scale, offset = reflectance_params(item)
                    paths[item.id] = {
                        "bands": [f for (it, b), f in zip(wanted, files) if it is item],
                        "scale": scale,
                        "offset": offset,
                    }
        except Exception as error:
            if not chunked:
                raise
            print(f"  warning: could not download this block ({error}); skipped for now", flush=True)
            skipped.append(name)
            shutil.rmtree(cache, ignore_errors=True)
            continue

        # which scenes can cover which tile
        footprints = [(item.id, item.bbox) for item in scenes]

        def candidates(x, y, z):
            west, south, east, north = tile_lonlat_bounds(x, y, z)
            return [paths[i] for i, b in footprints if not (b[2] < west or b[0] > east or b[3] < south or b[1] > north)]

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
                    db.execute("insert or replace into tiles values (?,?,?,?)", (maxz, x, y, data))
                if done % 200 == 0 or done == len(jobs):
                    rate = done / max(time.time() - started, 1e-6)
                    print(f"  {done}/{len(jobs)} tiles ({rate:.1f}/s)", flush=True)
                    db.commit()
        for item in scenes:
            if item.datetime:
                db.execute("insert or ignore into years values (?)", (item.datetime.year,))
        if chunked:
            db.execute("insert or replace into done_cells values (?)", (name,))
        db.commit()
        if not args.remote and not args.keep_cache:
            shutil.rmtree(cache, ignore_errors=True)  # keeps the disk use small: one block of scenes at a time

    if skipped:
        db.close()
        print(f"{len(skipped)} blocks were skipped (see the warnings above). Run the same command again to retry them;", flush=True)
        print("the blocks already done are kept. Nothing was written to the map file yet.", flush=True)
        sys.exit(1)

    year_rows = [r[0] for r in db.execute("select y from years order by y")]
    if not year_rows:
        sys.exit("No tiles were built.")
    years = str(year_rows[0]) if year_rows[0] == year_rows[-1] else f"{year_rows[0]}-{year_rows[-1]}"
    db.execute("delete from tiles where z < ?", (maxz,))  # lower zooms are rebuilt below (also when finishing again)
    db.commit()
    finish_pmtiles(db, args.out, bbox, minz, maxz, years, "Sentinel-2 cloudless mosaic", "Median of the clearest Sentinel-2 scenes")
    db.close()
    os.remove(db_path)
    if not args.remote and not args.keep_cache:
        shutil.rmtree(cache, ignore_errors=True)
    print(f"Done: {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB). Credit: Contains modified Copernicus Sentinel data {years}")

if __name__ == "__main__":
    main()
