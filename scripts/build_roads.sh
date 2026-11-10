#!/usr/bin/env bash
# Cuts roads, places and labels (OpenStreetMap, via Protomaps) for the chosen area into /data/roads.pmtiles
# Usage: AREA=start /app/build_roads.sh
set -euo pipefail
. "$(dirname "$0")/area.sh"

OUT="${OUT:-/data/roads.pmtiles}"
mkdir -p "$(dirname "$OUT")"

url=""
for i in $(seq 1 14); do
  d="$(date -u -d "-${i} day" +%Y%m%d)"
  candidate="https://build.protomaps.com/${d}.pmtiles"
  if curl -sfI -m 30 "$candidate" >/dev/null; then url="$candidate"; break; fi
done
if [ -z "$url" ]; then echo "Could not find a recent Protomaps build." >&2; exit 1; fi

echo "Source: $url"
echo "Area:   $BBOX"
rm -f "${OUT}.part"
pmtiles extract "$url" "${OUT}.part" --bbox="$BBOX" --download-threads=4
mv "${OUT}.part" "$OUT"
ls -lh "$OUT"
pmtiles show "$OUT" | head -25
echo "Done. Credit on the map: © OpenStreetMap contributors"
