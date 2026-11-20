# Writes the reference cases of the group customer_lifetime: the CSVs of reference/data/ and
# reference/expected.json (docs/specs/customer-lifetime-models.md, «Reference values»). Run by hand,
# inside the group's image, with the reference folder mounted writable:
#
#   docker run --rm -v "$PWD/gateway-go/internal/modelgroups/groups/customer_lifetime/reference:/ref" \
#     tabularia-models-customer_lifetime:dev Rscript /ref/make_reference.R
#
# Every table here is built, not drawn: no random number, no copy of a public data set. Lot B1 writes
# the refusal cases (a wrong summary stops with a sentence that says what to change; the sentences
# are written out here by hand, so that they are checked word for word). Lots B2 and B3 add their
# fitted cases (clv_*.csv, bgbb_*.csv) to the same script and the same expected.json.

args <- commandArgs(FALSE)
here <- dirname(normalizePath(sub("^--file=", "", grep("^--file=", args, value = TRUE))))
dir.create(file.path(here, "data"), showWarnings = FALSE)

# ── the refusal tables ───────────────────────────────────────────────────────────
# 30 customers, valid: x = 0…4 in turn (24 repeaters), t_x at 0.4…0.7 of the age, a spend for the repeaters, a holdout.
# Every other column is the same column with one defect, so that a case chooses its defect by the columns it names.
refusals <- function(n) {
  i <- seq_len(n)
  x <- i %% 5
  age <- 20 + i
  tx <- ifelse(x > 0, round(age * (0.3 + 0.1 * x), 1), 0)
  d <- data.frame(id = paste0("c", i), x = x, tx = tx, T = age, m = ifelse(x > 0, 10 + i, NA_real_), h = i %% 3, stringsAsFactors = FALSE)
  with_ <- function(col, row, value) {
    v <- d[[col]]
    v[[row]] <- value
    v
  }
  d$id_dup <- with_("id", 7, "c3")
  d$x_frac <- with_("x", 4, 2.5)
  d$tx_neg <- with_("tx", 11, -3)
  d$T_zero <- with_("T", 6, 0)
  d$tx_after <- with_("tx", 17, d$T[[17]] + 2)
  d$tx_nopurchase <- with_("tx", 15, 12)      # x = 0 but a recency
  d$x_same_moment <- with_("x", 10, 2)        # tx = 0 but two repeat purchases
  d$m_zero <- with_("m", 3, 0)
  d$h_neg <- with_("h", 20, -1)
  d$x_none <- 0
  d$tx_none <- 0
  d$x_few <- ifelse(i %in% c(2, 7, 13), 1, 0)
  d$tx_few <- ifelse(i %in% c(2, 7, 13), 5, 0)
  d$m_few <- ifelse(i %in% c(2, 7, 13), 20, NA_real_)
  # BG/BB: 6 opportunities
  d$n <- 6
  d$n_zero <- with_("n", 12, 0)
  d$x_bb <- pmin(x, 3)
  d$tx_bb <- ifelse(d$x_bb > 0, pmin(d$x_bb + 2, 6), 0)
  d$tx_bb_low <- with_("tx_bb", 8, 1)         # the customer 8 has x_bb = 3 and a last transaction at the opportunity 1
  d$tx_bb_late <- with_("tx_bb", 9, 8)        # an opportunity after the last
  d$h_bb <- i %% 3
  d$h_bb_over <- with_("h_bb", 14, 9)
  d
}
write_table <- function(d, name) utils::write.csv(d, file.path(here, "data", name), row.names = FALSE, na = "")
write_table(refusals(30), "clv_refusals.csv")
write_table(refusals(30)[1:12, ], "clv_refusals_small.csv")

# ── the cases ────────────────────────────────────────────────────────────────────
numbers <- c("x", "tx", "T", "m", "h", "x_frac", "tx_neg", "T_zero", "tx_after", "tx_nopurchase", "x_same_moment", "m_zero", "h_neg", "x_none", "tx_none",
             "x_few", "tx_few", "m_few", "n", "n_zero", "x_bb", "tx_bb", "tx_bb_low", "tx_bb_late", "h_bb", "h_bb_over")
types <- c(stats::setNames(rep("number", length(numbers)), numbers), id = "text", id_dup = "text")
base <- list(customer = "id", frequency = "x", recency = "tx", age = "T", time_unit = "week", seed = 1)
bb <- list(customer = "id", frequency = "x_bb", recency = "tx_bb", opportunities = "n", period_unit = "month", seed = 1)

