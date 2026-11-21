# The runner of the group «experiments» (docs/specs/ab-testing.md, «The runner»): `Rscript runner/run.R`
# in /tabularia/code, or `Rscript runner/run.R --selftest`. It sources the common library, then the
# group's files (runner/R/*.R), and hands the routes to tab_main: which function fits each node of the
# group. The nodes make no model: no card, so no `predict` route. No random number is drawn anywhere.
# Nothing here reads a column's name as code.

here <- dirname(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)))))
for (file in sort(list.files(file.path(here, "common", "R"), pattern = "[.]R$", full.names = TRUE))) source(file)
for (file in sort(list.files(file.path(here, "runner", "R"), pattern = "[.]R$", full.names = TRUE))) source(file)

tab_main("experiments", list(
  ab_test = list(fit = function(task) fit_ab_test(task)),
  ab_plan = list(fit = function(task) fit_ab_plan(task))
))
