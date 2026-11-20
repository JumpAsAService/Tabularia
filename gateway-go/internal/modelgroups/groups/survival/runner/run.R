# The runner of the group «survival» (docs/specs/statistical-models.md, «The runner protocol»):
# `Rscript runner/run.R` in /tabularia/code, or `Rscript runner/run.R --selftest`. It sources
# the common library, then one file per model (runner/R/*.R), and hands the routes to
# tab_main: which function fits and which predicts each model of the group. Nothing here
# reads a column's name as code.

here <- dirname(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)))))
for (file in sort(list.files(file.path(here, "common", "R"), pattern = "[.]R$", full.names = TRUE))) source(file)
for (file in sort(list.files(file.path(here, "runner", "R"), pattern = "[.]R$", full.names = TRUE))) source(file)

tab_main("survival", list(
  kaplan_meier = list(fit = function(task) fit_kaplan_meier(task), predict = function(task, card) predict_kaplan_meier(task, card)),
  cox = list(fit = function(task) fit_cox(task), predict = function(task, card) predict_cox(task, card)),
  parametric_survival = list(fit = function(task) fit_parametric_survival(task),
                             predict = function(task, card) predict_parametric_survival(task, card))
))
