# The self-test of the node `bg_bb` (bg_bb.R, lot B3): the node through its routes, fit_bg_bb() and predict_bg_bb(), on
# customers drawn here from BG/BB's own process (the helpers of clv_shared_tests.R). The formulas and the shared module
# are tested there; the fitted reference cases of expected.json (make_reference_bgbb.R) run after.

.bb_frame <- function(n = 500, seed = 12, par = c(1.2, 0.75, 0.66, 2.78), opportunities = 6) {
  set.seed(seed)
  d <- .clv_sim_bgbb(n, par, opportunities)
  d$m <- .clv_sim_spend(d$x)
  d$h <- stats::rbinom(nrow(d), 5, 0.1 * (d$x + 1) / 7)
  d$id <- paste0("d", seq_len(nrow(d)))
  d
}

TAB_COMMON_TESTS[["customer_lifetime: the node bg_bb fits through its route, writes the checks of a clean fit and predicts the fit's own rows"]] <- function(code) {
  d <- .bb_frame()
  params <- .clv_params(list(spend = "m", horizon = 5, holdout_transactions = "h", holdout_length = 5, output = "rows"), "bg_bb")
  task <- .clv_task(params, d, "bg_bb")
  tab_seed(task)
  res <- fit_bg_bb(task)
  .tab_expect(!length(tab_check_declared(res$rows, "bg_bb", "value")), "the rows are what the catalogue declares")
  df <- tab_results_frame(res$rows)
  verdict <- function(id) df$text[df$key == paste0("assumption|", id, "||verdict")]
  .tab_expect(identical(verdict("convergence"), "ok") && !is.null(verdict("frequency_fit")) && length(verdict("spend_tail")) == 1L &&
              all(c("assumption|sample_size||value", "assumption|holdout||value", "assumption|horizon_length||value") %in% df$key) &&
              !any(df$section == "assumption" & df$level == "boundary") && !any(df$section == "candidate"), "the checks of a clean BG/BB fit")
  .tab_expect(df$value[df$key == "assumption|horizon_length||value"] == 5 / 6, "the horizon over the most opportunities")
  # the verdict of a test is its p-value against the node's alpha, at both sides of it
  p <- df$value[df$key == "assumption|frequency_fit||p_value"]
  hi <- fit_bg_bb(.clv_task(utils::modifyList(params, list(alpha = min(p * 1.01, 0.999))), d, "bg_bb"))
  lo <- fit_bg_bb(.clv_task(utils::modifyList(params, list(alpha = p * 0.99)), d, "bg_bb"))
  said <- function(r) tab_results_frame(r$rows)$text[tab_results_frame(r$rows)$key == "assumption|frequency_fit||verdict"]
  .tab_expect(identical(said(hi), "failed") && identical(said(lo), "ok"), "frequency_fit against alpha: ", said(hi), " / ", said(lo))
  # Predict on the training rows is the fit's, bit for bit; on rows that break the summary's rules, NULL
  card <- jsonlite::fromJSON(tab_card_text(res$card), simplifyVector = FALSE)
  got <- predict_bg_bb(list(params = list(), limits = tab_catalog()$limits, .frame = d), card)$output
  .tab_expect(identical(got$prediction, res$output$pred_transactions) && identical(got$prediction_p_alive, res$output$pred_p_alive) &&
              identical(got$prediction_clv, res$output$pred_clv), "Predict gives the training rows the fit's numbers")
  d$tx[[7L]] <- d$T[[7L]] + 2
  bad <- suppressWarnings(predict_bg_bb(list(params = list(), limits = tab_catalog()$limits, .frame = d), card)$output)
  .tab_expect(is.na(bad$prediction[[7L]]) && identical(bad$prediction[-7L], got$prediction[-7L]), "a row with a recency after its opportunities gets NULL")
  # the route with no spend: the variant `purchases`, no CLV
  p2 <- .clv_params(list(horizon = 5, output = "results"), "bg_bb")
  r2 <- fit_bg_bb(.clv_task(p2, d[, c("id", "x", "tx", "T")][-7L, ], "bg_bb", variant = "purchases"))
  .tab_expect(!length(tab_check_declared(r2$rows, "bg_bb", "purchases")) && is.null(r2$card$customer_lifetime$spend), "the purchases variant")
}

TAB_COMMON_TESTS[["customer_lifetime: BG/BB's estimate at an edge is a measure with its reading, and the convergence check says what it cost"]] <- function(code) {
  # customers who never drop out (δ so large that θ is 1e-9): γ goes towards 0 and the Hessian is flat
  set.seed(5)
  d <- .clv_sim_bgbb(300, c(1, 1, 1, 1e9), 20)
  d$id <- paste0("d", seq_len(nrow(d)))
  res <- fit_bg_bb(.clv_task(.clv_params(list(horizon = 5, output = "results"), "bg_bb"), d, "bg_bb", variant = "purchases"))
  df <- tab_results_frame(res$rows)
  at <- df[df$section == "assumption" & df$level == "boundary", ]
  .tab_expect(nrow(at) > 0L && !any(at$stat %in% c("verdict", "advice", "h0", "count")), "the edge has no verdict")
  got <- at$level_2[at$stat == "value"]
  .tab_expect(all(is.finite(at$value[at$stat == "value"])) && all(at$text[at$stat == "advice_id"] == "boundary_no_dropout") &&
              all(got %in% c("gamma", "delta")), "an edge is its value and its reading: ", paste(got, collapse = ", "))
}
