# The runner of the group «regression» (docs/specs/statistical-models.md, «The runner protocol»):
# `Rscript runner/run.R` in /tabularia/code, or `Rscript runner/run.R --selftest`. It sources
# the common library, then one file per model (runner/R/*.R), and hands the routes to
# tab_main: which function fits and which predicts each model of the group. Nothing here
# reads a column's name as code.

here <- dirname(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)))))
for (file in sort(list.files(file.path(here, "common", "R"), pattern = "[.]R$", full.names = TRUE))) source(file)
for (file in sort(list.files(file.path(here, "runner", "R"), pattern = "[.]R$", full.names = TRUE))) source(file)

tab_main("regression", list(
  linear = list(
    fit = function(task) if (tab_penalized(task)) fit_penalized(task) else fit_linear(task),
    predict = function(task, card) if (identical(card$variant, "penalized")) predict_penalized(task, card) else predict_linear(task, card)
  ),
  logistic = list(
    fit = function(task) if (tab_penalized(task)) fit_penalized(task) else fit_logistic(task),
    predict = function(task, card) if (identical(card$variant, "penalized")) predict_penalized(task, card) else predict_logistic(task, card)
  )
))
