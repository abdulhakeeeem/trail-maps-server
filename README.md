# Trail Log map server

Serves your own free maps to the app:

| File | What it is | Free source | Credit to show on the map |
|---|---|---|---|
| `roads.pmtiles` | Roads, places, labels | OpenStreetMap (through Protomaps) | © OpenStreetMap contributors |
| `imagery.pmtiles` | Satellite map, 10 m per pixel, cloud-free | Sentinel-2 (Copernicus) | Contains modified Copernicus Sentinel data (year) |

The server turns each file into ordinary map tiles:

```
https://YOUR-DOMAIN/roads/{z}/{x}/{y}.mvt
https://YOUR-DOMAIN/imagery/{z}/{x}/{y}.jpg
```

Everything in this folder was tested on a Windows PC with Docker (compose file, builder image, roads and
imagery builds for a small area, serving real tiles). A full-size build has not been timed.

## What is in the folder

- `docker-compose.yml`: three services. `tiles` serves the map files. `style` serves the map style (`/style.json`). `builder` makes the files, and sits idle until you start a job.
- `Dockerfile.builder`: the builder image (Python, the pmtiles tool, Sentinel-2 libraries).
- `style/`: the map style file and the small web server that serves it. The file is generated from the app's own style code; a unit test keeps the two identical.
- `scripts/build_roads.sh`: cuts your area out of the newest Protomaps OpenStreetMap build (minutes).
- `scripts/build_imagery.sh` / `build_imagery.py`: builds the satellite map (hours).
- `scripts/area.sh`: the areas. `AREA=start` is about 200 km x 190 km around your first desert trips.
  `central` is Riyadh to the Eastern Province. `ksa` is the whole country (roads only for now).
  Or set your own: `BBOX=minLon,minLat,maxLon,maxLat`.

## Before you start

1. **Can phones reach the server from the internet?** Turn Wi-Fi off on a phone and open
   your server's web address. If it only opens inside a company network or VPN, the app will not work in the desert.
2. **HTTPS.** The app needs an `https` address. Coolify creates the certificate for you once a public domain points at the server.
3. **Disk space.** Check with `df -h` in the Coolify terminal. Plan for:
   - `start` area: about 1 GB of finished maps plus 8 to 10 GB of temporary downloads while building.
   - Bigger areas need proportionally more. Do not start `ksa` imagery without asking first.
4. **The server is shared.** Tell its owner. The builder is limited to 2 CPUs and 4 GB of memory in `docker-compose.yml`.

## Step 1: get this folder into a Git repository

Coolify builds from a Git repository. Use this repository directly, or copy this folder into your own.
If the repository is private, Coolify needs a deploy key or a GitHub App to read it. A public repository needs nothing.

## Step 2: deploy in Coolify

1. Open your project, then **+ New Resource**, then **Private Repository (with Deploy Key)** or
   **Public Repository** if you made it public.
2. Pick the repository and branch. Set **Build Pack** to **Docker Compose**. Leave the Compose location as `/docker-compose.yml`.
3. In the resource's settings, give the **`tiles`** service a domain such as `https://maps.example.com`
   (port 8080). Create a DNS record for that name pointing at the server, or ask whoever manages the domain.
   Also give the **`style`** service the same host with the path `/style.json` (port 80). The app's offline
   downloader needs the map style at a web address, and this tiny service serves it.
   (Coolify removes the path before passing the request on, so the service answers at both `/` and `/style.json`.)
4. Press **Deploy**. All three services should show as running.

## Step 3: build the maps

In Coolify open the resource, choose the **builder** service, then **Terminal**.

Roads (minutes):

```
AREA=start /app/build_roads.sh
```

Satellite map (hours). Run it in the background so closing the browser does not stop it:

```
AREA=start nohup /app/build_imagery.sh > /work/imagery.log 2>&1 &
tail -f /work/imagery.log
```

The log shows the scenes found, the downloads, and `N/M tiles`. When it prints `Done:` the file is ready.
The `tiles` service finds new files by itself. There is no need to restart it.

## Step 4: check it

Open these in a browser (use your own domain). Tile numbers must be real ones for your area:

- A roads tile, for example `https://maps.example.com/roads/14/10362/7006.mvt`, should download a small file (not "Archive not found").
- A satellite tile, for example `https://maps.example.com/imagery/14/10362/7006.jpg`, should show a picture once the imagery build has finished.
- The information pages `/roads.json` and `/imagery.json` only work if you add `--public-url=https://maps.example.com` to the `tiles` command in `docker-compose.yml`. The app does not need them.

Then put the address in `secrets.properties` as described under "Pointing the app at your server" below.

## Colours

Sentinel-2 desert sand looks washed out with the standard settings, so the builder uses its own stretch.
To try other looks on one tile without a full build:

```
python /app/build_imagery.py --bbox 47.685,25.185,47.695,25.195 --remote --preview /work/try.jpg \
  --black 0.03 --white 0.42 --gamma 1.6 --saturation 0.65
```

The defaults are those values. Lower `--white` or raise `--gamma` for a brighter picture. Lower `--saturation` for calmer colours.

## Keeping it fresh

Run the imagery build again once or twice a year. It uses the clearest scenes of the last 365 days.
If you want to keep the downloaded scenes between runs, add `--keep-cache`.

## Pointing the app at your server

The app already supports this (tested against a local copy of this server). Add one line to `secrets.properties`
next to the project's `build.gradle.kts` and rebuild:

```
MAP_SERVER_URL=https://maps.example.com
```

- With the line set, the app uses your server for the satellite map, roads, place names, trip pictures and offline downloads.
  The credits ("i" button on the map and the Settings screen) name OpenStreetMap and Copernicus Sentinel.
- Without it, the app keeps using MapTiler, as before.
- The road check ("is this point on a main road") reads the Protomaps road names, so it works with the new roads.
- For a quick test build without editing the file: `gradlew assembleDebug -PMAP_SERVER_URL=https://...`

## Limits to know

- 10 m per pixel: highways, dunes and big tracks show. Narrow tracks and small buildings do not.
- No labels or roads are drawn on the satellite map yet. The app draws them from `roads.pmtiles` and needs a font source for text.
- The tiles are public. Anyone who knows the address can load them. Add rate limiting in Coolify if that worries you.
