# ARIMA with manual orders, from AR to SARIMAX (docs/specs/statistical-models.md, lane M6).
# A stub until that lane builds it:
# the fit and the prediction refuse with «not built yet», so a flow that holds the node fails
# with a sentence and the group's self-test still runs.
#
# A fit reads its input with tab_input(task), builds its design with tab_design (never a
# formula from a column's name), starts its rows with tab_rows_start(task) and returns
# list(rows, output, card, native); see common/R/main.R.

fit_arima <- function(task) tab_not_built("the ARIMA model")

predict_arima <- function(task, card) tab_not_built("the forecast of an ARIMA model")
