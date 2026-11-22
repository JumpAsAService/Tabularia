# Volatility (GARCH) (docs/specs/garch-risk.md, «Part 1 — the node `garch`»): fit_garch(task), predict_garch(task, card).
#
# Lot G0 gives the node its contract (the catalogue, the fixture, the image with `rugarch`); lot G1 writes the series and the
# returns, the scale and its exact back-transform, `ugarchfit`, the coefficients, the measures, the checks (the weighted
# portmanteau tests, the ARCH-LM, Nyblom with its bridge-law p-value, the sign bias), the 1-step VaR, the holdout and its
# backtest, the `volatility` output and the card here and in their own files; lot G6 writes Predict. Until then the node refuses,
# with a sentence, as every node not built yet. `rugarch` is loaded only here, on the routes that need it, so that ARIMA and the
# decomposition do not pay its load time.

fit_garch <- function(task) tab_not_built("Volatility (GARCH)")

predict_garch <- function(task, card) tab_not_built("Predict with Volatility (GARCH)")
