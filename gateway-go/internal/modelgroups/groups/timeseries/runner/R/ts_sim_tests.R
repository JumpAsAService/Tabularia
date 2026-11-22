# The self-test of the simulation engine (ts_sim.R, lot G2): added to TAB_COMMON_TESTS. Its model is a toy, i.i.d. returns
# (normal, or Student t scaled to unit variance), whose cumulative return H_k = exp(r_1 + … + r_k) − 1 is the risk variable.
# Every number is checked against the same measure computed on the whole matrix of the paths, read back from the file (the
# tests alone hold 10,000 × 10 values at once).

# The toy model: draws path by path, h values a path, as the risk node's models do.
ts_sim_toy <- function(dist = "norm", mu = 3e-4, sigma = 0.012, df = 5) {
  list(columns = c("value", "cumulative"), draw = function(n, h) {
    z <- if (identical(dist, "norm")) stats::rnorm(n * h) else stats::rt(n * h, df) * sqrt((df - 2) / df)
    r <- matrix(mu + sigma * z, nrow = h)
    s <- r
    for (k in seq_len(h)[-1L]) s[k, ] <- s[k - 1L, ] + r[k, ]
    list(value = r, cumulative = expm1(s))
  })
}

.ts_sim_limits <- function(...) {
  utils::modifyList(list(memory_bytes = 2^31, tmp_bytes = 2^31, time_budget_seconds = 3600, seed_block = 1000L, risk_bins = 1024L,
                         max_sample_paths = 20L), list(...))
}

.ts_sim_measures_of <- function(...) {
  utils::modifyList(list(var_level = 0.99, es_level = 0.975, tail = "lower", quantiles = c(0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99),
                         threshold = NULL, alpha = 0.05, drawdown = TRUE), list(...))
}

# A run that keeps its file, and the file read back whole (path, step, cumulative): the test's own matrix.
.ts_sim_case <- function(model, N, h, seed = 1L, limits = .ts_sim_limits(), measures = .ts_sim_measures_of(), ...) {
  file <- tempfile("paths-", fileext = ".parquet")
  sim <- ts_sim_run(model, N, h, seed, limits, measures, file = file, keep = TRUE, ...)
  sim$table <- as.data.frame(arrow::read_parquet(file))
  sim$row_groups <- arrow::ParquetFileReader$create(file)$num_row_groups
  unlink(file)
  sim
}

.ts_sim_close <- function(got, want, rel = 1e-12, scale = abs(want)) {
  length(got) == length(want) && all((is.na(got) & is.na(want)) | abs(got - want) <= rel * pmax(scale, 1e-300))
}

