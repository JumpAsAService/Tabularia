#!/usr/bin/env bash
# The Stan half of lot P3 of docs/specs/bayes-prior-preview.md: for each case, one container of the Bayesian group's image
# (read-only root, no network, uid 10001, 2 GiB, 2 CPUs) compiles the program with the checks extension and runs it with
# fixed_param (bayes_prior_fixtures.py). By hand, ONE HEAVY JOB AT A TIME.
#
#   gateway-go/scripts/bayes-prior-fixtures.sh <out dir> [case ...]
#
# <out dir> must be writable by uid 10001 (chmod 777). Then copy the <case>.json files to frontend/tests/fixtures/bayes-prior/.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:?out dir}"; shift || true
image="${BAYES_IMAGE:-tabularia-bayes-b0:local}"
programs="$(mktemp -d)"; chmod 755 "$programs"
trap 'rm -rf "$programs"' EXIT
(cd "$here/.." && go run ./cmd/bayesfixtures prepare "$programs")
cases=("$@")
if [ ${#cases[@]} -eq 0 ]; then
  cases=(glm_linear glm_logistic glm_poisson glm_negbin hier_intercepts hier_slopes hier_nested ts_ar2 ts_random_walk ts_local_level
         ts_local_level_centred hier_slopes_centred free_truncated_hyper free_explicit_noncentred free_category truncated effects3)
fi
n=0
for c in "${cases[@]}"; do
  n=$((n + 1))
  seed=$((4100 + n))
  id=$(docker create --memory 2g --cpus 2 --read-only --network none --user 10001:10001 \
    --tmpfs /tmp:rw,exec,size=768m,uid=10001,mode=1777 -w /tmp \
    -v "$programs:/programs:ro" -v "$here/bayes_prior_fixtures.py:/run/fixtures.py:ro" -v "$out:/out" \
    "$image" python3 /run/fixtures.py --case "$c" --golden /programs --out /out --seed "$seed")
  docker start -a "$id" || echo "FAILED $c"
  docker rm "$id" >/dev/null
done
