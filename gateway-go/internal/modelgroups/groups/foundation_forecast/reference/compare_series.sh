#!/bin/sh
# The Python series against R's (compare_series.py says what is compared): the cases and the Python side in the image
# of the group «foundation_forecast», the R side in the image of the group «timeseries», the files mounted (no image is
# rebuilt). From the repository's root:
#
#   sh gateway-go/internal/modelgroups/groups/foundation_forecast/reference/compare_series.sh [work folder]
#
# The images: TABULARIA_FF_IMAGE (tabularia-models-foundation_forecast:dev), TABULARIA_TS_IMAGE
# (tabularia-models-timeseries:dev). Each step is one small container, one after the other (1 CPU, 2.5 GB at most).
# Exit 0 when the two languages agree on every case, 1 otherwise.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
mg=$(cd "$here/../../.." && pwd)
work=${1:-$(mktemp -d)}
mkdir -p "$work"
work=$(cd "$work" && pwd)
ff=${TABULARIA_FF_IMAGE:-tabularia-models-foundation_forecast:dev}
ts=${TABULARIA_TS_IMAGE:-tabularia-models-timeseries:dev}
run() {
  docker run --rm --memory=2500m --cpus=1 --network=none --read-only --tmpfs /tmp --user "$(id -u):$(id -g)" \
    -e TABULARIA_MODELGROUPS=/mg -v "$mg:/mg:ro" -v "$work:/work" -w /work "$@"
}
script=/mg/groups/foundation_forecast/reference
run --entrypoint python "$ff" -I -B "$script/compare_series.py" cases /work
run --entrypoint python "$ff" -I -B "$script/compare_series.py" python /work
run --entrypoint Rscript "$ts" "$script/compare_series.R" /work
run --entrypoint python "$ff" -I -B "$script/compare_series.py" compare /work
