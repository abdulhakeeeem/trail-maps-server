#!/usr/bin/env bash
# Picks the area to build. Usage: AREA=start ./build_roads.sh   (or set BBOX yourself)
# BBOX is "minLon,minLat,maxLon,maxLat".
if [ -z "${BBOX:-}" ]; then
  case "${AREA:-start}" in
    start)   BBOX="46.5,24.3,48.5,26.0" ;;   # around the first desert trips (about 200 km x 190 km)
    central) BBOX="44.0,21.0,52.0,28.0" ;;   # Riyadh, Eastern Province and the desert between
    ksa)     BBOX="34.4,16.2,56.0,32.3"; export CLIP=ksa ;;   # all of Saudi Arabia (imagery: build in blocks, see the README)
    *) echo "Unknown AREA '${AREA}'. Use start, central, ksa, or set BBOX." >&2; exit 1 ;;
  esac
fi
export BBOX