# Every measure of a run against the whole matrix.
.ts_sim_check_whole <- function(sim, measures, N, h) {
  H <- matrix(sim$table$cumulative, nrow = h)
  .tab_expect(identical(sim$table$path, rep(seq_len(N), each = h)) && identical(sim$table$step, rep(seq_len(h), N)),
              "the rows are sorted by path, then step")
  sgn <- if (identical(measures$tail, "upper")) -1 else 1
  alpha <- measures$alpha
  z <- stats::qnorm(1 - alpha / 2)
  p <- round(1 - measures$var_level, 12)
  pe <- round(1 - measures$es_level, 12)
  ke <- round(N * pe, 6)
  fl <- floor(ke)
  kv <- ceiling(ke)
  l <- stats::qbinom(alpha / 2, N, p)
  u <- stats::qbinom(1 - alpha / 2, N, p) + 1
  s <- sim$steps
  for (k in seq_len(h)) {
    x <- sort(sgn * H[k, ])
    var <- x[[ceiling(round(N * p, 6))]]
    es <- (sum(x[seq_len(fl)]) + (ke - fl) * (if (ke > fl) x[[fl + 1L]] else 0)) / ke
    tail <- x[seq_len(kv)]
    se <- sqrt((stats::var(tail) + (1 - pe) * (es - x[[kv]])^2) / ke)
    lo <- if (l >= 1) x[[l]] else NA_real_
    hi <- if (u <= N) x[[u]] else NA_real_
    want <- if (sgn > 0) c(var, lo, hi, es) else c(-var, -hi, -lo, -es)
    .tab_expect(identical(c(s$var[[k]], s$var_low[[k]], s$var_high[[k]]), want[1:3]), "step ", k, ": VaR and its interval ",
                paste(c(s$var[[k]], s$var_low[[k]], s$var_high[[k]]), collapse = " "), ", the whole matrix ", paste(want[1:3], collapse = " "))
    .tab_expect(.ts_sim_close(s$es[[k]], want[[4L]]), sprintf("step %d: ES %.17g, the whole matrix %.17g", k, s$es[[k]], want[[4L]]))
    .tab_expect(.ts_sim_close(s$es_se[[k]], se, 1e-9), sprintf("step %d: ES's se %.17g, the whole matrix %.17g", k, s$es_se[[k]], se))
    .tab_expect(.ts_sim_close(c(s$es_low[[k]], s$es_high[[k]]), sort(c(want[[4L]] - z * se, want[[4L]] + z * se)), 1e-9), "step ", k, ": ES's interval")
    y <- H[k, ]
    m <- mean(y)
    sd <- stats::sd(y)
    d <- y - m
    M2 <- sum(d^2)
    .tab_expect(.ts_sim_close(s$mean[[k]], m, 1e-12, sd), sprintf("step %d: mean %.17g, %.17g", k, s$mean[[k]], m))
    .tab_expect(.ts_sim_close(s$sd[[k]], sd), sprintf("step %d: sd %.17g, %.17g", k, s$sd[[k]], sd))
    .tab_expect(.ts_sim_close(s$skewness[[k]], sqrt(N) * sum(d^3) / M2^1.5, 1e-10, 1), "step ", k, ": skewness")
    .tab_expect(.ts_sim_close(s$kurtosis[[k]], N * sum(d^4) / M2^2 - 3, 1e-10, 1), "step ", k, ": kurtosis")
    .tab_expect(.ts_sim_close(c(s$mean_low[[k]], s$mean_high[[k]]), m + c(-1, 1) * z * sd / sqrt(N), 1e-12, sd), "step ", k, ": mean's interval")
    # the fan: within its declared error
    ys <- sort(y)
    for (i in seq_along(measures$quantiles)) {
      true <- ys[[max(1, ceiling(round(N * measures$quantiles[[i]], 6)))]]
      .tab_expect(abs(sim$quantiles[k, i] - true) <= s$band_error[[k]] * (1 + 1e-9) + 1e-15,
                  sprintf("step %d: quantile %g at %.17g, the whole matrix %.17g, error %.3g", k, measures$quantiles[[i]], sim$quantiles[k, i], true,
                          s$band_error[[k]]))
    }
  }
  if (!is.null(measures$threshold)) {
    beyond <- if (sgn > 0) H < measures$threshold else H > measures$threshold
    crossed <- apply(beyond, 2L, cumsum) > 0
    if (h == 1L) crossed <- matrix(crossed, nrow = 1L)
    cb <- rowSums(beyond)
    cc <- rowSums(crossed)
    .tab_expect(identical(s$count_beyond, as.numeric(cb)) && identical(s$count_crossed, as.numeric(cc)), "the threshold's counts")
    cp <- stats::binom.test(cb[[h]], N, conf.level = 1 - alpha)$conf.int
    .tab_expect(.ts_sim_close(c(s$prob_beyond_low[[h]], s$prob_beyond_high[[h]]), as.numeric(cp), 1e-12), "Clopper–Pearson at the horizon")
  }
  if (isTRUE(measures$drawdown)) {
    D <- apply(1 + H, 2L, function(w) max(0, 1 - w / cummax(pmax(w, 1))))
    if (h == 1L) D <- pmax(0, 1 - (1 + H[1L, ]) / pmax(1 + H[1L, ], 1))
    ds <- sort(D)
    dd <- sim$drawdown
    lm <- stats::qbinom(alpha / 2, N, 0.5)
    um <- stats::qbinom(1 - alpha / 2, N, 0.5) + 1
    .tab_expect(identical(c(dd$median, dd$median_low, dd$median_high), c(ds[[ceiling(N / 2)]], ds[[lm]], ds[[um]])), "the drawdown's median")
    .tab_expect(identical(dd$var, ds[[ceiling(round(N * measures$var_level, 6))]]), sprintf("the drawdown's VaR %.17g", dd$var))
    .tab_expect(.ts_sim_close(dd$mean, mean(D), 1e-12, stats::sd(D)) && .ts_sim_close(dd$sd, stats::sd(D)), "the drawdown's mean and sd")
  }
  invisible(TRUE)
}

