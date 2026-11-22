# The runner of the group «timeseries» (docs/specs/statistical-models.md, «The runner protocol»):
# `Rscript runner/run.R` in /tabularia/code, or `Rscript runner/run.R --selftest`. It sources
# the common library, then one file per model (runner/R/*.R), and hands the routes to
# tab_main: which function fits and which predicts each model of the group (the decomposition and the risk simulation make no model: no predict). Nothing here
# reads a column's name as code.

here <- dirname(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)))))
for (file in sort(list.files(file.path(here, "common", "R"), pattern = "[.]R$", full.names = TRUE))) source(file)
for (file in sort(list.files(file.path(here, "runner", "R"), pattern = "[.]R$", full.names = TRUE))) source(file)

tab_main("timeseries", list(
  arima = list(
    fit = function(task) if (tab_auto_orders(task)) fit_arima_auto(task) else fit_arima(task),
    predict = function(task, card) predict_arima(task, card)
  ),
  # no model: no card, so no `predict` route
  decomposition = list(fit = function(task) fit_decomposition(task)),
  # a GARCH makes a card and is predicted (lot G6); the risk simulation makes no model: no `predict` route
  garch = list(fit = function(task) fit_garch(task), predict = function(task, card) predict_garch(task, card)),
  risk_simulation = list(fit = function(task) fit_risk_simulation(task))
))
