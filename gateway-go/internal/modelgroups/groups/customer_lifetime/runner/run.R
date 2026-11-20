# The runner of the group «customer_lifetime» (docs/specs/customer-lifetime-models.md, «The
# runner»): `Rscript runner/run.R` in /tabularia/code, or `Rscript runner/run.R --selftest`. It
# sources the common library, then the group's files (runner/R/*.R), and hands the routes to
# tab_main: which function fits and which predicts each node of the group. Nothing here reads a
# column's name as code.

here <- dirname(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)))))
for (file in sort(list.files(file.path(here, "common", "R"), pattern = "[.]R$", full.names = TRUE))) source(file)
for (file in sort(list.files(file.path(here, "runner", "R"), pattern = "[.]R$", full.names = TRUE))) source(file)

tab_main("customer_lifetime", list(
  clv = list(fit = function(task) fit_clv(task), predict = function(task, card) predict_clv(task, card)),
  bg_bb = list(fit = function(task) fit_bg_bb(task), predict = function(task, card) predict_bg_bb(task, card))
))