TAB_COMMON_TESTS[["timeseries: the simulation's streamed measures equal the whole matrix's (VaR, ES, intervals, moments, threshold, drawdown, fan)"]] <- function(code) {
  if (!requireNamespace("arrow", quietly = TRUE)) return("skipped")
  # normal, lower tail, a threshold, the drawdown, several batches (3 blocks each)
  ms <- .ts_sim_measures_of(threshold = -0.03)
  sim <- .ts_sim_case(ts_sim_toy("norm"), 10000, 10, limits = .ts_sim_limits(), measures = ms, batch_blocks = 3L)
  .tab_expect(sim$run$batches == 4L && sim$row_groups == 4L, "4 batches, 4 row groups: ", sim$run$batches, ", ", sim$row_groups)
  .ts_sim_check_whole(sim, ms, 10000, 10)
  # Student t, the upper tail, levels whose N·p is not whole (10,000 × 0.0025 = 25, × 0.0123 = 123)
  ms <- .ts_sim_measures_of(tail = "upper", threshold = 0.05, var_level = 0.9877, es_level = 0.9975, alpha = 0.1)
  sim <- .ts_sim_case(ts_sim_toy("t", df = 4), 10000, 10, seed = 7L, measures = ms)
  .ts_sim_check_whole(sim, ms, 10000, 10)
  # one step: half the drawdowns are 0, a point mass the bin at the minimum holds; ES with N·p' = 2.5
  ms <- .ts_sim_measures_of(var_level = 0.999, es_level = 0.99975)
  sim <- .ts_sim_case(ts_sim_toy("norm"), 10000, 1, seed = 3L, measures = ms)
  .tab_expect(identical(sim$drawdown$median, 0), "the median drawdown of one step is 0")
  .ts_sim_check_whole(sim, ms, 10000, 1)
}

TAB_COMMON_TESTS[["timeseries: the simulation gives the same bits at every batch size and memory (64 MiB, 256 MiB, 2 GiB), and a run's first paths are a shorter run's"]] <- function(code) {
  if (!requireNamespace("arrow", quietly = TRUE)) return("skipped")
  ms <- .ts_sim_measures_of(tail = "upper", threshold = 0.02)
  model <- ts_sim_toy("t")
  strip <- function(sim) sim[c("steps", "quantiles", "drawdown", "hist", "sample", "table")]
  # baseline 0: the three budgets are what the batches may use; at 100 steps 64 MiB holds 3 blocks, the others 10 (a row
  # group's 1,048,576 rows)
  runs <- list()
  for (mb in c(64, 256, 2048)) {
    runs[[paste0(mb, " MiB")]] <- .ts_sim_case(model, 10000, 100, seed = 11L, limits = .ts_sim_limits(memory_bytes = mb * 2^20),
                                              measures = ms, baseline_bytes = 0)
  }
  for (bb in c(1L, 3L)) runs[[paste0(bb, " blocks")]] <- .ts_sim_case(model, 10000, 100, seed = 11L, measures = ms, batch_blocks = bb)
  sizes <- unname(vapply(runs, function(r) r$run$batch_paths, 0))
  want <- c(vapply(c(64, 256, 2048), function(mb) ts_sim_batch_paths(mb * 2^20, 100, 3, 1000, TS_SIM_BLOCK_FACTOR, TS_SIM_BATCH_FACTOR,
                                                                    TS_SIM_FIXED_BYTES, 10000), 0), 1000, 3000)
  .tab_expect(identical(sizes, want) && sizes[[1L]] < sizes[[2L]], "batch sizes ", paste(sizes, collapse = " "))
  .tab_expect(identical(unname(vapply(runs, function(r) r$row_groups, 0L)), as.integer(ceiling(10000 / sizes))), "one row group a batch")
  for (nm in names(runs)[-1L]) .tab_expect(identical(strip(runs[[nm]]), strip(runs[[1L]])), nm, " differs from 64 MiB")
  # the prefix: the 2,000 paths of a run of 2,000 are the first 2,000 of a run of 10,000
  short <- .ts_sim_case(model, 2000, 100, seed = 11L, measures = ms)
  first <- lapply(runs[[1L]]$table, `[`, seq_len(2000 * 100))
  .tab_expect(identical(as.list(short$table), first) && identical(short$sample, runs[[1L]]$sample),
              "the prefix property")
  # the streams: block 3 is the third L'Ecuyer-CMRG stream of the seed, drawn by the model
  set.seed(11L, kind = "L'Ecuyer-CMRG", normal.kind = "Inversion", sample.kind = "Rejection")
  s <- parallel::nextRNGStream(parallel::nextRNGStream(.Random.seed))
  assign(".Random.seed", s, envir = globalenv())
  b3 <- model$draw(1000L, 100L)$cumulative
  tab_seed(list(params = list(seed = 1L)))
  .tab_expect(identical(as.vector(b3), runs[[1L]]$table$cumulative[2000 * 100 + seq_len(1000 * 100)]), "block 3 is the seed's third stream")
}

