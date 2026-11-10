#!/usr/bin/env bash
# Builds the satellite map for the chosen area into /data/imagery.pmtiles
# Usage: AREA=start /app/build_imagery.sh            (extra options go to build_imagery.py, e.g. --scenes 4)
set -euo pipefail
. "$(dirname "$0")/area.sh"
exec python "$(dirname "$0")/build_imagery.py" --bbox "$BBOX" "$@"
