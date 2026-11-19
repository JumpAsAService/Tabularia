# The reference values of the group «timeseries», computed OUTSIDE Tabularia
# (docs/specs/statistical-models.md, «Reference values»): by hand, with R and the pinned
# packages of ../recipe.json plus the witnesses of witness.json, never by the runner.
#
#   Rscript gateway-go/internal/modelgroups/groups/timeseries/reference/make_reference.R
#
# It reads the small tables of data/ (tens to a few hundred rows, synthetic, fixed seeds),
# calls the packages directly (lm, glm, coxph, survfit, forecast::Arima…) and writes
# expected.json in the format common/R/selftest.R reads: {"format_version": 1,
# "generated": {"by", "R", "packages"}, "cases": [{"id", "model", "data", "types",
# "params", "expect": [{"key", "value" | "text", "rel", "abs"}], "refuses"}]}.
#
# Each model's lane (M1…M7) adds its cases here; until then there are none, and the
# self-test runs the common library's tests only.

stop("no reference case yet for the group timeseries: the lanes of its models write them")