TAB_COMMON_TESTS[["timeseries: the simulation's file: one row group a batch, the time's own type, sorted rows; the chunked copy"]] <- function(code) {
  if (!requireNamespace("arrow", quietly = TRUE)) return("skipped")
  for (time in list(arrow::Array$create(bit64::as.integer64(3e9) + 0:4), arrow::Array$create(as.Date("2026-07-15") + 1:5))) {
    file <- tempfile(fileext = ".parquet")
    sim <- ts_sim_run(ts_sim_toy(), 2500, 5, 1L, .ts_sim_limits(), .ts_sim_measures_of(quantiles = NULL, drawdown = FALSE), time = time,
                      file = file, keep = TRUE, batch_blocks = 2L)
    reader <- arrow::ParquetFileReader$create(file)
    schema <- reader$GetSchema()
    .tab_expect(identical(names(schema), c("path", "step", "time", "value", "cumulative")), "columns ", paste(names(schema), collapse = " "))
    .tab_expect(schema$time$type == time$type && schema$path$type == arrow::int32() && schema$step$type == arrow::int32(),
                "the time keeps its type: ", schema$time$type$ToString())
    .tab_expect(reader$num_row_groups == 2L && identical(vapply(0:1, function(i) reader$ReadRowGroup(i)$num_rows, 0L), c(10000L, 2500L)),
                "one row group a batch of 2 blocks")
    tab <- arrow::read_parquet(file, as_data_frame = FALSE)
    .tab_expect(identical(as.vector(tab$time), as.vector(time$Take(rep(0:4, 2500)))), "the time of each step")
    .tab_expect(identical(sim$run$paths_bytes, file.size(file)) && all(is.na(sim$steps$band_error) | sim$steps$band_error == 0), "the run's notes")
    # the copy, in chunks of 1,000 bytes, to a path and to an address
    for (to in c(tempfile(fileext = ".parquet"), paste0("file://", tempfile(fileext = ".parquet")))) {
      ts_sim_copy(file, to, chunk_bytes = 1000)
      local <- sub("^file://", "", to)
      .tab_expect(identical(readBin(local, "raw", file.size(file) + 1), readBin(file, "raw", file.size(file) + 1)), "the copy to ", to)
      unlink(local)
    }
    unlink(file)
  }
  # a run that does not keep its file deletes it
  file <- tempfile(fileext = ".parquet")
  sim <- ts_sim_run(ts_sim_toy(), 1000, 5, 1L, .ts_sim_limits(), .ts_sim_measures_of(), file = file)
  .tab_expect(!file.exists(file) && is.null(sim$file), "the paths file is deleted when it is not the output")
}