refusal <- function(id, params, code, message, data = "data/clv_refusals.csv", model = "clv", variant = "purchases") {
  roles <- c("customer", "frequency", "recency", "age", "opportunities", "spend", "holdout_transactions")
  used <- unique(unlist(params[intersect(roles, names(params))]))
  list(id = id, model = model, variant = variant, data = data, types = as.list(types[used]), params = params, refuses = list(code = code, message = message))
}
over <- function(p, ...) utils::modifyList(p, list(...))

cases <- list(
  refusal("refusal duplicate customer", over(base, customer = "id_dup"), "duplicate_customer",
          "the customer c3 appears on 2 rows: one row per customer; group the transactions by customer first"),
  refusal("refusal frequency not whole", over(base, frequency = "x_frac"), "frequency_not_whole", "frequency must count purchases: the customer c4 has 2.5"),
  refusal("refusal negative recency", over(base, recency = "tx_neg"), "negative_value",
          "recency, age and frequency are 0 or more: the customer c11 has a recency of −3"),
  refusal("refusal age zero", over(base, age = "T_zero"), "age_zero", "the customer c6 has an age of 0: age is the time since the first purchase, more than 0"),
  refusal("refusal recency after age", over(base, recency = "tx_after"), "recency_after_age",
          "the customer c17 bought last (recency 39) after the end of the period (age 37): recency is at most age"),
  refusal("refusal recency without purchase", over(base, recency = "tx_nopurchase"), "recency_without_purchase",
          "the customer c15 has no repeat purchase but a recency of 12: recency is 0 without repeat purchases"),
  refusal("refusal same moment", over(base, frequency = "x_same_moment"), "same_moment",
          paste0("the customer c10 has 2 repeat purchases at recency 0: purchases of the same day must be one purchase; ",
                 "group by customer and day first")),
  refusal("refusal spend not positive", over(base, spend = "m_zero"), "spend_not_positive",
          paste0("the customer c3 has an average spend of 0 with repeat purchases: the spend model needs amounts above 0; ",
                 "leave out returns and refunds, or those customers"), variant = "value"),
  refusal("refusal holdout negative", over(base, holdout_transactions = "h_neg", holdout_length = 26), "holdout_negative",
          "holdout purchases are whole numbers, 0 or more: the customer c20 has −1"),
  refusal("refusal few customers", base, "few_customers", "12 customers: these models need at least 20", data = "data/clv_refusals_small.csv"),
  refusal("refusal no repeaters", over(base, frequency = "x_none", recency = "tx_none"), "no_repeaters",
          "no customer bought again: the models cannot tell the purchase rate from the dropout without repeat purchases"),
  refusal("refusal few repeaters", over(base, frequency = "x_few", recency = "tx_few"), "few_repeaters",
          "3 customers bought again: these models need at least 5"),
  refusal("refusal few spenders", over(base, spend = "m_few"), "few_spenders", "3 repeat customers have a spend: the spend model needs at least 5", variant = "value"),
  refusal("refusal BG/BB opportunities", over(bb, opportunities = "n_zero"), "bgbb_opportunities", "opportunities run from 1 to 1,000: the customer c12 has 0",
          model = "bg_bb"),
  refusal("refusal BG/BB frequency", over(bb, recency = "tx_bb_low"), "bgbb_frequency",
          "the customer c8 has 3 transactions in 1 opportunities up to its last: at most one transaction per opportunity", model = "bg_bb"),
  refusal("refusal BG/BB recency after opportunities", over(bb, recency = "tx_bb_late"), "recency_after_age",
          "the customer c9 bought last at the opportunity 8, after the last of its 6 opportunities: recency is at most opportunities", model = "bg_bb"),
  refusal("refusal BG/BB holdout", over(bb, holdout_transactions = "h_bb_over", holdout_length = 5), "bgbb_holdout",
          "a customer has at most one transaction per opportunity: the customer c14 has 9 in the 5 opportunities of the holdout", model = "bg_bb")
)
# the cases of the nodes' fits are added here by lots B2 and B3

expected <- list(
  format_version = 1,
  generated = list(by = "make_reference.R", note = paste0("lot B1 writes the refusal cases; lots B2 and B3 add the fitted cases ",
                                                           "(docs/specs/customer-lifetime-models.md, «Reference values»)"),
                   R = paste(R.version$major, R.version$minor, sep = "."), packages = list(jsonlite = as.character(utils::packageVersion("jsonlite")))),
  cases = cases)
writeLines(jsonlite::toJSON(expected, auto_unbox = TRUE, pretty = TRUE, null = "null", digits = NA), file.path(here, "expected.json"), useBytes = TRUE)
cat("wrote", length(cases), "cases\n")