TAB_COMMON_TESTS[["timeseries: the simulation refuses with its numbers (paths, memory, disk, time, a value not finite) and leaves no file"]] <- function(code) {
  if (!requireNamespace("arrow", quietly = TRUE)) return("skipped")
  file <- file.path(tempfile("sim-"), "paths.parquet")
  run <- function(N = 10000, h = 10, limits = .ts_sim_limits(), measures = .ts_sim_measures_of(), model = ts_sim_toy(), ...) {
    ts_sim_run(model, N, h, 1L, limits, measures, file = file, ...)
  }
  msg <- .tab_refused_with("too_few_paths", run(N = 100, measures = .ts_sim_measures_of(var_level = 0.999)))
  .tab_expect(identical(msg, "with 100 paths and a 99.9 % level no simulated value lies beyond the VaR: at least 1,000 paths"), msg)
  # memory: one block of 1,000 paths of 1,000 steps does not fit
  need <- ts_sim_batch_bytes(1000, 1000, 3, 1000, TS_SIM_BLOCK_FACTOR, TS_SIM_BATCH_FACTOR, TS_SIM_FIXED_BYTES)
  msg <- .tab_refused_with("horizon_memory", run(N = 1000, h = 1000, limits = .ts_sim_limits(memory_bytes = 50e6), baseline_bytes = 10e6))
  .tab_expect(identical(msg, paste0("a block of 1,000 paths of 1,000 steps needs about ", .ts_sim_bytes(need), " and the group's memory leaves ",
                                    "40 MB after the fit: fewer steps, or more memory for the group in Administration → Statistical models")), msg)
  # disk: projected from the first row group
  msg <- .tab_refused_with("too_many_values", run(N = 100000, limits = .ts_sim_limits(tmp_bytes = 1e6), batch_blocks = 1L))
  .tab_expect(grepl("^100,000 paths of 10 steps would write about [0-9.]+ MB; this Job's disk holds 1 MB: fewer paths or steps$", msg), msg)
  .tab_expect(!file.exists(file), "a refusal leaves no file")
  # time: the task's clock started 20 minutes ago, the node's timeout is 10
  msg <- .tab_refused_with("too_slow", run(limits = .ts_sim_limits(started_unix = as.numeric(Sys.time()) - 1200, time_budget_seconds = 600,
                                                                    group_max_seconds = 1800)))
  .tab_expect(grepl("^about 20 minutes for 10,000 paths of 10 steps; the node's timeout is 10 minutes: fewer paths or steps, or a longer timeout, up to the group's 30 minutes$", msg), msg)
  .tab_expect(!file.exists(file), "a refusal leaves no file")
  msg <- .tab_refused_with("not_finite", run(model = ts_sim_toy(sigma = 400), h = 3))
  .tab_expect(grepl("^the simulated path [0-9,]+ is not a finite number at step [123]: ", msg), msg)
  .tab_expect(!file.exists(file), "a refusal leaves no file")
}

TAB_COMMON_TESTS[["timeseries: the simulation stays below memory_bytes = 256 MiB in a process of its own, with more than one batch"]] <- function(code) {
  if (!requireNamespace("arrow", quietly = TRUE)) return("skipped")
  out <- tempfile(fileext = ".json")
  script <- tempfile(fileext = ".R")
  lib <- c(sort(list.files(file.path(code, "common", "R"), pattern = "[.]R$", full.names = TRUE)),
           file.path(code, "runner", "R", c("ts_sim.R", "ts_sim_tests.R")))
  # one line a file, each path a quoted string of its own: a ConfigMap's mount resolves to a long
  # «..<timestamp>» directory, and a deparsed vector of such paths breaks over lines the child cannot parse
  quoted <- function(path) encodeString(path, quote = '"')
  writeLines(c(sprintf("source(%s)", quoted(lib)),
               "suppressMessages(library(arrow))",
               "limits <- .ts_sim_limits(memory_bytes = 256 * 2^20)",
               sprintf("sim <- ts_sim_run(ts_sim_toy('t'), 200000, 10, 1L, limits, .ts_sim_measures_of(threshold = -0.05), file = %s)",
                       quoted(tempfile(fileext = ".parquet"))),
               sprintf("writeLines(jsonlite::toJSON(list(hwm = ts_sim_hwm(), batches = sim$run$batches, baseline = sim$run$baseline_bytes), auto_unbox = TRUE, digits = NA), %s)",
                       quoted(out))),
             script)
  status <- system2(file.path(R.home("bin"), "Rscript"), c("--vanilla", script), stdout = FALSE, stderr = FALSE)
  .tab_expect(identical(status, 0L) && file.exists(out), "the child process failed (", status, ")")
  got <- jsonlite::fromJSON(out)
  unlink(c(out, script))
  .tab_expect(got$hwm < 256 * 2^20, sprintf("peak %.0f MB, above 256 MiB", got$hwm / 1e6))
  .tab_expect(got$batches > 1L, "one batch: ", got$batches)
}
