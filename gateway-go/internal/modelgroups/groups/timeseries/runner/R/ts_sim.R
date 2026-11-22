# The simulation engine of the risk node (docs/specs/garch-risk.md, «The simulation», «The Parquet file of the paths», «The
# measures, without holding the paths (two passes)», «Time»; lot G2). It knows nothing of GARCH or ARIMA: a model hands it a
# function that draws a seed block of paths, and it does the rest with the paths never all in memory.
#
#   sim <- ts_sim_run(model, paths, horizon, seed, limits, measures, time = NULL, file = "/tmp/paths.parquet", keep = FALSE)
#
# model    list(draw = function(n, h) …, columns = c("value", "cumulative", …), fixed_bytes, block_factor, batch_factor)
#          `draw(n, h)` is called once per seed block with .Random.seed already set to that block's L'Ecuyer-CMRG stream; it
#          draws the block's innovations in one call, path by path, and returns a list of h × n matrices (column j is the
#          block's path j, row k its step k): `cumulative` (H_k, the risk variable, the one quantity every measure is made of)
#          and one matrix per other name of `columns`. `columns` are the file's Float64 columns in their order (`cumulative`
#          among them). `fixed_bytes` (F), `block_factor` (φ) and `batch_factor` (ρ) are the model's measured memory constants
#          (below); the toy model's are the default.
# paths    N, horizon h, seed: the run's numbers. N·h is computed in doubles.
# limits   the task's limits: memory_bytes, tmp_bytes, time_budget_seconds, started_unix, seed_block, risk_bins (and,
#          when the caller has it, group_max_seconds for the sentence of `too_slow`).
# measures list(var_level, es_level, tail = "lower" | "upper", quantiles, threshold = NULL or a number on H's scale,
#          alpha, drawdown = TRUE when H is a relative change (W_k = 1 + H_k), max_sample_paths).
# time     NULL, or an Arrow Array of length h: the `time` column of each step, of the input's own type.
# file     where the paths are written (always: pass 2 reads it); `keep = FALSE` deletes it at the end, `TRUE` leaves it for
#          ts_sim_copy() to the output. A refusal always deletes it: no partial result.
#
# It returns list(steps = a data frame per step, quantiles = h × length(quantiles) matrix (the fan), drawdown = a list or
# NULL, hist = list(horizon, drawdown) of bars, sample = the first max_sample_paths paths, run = what run.json and the notes
# say, file). Every measure is on H's own scale: VaR, ES and quantiles are values of H; G3 maps them (price, money) by
# increasing linear maps, which carry quantiles, means and expected shortfalls exactly.
#
# Memory. A batch is a whole number of seed blocks, simulated block by block: a block's draws become one Arrow record batch;
# the batch's record batches are one row group of the Parquet file, then freed by a full collection (they survived the
# batch's minor ones, so only a full one frees them). The engine's own work on a block goes a slice of steps at a time
# (TS_SIM_SLICE_VALUES), and R collects its garbage when the engine asks, after every TS_SIM_GC_VALUES values made. The
# growth of the resident size over R0 (read after the accumulators and a warm-up of the writer and reader) is modelled as
# F + 8·(φ·B_s·h + ρ·m·P·h): P the batch's paths, m the bytes of a row of the file's columns divided by 8 (counted from the
# schema), F a fixed part (the collections' garbage, the reader's buffers in pass 2), φ a block's own work, ρ the batch's
# data with its copies. F, φ and ρ were MEASURED (lot G2, the spec's «As built / G2») on the toy model in fresh processes,
# both passes included, the largest of each over 10 to 1,000 steps and 1 to 104 blocks a batch; G3 measures its models'.
# The batch is the largest whole number of blocks that fits memory_bytes − R0, never more rows than one Parquet row group
# holds (Arrow's 1,048,576: a bound of the library); not even one block: refused with the numbers.
#
# Reproducibility. Block b draws from the b-th L'Ecuyer-CMRG stream of the seed, whatever the batch; every sum is made per
# block and added in block order; pass 2 sorts what it selects. The same seed gives the same numbers, bit for bit, at every
# batch size, and the first k blocks of a run are the k blocks of a shorter run (the prefix property).
#
# Pass 1 (while simulating): per quantity (H_1 … H_h, and the drawdown D when asked) the moments by Pébay's merge, and a
# histogram of B equal bins between the first block's minimum and maximum, plus a bin below, a bin AT the minimum (a point
# mass, the drawdown's 0, needs no selection) and a bin above; per bin the count; the threshold's counts.
# Pass 2 (after the last batch, the file read a row group at a time, one column): the values of the bins that hold the ranks
# the exact measures need are kept, sorted, and read; Σx and Σx² of the values below the bins of the ES's ranks are summed
# block by block in block order; the fan's edges are located in a sub-histogram of B bins of their bin, within its width / B,
# said per step as `band_error`.

TS_SIM_ROW_GROUP_ROWS <- 1048576 # Arrow C++'s default maximum row group length (parquet::DEFAULT_MAX_ROW_GROUP_LENGTH),
                                 # read in arrow 26.0.1: a WriteTable of 3,000,000 rows wrote groups of 1,048,576

# The toy model's memory constants, measured (spec «As built / G2»): growth over R0 = F + 8·(φ·B_s·h + ρ·m·P·h), bounding
# every measurement (10, 100, 250 and 1,000 steps; 1 to 104 blocks a batch; both passes). A model of G3 passes its own
# (model$fixed_bytes, block_factor, batch_factor). F was 12 MB until lot G9, which measured the second pass again over the
# whole run (VmHWM − R0): it adds 20 to 28 MB beyond the model at 10, 100 and 250 steps (its sub-histograms, its slices'
# selections and R's garbage between collections, nearly the same at every batch size), and at 256 MiB the process passed
# memory_bytes by up to 17 MB. F = 48 MB bounds every point again, by 7.7 MB at least (the spec's «As built / G9»).
TS_SIM_BLOCK_FACTOR <- 31
TS_SIM_BATCH_FACTOR <- 4
TS_SIM_FIXED_BYTES <- 48e6
TS_SIM_GC_VALUES <- 65536

# ── small helpers ──────────────────────────────────────────────────────────────

.ts_sim_status_kb <- function(field) {
  lines <- tryCatch(readLines("/proc/self/status", warn = FALSE), error = function(e) character())
  line <- grep(paste0("^", field, ":"), lines, value = TRUE)
  if (!length(line)) return(NA_real_)
  as.numeric(gsub("[^0-9]", "", line[[1L]])) * 1024
}

# The process's resident size and its peak (bytes).
ts_sim_rss <- function() .ts_sim_status_kb("VmRSS")
ts_sim_hwm <- function() .ts_sim_status_kb("VmHWM")

# The run's memory peak: the cgroup's (cgroup v2 memory.peak: the Job's, page cache included) when it can be read, the
# process's VmHWM otherwise.
ts_sim_peak <- function() {
  cg <- suppressWarnings(tryCatch(as.numeric(readLines("/sys/fs/cgroup/memory.peak", warn = FALSE)[[1L]]), error = function(e) NA_real_))
  list(cgroup_bytes = cg, process_bytes = ts_sim_hwm(), source = if (is.finite(cg)) "memory.peak" else "VmHWM")
}

# The memory the run may use: the task's memory_bytes; without it the cgroup's limit; without one the machine's available.
.ts_sim_memory_limit <- function(limits) {
  given <- as.numeric(limits$memory_bytes %||% NA)
  if (is.finite(given) && given > 0) return(given)
  cg <- suppressWarnings(tryCatch(as.numeric(readLines("/sys/fs/cgroup/memory.max", warn = FALSE)[[1L]]), error = function(e) NA_real_))
  if (is.finite(cg) && cg > 0) return(cg)
  lines <- tryCatch(readLines("/proc/meminfo", warn = FALSE), error = function(e) character())
  line <- grep("^MemAvailable:", lines, value = TRUE)
  if (length(line)) return(as.numeric(gsub("[^0-9]", "", line[[1L]])) * 1024)
  Inf
}

.ts_sim_int <- function(x) format(x, big.mark = ",", scientific = FALSE, trim = TRUE)

.ts_sim_bytes <- function(b) {
  if (b >= 1e9) return(paste0(formatC(b / 1e9, format = "f", digits = if (b >= 1e10) 0L else 1L), " GB"))
  if (b >= 1e6) return(paste0(formatC(b / 1e6, format = "f", digits = 0L), " MB"))
  paste0(formatC(max(b, 0) / 1e3, format = "f", digits = 0L), " kB")
}

.ts_sim_duration <- function(s) {
  if (s >= 120) return(paste0(.ts_sim_int(round(s / 60)), " minutes"))
  paste0(.ts_sim_int(max(1, round(s))), " seconds")
}

.ts_sim_level <- function(c) paste0(format(round(100 * c, 10), drop0trailing = TRUE, scientific = FALSE, trim = TRUE), " %")

# 1 − c as the person meant it (0.01 for 0.99, not 0.010000000000000009), and ⌈N·p⌉ of that p.
.ts_sim_p <- function(c) round(1 - c, 12)
.ts_sim_rank <- function(n, p) max(1, min(n, ceiling(round(n * p, 6))))

# ── the toy-free parts every run needs ─────────────────────────────────────────

# The bin of each value: 0 below lo, 1 equal to lo, 2 … B + 1 the equal bins of (lo, hi], B + 2 above hi. x a matrix with one
# row per quantity (lo, hi, w recycled down its rows) or a vector of one quantity. The same function in both passes on the
# same doubles: the same bins.
.ts_sim_bins <- function(x, lo, hi, w, B) {
  i <- floor((x - lo) / ifelse(w > 0, w, 1)) + 2
  i[i > B + 1] <- B + 1
  i[x == lo] <- 1
  i[x < lo] <- 0
  i[x > hi] <- B + 2
  storage.mode(i) <- "integer"
  i
}

# The drawdown of each path: D = max_k (1 − W_k / max_{j ≤ k} W_j), W_k = 1 + H_k, W_0 = 1. H an h × n matrix.
.ts_sim_drawdown <- function(H) {
  n <- ncol(H)
  peak <- rep(1, n)
  dd <- numeric(n)
  for (k in seq_len(nrow(H))) {
    w <- 1 + H[k, ]
    peak <- pmax(peak, w)
    dd <- pmax(dd, 1 - w / peak)
  }
  dd
}

# The memory a batch of `paths` paths adds over R0 (bytes), and the largest batch (whole blocks) that fits `free` bytes.
ts_sim_batch_bytes <- function(paths, horizon, m, block, phi, rho, fixed) fixed + 8 * horizon * (phi * block + rho * m * paths)

ts_sim_batch_paths <- function(free, horizon, m, block, phi, rho, fixed, total) {
  cap <- floor(TS_SIM_ROW_GROUP_ROWS / horizon / block) * block
  fit <- floor(((free - fixed) / (8 * horizon) - phi * block) / (rho * m) / block) * block
  min(fit, max(cap, block), ceiling(total / block) * block)
}

# The steps of a block cut in slices of at most TS_SIM_SLICE_VALUES values: the engine's own temporaries are a slice's,
# not a block's (a block of 1,000 paths of 1,000 steps is a million values). Every measure is per step, so a slice changes
# no bit of any result: it is not part of the method, only of the memory.
TS_SIM_SLICE_VALUES <- 65536
.ts_sim_slices <- function(h, n) {
  per <- max(1L, as.integer(TS_SIM_SLICE_VALUES %/% n))
  split(seq_len(h), ceiling(seq_len(h) / per))
}

# The writer, the reader and the functions of both passes used once on a few rows, before R0 is read: what they load and
# initialise the first time (Arrow's Parquet code, compiled R closures) is then in R0, not counted as a batch's growth.
.ts_sim_warm <- function(schema, props, time) {
  file <- tempfile(fileext = ".parquet")
  on.exit(unlink(file), add = TRUE)
  rows <- lapply(names(schema), function(nm) {
    if (nm == "time") return(time$Slice(0, 1))
    if (nm %in% c("path", "step")) return(arrow::Array$create(1L))
    arrow::Array$create(0)
  })
  sink <- arrow::FileOutputStream$create(file)
  writer <- arrow::ParquetFileWriter$create(schema, sink, properties = props)
  writer$WriteTable(arrow::Table$create(do.call(arrow::record_batch, c(stats::setNames(rows, names(schema)), list(schema = schema)))), chunk_size = 1L)
  writer$Close()
  sink$close()
  reader <- arrow::ParquetFileReader$create(file, props = arrow::ParquetArrowReaderProperties$create(use_threads = FALSE), mmap = FALSE)
  invisible(as.vector(reader$ReadRowGroup(0L, column_indices = 0L)$column(0L)$Slice(0, 1)))
  x <- matrix(stats::runif(4L), 2L)
  invisible(tapply(1:4, c(1L, 1L, 2L, 2L), min))
  invisible(parallel::nextRNGStream(c(10407L, 1:6)))
  invisible(.ts_sim_bins(x, c(0, 0), c(1, 1), c(0.5, 0.5), 2L))
  gc()
  invisible(TRUE)
}

# ── pass 1: accumulators ───────────────────────────────────────────────────────

# An environment: the slices update its vectors in place (a list would be copied at each update, and its vectors with it).
.ts_sim_acc_new <- function(nq, B) {
  K <- B + 3L
  list2env(list(B = B, K = K, nq = nq, n = 0, cnt = numeric(nq * K),
                lo = numeric(nq), hi = numeric(nq), w = numeric(nq), gmin = rep(Inf, nq), gmax = rep(-Inf, nq),
                mean = numeric(nq), M2 = numeric(nq), M3 = numeric(nq), M4 = numeric(nq)), parent = emptyenv())
}

# A block's values of the quantities `q` (x: length(q) × n matrix) into the accumulators: its histogram, its moments merged
# after those of the earlier blocks (Pébay 2008, pairwise, the order fixed by the blocks).
.ts_sim_acc_add <- function(acc, x, q) {
  B <- acc$B
  K <- acc$K
  n <- ncol(x)
  lo <- acc$lo[q]
  hi <- acc$hi[q]
  w <- acc$w[q]
  idx <- .ts_sim_bins(x, lo, hi, w, B)
  out <- which(idx == 0L | idx == B + 2L)
  if (length(out)) {
    vals <- x[out]
    rows <- (out - 1L) %% nrow(x) + 1L
    mn <- tapply(vals, rows, min)
    mx <- tapply(vals, rows, max)
    at <- q[as.integer(names(mn))]
    acc$gmin[at] <- pmin(acc$gmin[at], as.vector(mn))
    acc$gmax[at] <- pmax(acc$gmax[at], as.vector(mx))
    rm(vals, rows)
  }
  # the quantities q are consecutive: their bins are one range of the accumulators
  base <- (q[[1L]] - 1L) * K
  g <- as.vector(idx + 1L + (q - q[[1L]]) * K)
  rm(idx, out)
  span <- base + seq_len(length(q) * K)
  acc$cnt[span] <- acc$cnt[span] + tabulate(g, nbins = length(q) * K)
  rm(g)
  # the block's moments, then the merge
  mb <- rowMeans(x)
  d <- x - mb
  d2 <- d * d
  M2b <- rowSums(d2)
  M3b <- rowSums(d2 * d)
  M4b <- rowSums(d2 * d2)
  rm(d, d2)
  na <- acc$n
  if (na == 0) {
    acc$mean[q] <- mb
    acc$M2[q] <- M2b
    acc$M3[q] <- M3b
    acc$M4[q] <- M4b
  } else {
    nb <- n
    nn <- na + nb
    delta <- mb - acc$mean[q]
    ma <- acc$mean[q]
    M2a <- acc$M2[q]
    M3a <- acc$M3[q]
    M4a <- acc$M4[q]
    acc$mean[q] <- ma + delta * nb / nn
    acc$M2[q] <- M2a + M2b + delta^2 * na * nb / nn
    acc$M3[q] <- M3a + M3b + delta^3 * na * nb * (na - nb) / nn^2 + 3 * delta * (na * M2b - nb * M2a) / nn
    acc$M4[q] <- M4a + M4b + delta^4 * na * nb * (na^2 - na * nb + nb^2) / nn^3 +
      6 * delta^2 * (na^2 * M2b + nb^2 * M2a) / nn^2 + 4 * delta * (na * M3b - nb * M3a) / nn
  }
  acc
}

# The first block's range of each quantity: the bins' edges for the whole run.
.ts_sim_acc_edges <- function(acc, x, q) {
  r <- apply(x, 1L, range)
  acc$lo[q] <- r[1L, ]
  acc$hi[q] <- r[2L, ]
  acc$w[q] <- (r[2L, ] - r[1L, ]) / acc$B
  acc$gmin[q] <- r[1L, ]
  acc$gmax[q] <- r[2L, ]
  acc
}

# The range of a bin (row j of quantity q's histogram), for its sub-histogram: [a, b].
.ts_sim_bin_range <- function(acc, q, j) {
  B <- acc$B
  lo <- acc$lo[[q]]
  w <- acc$w[[q]]
  if (j == 1L) return(c(acc$gmin[[q]], lo))
  if (j == 2L) return(c(lo, lo))
  if (j == B + 3L) return(c(acc$hi[[q]], acc$gmax[[q]]))
  c(lo + (j - 3L) * w, lo + (j - 2L) * w)
}

# ── the run ────────────────────────────────────────────────────────────────────

ts_sim_run <- function(model, paths, horizon, seed, limits, measures, time = NULL, file = "/tmp/paths.parquet", keep = FALSE,
                       after_seconds = 0, baseline_bytes = NULL, batch_blocks = NULL, gc_values = NULL) {
  N <- as.numeric(paths)
  h <- as.integer(horizon)
  Bs <- as.integer(limits$seed_block %||% tab_catalog()$limits$seed_block %||% 1000L)
  B <- as.integer(limits$risk_bins %||% tab_catalog()$limits$risk_bins %||% 1024L)
  alpha <- as.numeric(measures$alpha %||% 0.05)
  c_var <- as.numeric(measures$var_level %||% 0.99)
  c_es <- as.numeric(measures$es_level %||% 0.975)
  sgn <- if (identical(measures$tail %||% "lower", "upper")) -1 else 1
  qs <- sort(as.numeric(unlist(measures$quantiles %||% list())))
  thr <- if (is.null(measures$threshold)) NULL else as.numeric(measures$threshold)
  with_dd <- isTRUE(measures$drawdown)
  n_sample <- as.integer(min(N, measures$max_sample_paths %||% limits$max_sample_paths %||% 20L))
  phi <- as.numeric(model$block_factor %||% TS_SIM_BLOCK_FACTOR)
  rho <- as.numeric(model$batch_factor %||% TS_SIM_BATCH_FACTOR)
  fixed <- as.numeric(model$fixed_bytes %||% TS_SIM_FIXED_BYTES)
  columns <- model$columns
  if (!"cumulative" %in% columns) stop("the model's columns must hold `cumulative`")

  # too few paths for the levels: no simulated value would lie beyond the VaR
  worst <- max(c_var, c_es)
  if (N * .ts_sim_p(worst) < 1) {
    least <- ceiling(round(1 / .ts_sim_p(worst), 6))
    tab_refuse("too_few_paths", paste0("with ", .ts_sim_int(N), " paths and a ", .ts_sim_level(worst), " level no simulated value lies ",
                                       "beyond the VaR: at least ", .ts_sim_int(least), " paths"),
               list(paths = N, level = worst, least = least))
  }

  # the clock: the task's start (the Job's creation), or this process's own start
  started <- as.numeric(limits$started_unix %||% (as.numeric(Sys.time()) - proc.time()[["elapsed"]]))
  budget <- as.numeric(limits$time_budget_seconds %||% Inf)
  memory <- .ts_sim_memory_limit(limits)
  tmp_bytes <- as.numeric(limits$tmp_bytes %||% Inf)

  # the file's schema: path, step, time (the input's type), the model's Float64 columns
  time_bytes <- if (is.null(time)) 0 else time$type$bit_width / 8
  fields <- c(list(path = arrow::int32(), step = arrow::int32()), if (!is.null(time)) list(time = time$type),
              stats::setNames(rep(list(arrow::float64()), length(columns)), columns))
  schema <- do.call(arrow::schema, fields)
  m <- (4 + 4 + time_bytes + 8 * length(columns)) / 8

  dictionary <- stats::setNames(names(fields) %in% c("step", "time"), names(fields))
  props <- arrow::ParquetWriterProperties$create(names(fields), compression = "zstd", use_dictionary = dictionary)

  # the accumulators, then R0: the resident size every batch adds to
  nq <- h + as.integer(with_dd)
  acc <- .ts_sim_acc_new(nq, B)
  beyond <- numeric(h)
  crossed <- numeric(h)
  .ts_sim_warm(schema, props, time)
  R0 <- as.numeric(baseline_bytes %||% ts_sim_rss())
  free <- memory - R0
  # R collects its garbage when the engine asks, after every `gc_values` values made: a fixed amount of work, so that the
  # garbage between two collections, in the measured fixed term, is the same at every memory limit
  gc_values <- as.numeric(gc_values %||% TS_SIM_GC_VALUES)
  block_bytes <- ts_sim_batch_bytes(Bs, h, m, Bs, phi, rho, fixed)
  if (!is.finite(free) || free < block_bytes) {
    tab_refuse("horizon_memory", paste0("a block of ", .ts_sim_int(Bs), " paths of ", .ts_sim_int(h), " steps needs about ",
                                        .ts_sim_bytes(block_bytes), " and the group's memory leaves ", .ts_sim_bytes(max(free, 0)),
                                        " after the fit: fewer steps, or more memory for the group in Administration → Statistical models"),
               list(block_paths = Bs, horizon = h, block_bytes = block_bytes, free_bytes = max(free, 0), memory_bytes = memory))
  }
  batch_paths <- if (is.null(batch_blocks)) ts_sim_batch_paths(free, h, m, Bs, phi, rho, fixed, N) else
    min(as.integer(batch_blocks) * Bs, ceiling(N / Bs) * Bs)
  n_blocks <- as.integer(ceiling(N / Bs))
  per_batch <- as.integer(batch_paths / Bs)
  n_batches <- as.integer(ceiling(n_blocks / per_batch))

  # Arrow's conversions in this thread only (the group has one CPU; each thread would keep memory of its own)
  old_threads <- options(arrow.use_threads = FALSE)
  on.exit(options(old_threads), add = TRUE)

  # the streams: block b draws from the b-th stream of the seed; the caller's generator is given back at the end
  old_kind <- RNGkind()
  old_seed <- if (exists(".Random.seed", envir = globalenv(), inherits = FALSE)) get(".Random.seed", envir = globalenv()) else NULL
  done <- FALSE
  on.exit({
    do.call(RNGkind, as.list(old_kind))
    if (!is.null(old_seed)) assign(".Random.seed", old_seed, envir = globalenv())
    if (!done || !keep) unlink(file)
  }, add = TRUE)
  set.seed(as.integer(seed), kind = "L'Ecuyer-CMRG", normal.kind = "Inversion", sample.kind = "Rejection")
  stream <- get(".Random.seed", envir = globalenv())

  unlink(file)
  dir.create(dirname(file), recursive = TRUE, showWarnings = FALSE)
  sink <- arrow::FileOutputStream$create(file)
  writer <- arrow::ParquetFileWriter$create(schema, sink, properties = props)
  open <- TRUE
  on.exit(if (open) { try(writer$Close(), silent = TRUE); try(sink$close(), silent = TRUE) }, add = TRUE, after = FALSE)

  sample <- NULL
  t_batches <- numeric()
  t_write <- numeric()
  t_acc <- numeric()
  rows_total <- N * h
  bytes_first <- NA_real_
  rho_seen <- NA_real_
  thr_x <- if (is.null(thr)) NULL else sgn * thr
  t_sim0 <- proc.time()[["elapsed"]]
  block <- 0L
  first_path <- 1L
  since <- 0
  for (batch in seq_len(n_batches)) {
    tb0 <- proc.time()[["elapsed"]]
    if (batch == 1L) tryCatch(writeLines("5", "/proc/self/clear_refs"), error = function(e) NULL, warning = function(w) NULL)
    rbs <- vector("list", per_batch)
    ta <- 0
    for (i in seq_len(per_batch)) {
      block <- block + 1L
      if (block > n_blocks) break
      n <- as.integer(min(Bs, N - (block - 1) * Bs))
      if (block > 1L) stream <- parallel::nextRNGStream(stream)
      assign(".Random.seed", stream, envir = globalenv())
      out <- model$draw(n, h)
      since <- since + n * h
      H <- out$cumulative
      if (!is.matrix(H) || nrow(H) != h || ncol(H) != n) stop("the model's draw must give h × n matrices")
      bad <- which(!is.finite(H))
      if (length(bad)) {
        k <- (bad[[1L]] - 1L) %% h + 1L
        p <- first_path + (bad[[1L]] - 1L) %/% h
        tab_refuse("not_finite", paste0("the simulated path ", .ts_sim_int(p), " is not a finite number at step ", k,
                                        ": the model's parameters make the series explode; fewer steps, or another model"),
                   list(path = p, step = k))
      }
      ta0 <- proc.time()[["elapsed"]]
      if (block == 1L) sample <- lapply(out[columns], function(x) x[, seq_len(n_sample), drop = FALSE])
      # the block's quantities a slice of steps at a time: every sum is per step, so the slices change no bit
      run <- logical(n)
      for (rows in .ts_sim_slices(h, n)) {
        X <- if (sgn > 0) H[rows, , drop = FALSE] else -H[rows, , drop = FALSE]
        if (block == 1L) acc <- .ts_sim_acc_edges(acc, X, rows)
        acc <- .ts_sim_acc_add(acc, X, rows)
        if (!is.null(thr_x)) {
          b <- X < thr_x
          beyond[rows] <- beyond[rows] + rowSums(b)
          for (k in seq_along(rows)) {
            run <- run | b[k, ]
            crossed[[rows[[k]]]] <- crossed[[rows[[k]]]] + sum(run)
          }
          rm(b)
        }
        rm(X)
        since <- since + length(rows) * n
        if (since >= gc_values) {
          gc(full = FALSE)
          since <- 0
        }
      }
      if (with_dd) {
        D <- matrix(.ts_sim_drawdown(H), nrow = 1L)
        if (block == 1L) acc <- .ts_sim_acc_edges(acc, D, h + 1L)
        acc <- .ts_sim_acc_add(acc, D, h + 1L)
        rm(D)
      }
      acc$n <- acc$n + n
      ta <- ta + proc.time()[["elapsed"]] - ta0
      # the columns handed to Arrow without a copy: their dimensions dropped in place
      rm(H)
      for (nm in columns) dim(out[[nm]]) <- NULL
      rb <- c(list(path = rep(seq.int(first_path, length.out = n), each = h), step = rep.int(seq_len(h), n)),
              if (!is.null(time)) list(time = time$Take(rep.int(0:(h - 1L), n))), out[columns])
      rm(out)
      rbs[[i]] <- do.call(arrow::record_batch, c(rb, list(schema = schema)))
      rm(rb)
      first_path <- first_path + n
    }
    rbs <- rbs[!vapply(rbs, is.null, TRUE)]
    tw0 <- proc.time()[["elapsed"]]
    table <- do.call(arrow::Table$create, unname(rbs))
    rows <- table$num_rows
    writer$WriteTable(table, chunk_size = rows)
    # the batch freed now, by a full collection: its record batches survived the batch's minor collections, so they are
    # old, and a minor one would leave them, and the data they hold, to R's next full collection (measured: the resident
    # size crept up batch after batch)
    rm(table, rbs)
    gc(full = TRUE)
    since <- 0
    t_write[[batch]] <- proc.time()[["elapsed"]] - tw0
    t_acc[[batch]] <- ta
    t_batches[[batch]] <- proc.time()[["elapsed"]] - tb0
    if (batch == 1L) {
      # what the first batch measured: its memory (when the kernel lets the peak be reset), its bytes on disk
      peak <- ts_sim_hwm()
      rho_seen <- (peak - R0 - fixed - 8 * h * phi * Bs) / (8 * m * rows)
      bytes_first <- sink$tell()
      projected <- bytes_first / rows * rows_total
      others <- .ts_sim_others(file)
      room <- tmp_bytes - others
      if (projected > room) {
        tab_refuse("too_many_values", paste0(.ts_sim_int(N), " paths of ", .ts_sim_int(h), " steps would write about ", .ts_sim_bytes(projected),
                                             "; this Job's disk holds ", .ts_sim_bytes(max(room, 0)), ": fewer paths or steps"),
                   list(paths = N, horizon = h, bytes = projected, disk_bytes = max(room, 0)))
      }
    }
    # time: what is left, at the rates measured so far
    left <- n_batches - batch
    rate_write <- bytes_first / max(t_write[[1L]], 1e-3)
    projected_s <- (as.numeric(Sys.time()) - started) + mean(t_batches) * left +
      n_batches * (mean(t_write) + mean(t_acc)) + (if (keep) bytes_first / rows * rows_total / rate_write else 0) + after_seconds
    if (projected_s > budget) .ts_sim_too_slow(projected_s, N, h, budget, limits)
  }
  writer$Close()
  sink$close()
  open <- FALSE
  seconds_simulation <- proc.time()[["elapsed"]] - t_sim0
  peak_pass_1 <- ts_sim_hwm()
  paths_bytes <- file.size(file)

  # ── pass 2 ──
  t_p2 <- proc.time()[["elapsed"]]
  plan <- .ts_sim_plan(acc, N, h, qs, sgn, c_var, c_es, alpha, with_dd)
  sel_free <- free - 8 * h * (phi * Bs + rho * 1 * batch_paths)
  reads <- .ts_sim_reads(plan, acc, sel_free)
  sel <- list(values = numeric(), off = integer(), tid = integer(acc$nq * acc$K))
  sub <- list(cnt = integer(), sid = integer(acc$nq * acc$K))
  rg_seconds <- numeric()
  for (r in seq_along(reads)) {
    got <- .ts_sim_pass2(file, acc, plan, reads[[r]], h, Bs, sgn, with_dd, started, budget, after_seconds, keep, paths_bytes,
                         rate_write = bytes_first / max(t_write[[1L]], 1e-3), N = N, limits = limits, rg_seconds = rg_seconds,
                         gc_values = gc_values)
    rg_seconds <- got$rg_seconds
    gc(full = TRUE)
    plan <- .ts_sim_resolve(plan, acc, got, reads[[r]])
    rm(got)
    gc(full = FALSE)
  }
  seconds_pass_2 <- proc.time()[["elapsed"]] - t_p2

  out <- .ts_sim_measures(plan, acc, N, h, qs, sgn, c_var, c_es, alpha, thr, beyond, crossed, with_dd)
  done <- TRUE
  peak <- ts_sim_peak()
  out$sample <- sample
  out$hist <- list(horizon = ts_sim_bars(acc, h, sgn), drawdown = if (with_dd) ts_sim_bars(acc, h + 1L, 1) else NULL)
  out$file <- if (keep) file else NULL
  out$run <- list(paths = N, horizon = h, seed = as.integer(seed), seed_block = Bs, bins = B, batches = n_batches,
                  batch_paths = batch_paths, batch_rows_cap = TS_SIM_ROW_GROUP_ROWS, row_bytes = 8 * m,
                  block_factor = phi, batch_factor = rho, fixed_bytes = fixed, gc_values = gc_values, batch_factor_seen = rho_seen,
                  baseline_bytes = R0, memory_bytes = memory, tmp_bytes = tmp_bytes, simulated_values = N * h,
                  paths_bytes = paths_bytes, pass_2_reads = length(reads), selected_values = plan$selected,
                  seconds_simulation = seconds_simulation, seconds_pass_1_measures = sum(t_acc), seconds_write = sum(t_write),
                  seconds_pass_2 = seconds_pass_2,
                  peak_bytes = if (is.finite(peak$cgroup_bytes)) peak$cgroup_bytes else peak$process_bytes,
                  peak_process_bytes = peak$process_bytes, peak_pass_1_bytes = peak_pass_1, peak_source = peak$source)
  out
}

# What /tmp already holds besides the paths file (bytes).
.ts_sim_others <- function(file) {
  all <- list.files(dirname(file), recursive = TRUE, full.names = TRUE, all.files = TRUE, no.. = TRUE)
  all <- all[normalizePath(all, mustWork = FALSE) != normalizePath(file, mustWork = FALSE)]
  sum(file.size(all), na.rm = TRUE)
}

.ts_sim_too_slow <- function(projected, N, h, budget, limits) {
  most <- limits$group_max_seconds
  tab_refuse("too_slow", paste0("about ", .ts_sim_duration(projected), " for ", .ts_sim_int(N), " paths of ", .ts_sim_int(h),
                                " steps; the node's timeout is ", .ts_sim_duration(budget), ": fewer paths or steps, or a longer timeout",
                                if (!is.null(most)) paste0(", up to the group's ", .ts_sim_duration(as.numeric(most))) else ""),
             list(seconds = projected, paths = N, horizon = h, timeout_seconds = budget))
}

# ── pass 2: what to select ─────────────────────────────────────────────────────

# The ranks every exact measure needs, by quantity, and the bins that hold them; the fan's edges and the bins to
# sub-histogram. X = sgn·H: a lower tail of X is the risk side; the fan's quantile q of H is X's rank N + 1 − ⌈Nq⌉ when
# sgn = −1.
.ts_sim_plan <- function(acc, N, h, qs, sgn, c_var, c_es, alpha, with_dd) {
  p <- .ts_sim_p(c_var)
  pe <- .ts_sim_p(c_es)
  l <- stats::qbinom(alpha / 2, N, p)
  u <- stats::qbinom(1 - alpha / 2, N, p) + 1
  ke <- round(N * pe, 6)
  ranks_h <- c(var = .ts_sim_rank(N, p), var_low = l, var_high = u, es_floor = floor(ke), es_next = floor(ke) + 1,
               es_var = .ts_sim_rank(N, pe))
  ranks_h <- ranks_h[ranks_h >= 1 & ranks_h <= N]
  fan <- if (length(qs)) vapply(qs, function(q) { r <- .ts_sim_rank(N, q); if (sgn > 0) r else N + 1 - r }, 0) else numeric()
  rows <- list()
  for (q in seq_len(h)) rows[[q]] <- list(ranks = ranks_h, fan = fan)
  if (with_dd) {
    lv <- c(median = 0.5, var = c_var)
    rk <- numeric()
    for (nm in names(lv)) {
      rk[nm] <- .ts_sim_rank(N, lv[[nm]])
      rk[paste0(nm, "_low")] <- stats::qbinom(alpha / 2, N, lv[[nm]])
      rk[paste0(nm, "_high")] <- stats::qbinom(1 - alpha / 2, N, lv[[nm]]) + 1
    }
    rows[[h + 1L]] <- list(ranks = rk[rk >= 1 & rk <= N], fan = numeric())
  }
  K <- acc$K
  selected <- 0
  for (q in seq_along(rows)) {
    cum0 <- c(0, cumsum(acc$cnt[(q - 1L) * K + seq_len(K)]))
    rows[[q]]$sel_bins <- sort(unique(setdiff(findInterval(rows[[q]]$ranks - 0.5, cum0), 2L)))
    rows[[q]]$fan_bins <- findInterval(rows[[q]]$fan - 0.5, cum0)
    rows[[q]]$sub_bins <- sort(unique(setdiff(rows[[q]]$fan_bins, c(2L, rows[[q]]$sel_bins))))
    # the bins of the ES's two ranks: pass 2 sums the values below each (Σx, Σx²), the rest of the ES is selected values
    es <- rows[[q]]$ranks[intersect(c("es_floor", "es_var"), names(rows[[q]]$ranks))]
    rows[[q]]$low_bins <- if (length(es)) findInterval(es - 0.5, cum0) else integer()
    rows[[q]]$cum0 <- cum0
    selected <- selected + sum(acc$cnt[(q - 1L) * K + rows[[q]]$sel_bins])
  }
  list(rows = rows, selected = selected, values = list(), edges = list())
}

# The quantities split over as few reads as the memory left allows: a selected value costs 16 bytes (its place and the
# sort's copy), a sub-histogram (B + 2) · 4. One quantity's targets that do not fit alone: refused, with the numbers.
.ts_sim_reads <- function(plan, acc, free) {
  cost <- vapply(seq_along(plan$rows), function(q) {
    row <- plan$rows[[q]]
    16 * sum(acc$cnt[(q - 1L) * acc$K + row$sel_bins]) + 4 * (acc$B + 2) * length(row$sub_bins)
  }, 0)
  big <- which(cost > free)
  if (length(big)) {
    tab_refuse("values_memory", paste0("the exact measures need ", .ts_sim_bytes(cost[[big[[1L]]]]), " of simulated values at once and the ",
                                       "group's memory leaves ", .ts_sim_bytes(max(free, 0)), ": more memory for the group in ",
                                       "Administration → Statistical models, or fewer paths"),
               list(bytes = cost[[big[[1L]]]], free_bytes = max(free, 0)))
  }
  reads <- list()
  current <- integer()
  used <- 0
  for (q in seq_along(cost)) {
    if (used + cost[[q]] > free && length(current)) {
      reads[[length(reads) + 1L]] <- current
      current <- integer()
      used <- 0
    }
    current <- c(current, q)
    used <- used + cost[[q]]
  }
  reads[[length(reads) + 1L]] <- current
  reads
}

# One read of the file: the values of the selected bins of the quantities `qset`, and the sub-histograms of their fan bins.
.ts_sim_pass2 <- function(file, acc, plan, qset, h, Bs, sgn, with_dd, started, budget, after_seconds, keep, paths_bytes,
                          rate_write, N, limits, rg_seconds, gc_values) {
  K <- acc$K
  B <- acc$B
  # the selection's places: target t is (quantity, bin), its values at off[t] + 1 … off[t] + count
  tq <- integer()
  tb <- integer()
  for (q in qset) {
    bins <- plan$rows[[q]]$sel_bins
    tq <- c(tq, rep.int(q, length(bins)))
    tb <- c(tb, bins)
  }
  counts <- acc$cnt[(tq - 1L) * K + tb]
  off <- c(0, cumsum(counts))[seq_along(counts)]
  values <- numeric(sum(counts))
  fill <- numeric(length(tq))
  tid <- integer(acc$nq * K)
  tid[(tq - 1L) * K + tb] <- seq_along(tq)
  # the sub-histograms: target s is (quantity, bin) with its range [a, b] cut in B
  sq <- integer()
  sb <- integer()
  for (q in qset) {
    bins <- plan$rows[[q]]$sub_bins
    sq <- c(sq, rep.int(q, length(bins)))
    sb <- c(sb, bins)
  }
  sid <- integer(acc$nq * K)
  sid[(sq - 1L) * K + sb] <- seq_along(sq)
  ranges <- if (length(sq)) t(mapply(function(q, j) .ts_sim_bin_range(acc, q, j), sq, sb)) else matrix(numeric(), 0L, 2L)
  delta <- if (length(sq)) (ranges[, 2L] - ranges[, 1L]) / B else numeric()
  subcnt <- integer(length(sq) * B)
  # Σx, Σx² of the values below the bins of the ES's ranks, per quantity: (below low1, below low2)
  low1 <- rep(-1L, acc$nq)
  low2 <- rep(-1L, acc$nq)
  for (q in qset[qset <= h]) {
    lb <- plan$rows[[q]]$low_bins
    if (length(lb)) {
      low1[[q]] <- lb[[1L]]
      low2[[q]] <- lb[[length(lb)]]
    }
  }
  lowsum <- matrix(0, acc$nq, 4L)
  want_h <- any(qset <= h)
  want_d <- with_dd && (h + 1L) %in% qset

  # x: the values of the quantities q (a length(q) × n matrix); where they go
  take <- function(x, q) {
    idx <- .ts_sim_bins(x, acc$lo[q], acc$hi[q], acc$w[q], B)
    got <- list()
    if (q[[1L]] <= h) {
      # bin row j holds idx j − 1: «below row j» is idx < j − 1
      xm <- x * (idx < low1[q] - 1L)
      got$low <- cbind(rowSums(xm), rowSums(xm * x))
      xm <- if (identical(low1[q], low2[q])) xm else x * (idx < low2[q] - 1L)
      got$low <- cbind(got$low, rowSums(xm), rowSums(xm * x))
      got$q <- q
      rm(xm)
    }
    g <- as.vector(idx + 1L + (q - 1L) * K)
    rm(idx)
    if (length(tq)) {
      t <- tid[g]
      k <- which(t > 0L)
      if (length(k)) {
        tt <- t[k]
        o <- order(tt, method = "radix")
        tt <- tt[o]
        within <- seq_along(tt) - match(tt, tt) + 1L
        got$pos <- off[tt] + fill[tt] + within
        got$vals <- x[k][o]
        got$fill <- tabulate(tt, nbins = length(tq))
      }
    }
    if (length(sq)) {
      s <- sid[g]
      k <- which(s > 0L)
      if (length(k)) {
        ss <- s[k]
        j <- floor((x[k] - ranges[ss, 1L]) / ifelse(delta[ss] > 0, delta[ss], 1)) + 1
        j[j < 1] <- 1
        j[j > B] <- B
        # the slice's targets are contiguous (sq follows the quantities' order): count over their range only
        first <- min(ss)
        got$sub_at <- (first - 1L) * B + seq_len((max(ss) - first + 1L) * B)
        got$sub <- tabulate((ss - first) * B + as.integer(j), nbins = length(got$sub_at))
      }
    }
    got
  }
  # read, not memory-mapped: a mapped file's pages would count in the process's resident size
  reader <- arrow::ParquetFileReader$create(file, props = arrow::ParquetArrowReaderProperties$create(use_threads = FALSE), mmap = FALSE)
  column <- match("cumulative", names(reader$GetSchema())) - 1L
  n_rg <- reader$num_row_groups
  block <- 0L
  since <- 0
  for (rg in seq_len(n_rg) - 1L) {
    t0 <- proc.time()[["elapsed"]]
    read <- reader$ReadRowGroup(rg, column_indices = column)
    col <- read$column(0L)
    rg_rows <- col$length()
    at <- 0
    while (at < rg_rows) {
      block <- block + 1L
      n <- as.integer(min(Bs, N - (block - 1) * Bs))
      H <- as.vector(col$Slice(at, n * h))
      dim(H) <- c(h, n)
      at <- at + n * h
      parts <- list()
      if (want_h) {
        hq <- qset[qset <= h]
        for (rows in .ts_sim_slices(h, n)) {
          rows <- rows[rows %in% hq]
          if (!length(rows)) next
          parts[[length(parts) + 1L]] <- take(if (sgn > 0) H[rows, , drop = FALSE] else -H[rows, , drop = FALSE], rows)
          since <- since + length(rows) * n
          if (since >= gc_values) {
            gc(full = FALSE)
            since <- 0
          }
        }
      }
      if (want_d) parts[[length(parts) + 1L]] <- take(matrix(.ts_sim_drawdown(H), nrow = 1L), h + 1L)
      rm(H)
      for (got in parts) {
        if (!is.null(got$pos)) {
          values[got$pos] <- got$vals
          fill <- fill + got$fill
        }
        if (!is.null(got$sub)) subcnt[got$sub_at] <- subcnt[got$sub_at] + got$sub
        if (!is.null(got$low)) lowsum[got$q, ] <- lowsum[got$q, ] + got$low
      }
      rm(parts)
    }
    rm(col, read)
    gc(full = TRUE)
    rg_seconds <- c(rg_seconds, proc.time()[["elapsed"]] - t0)
    left <- n_rg - rg - 1L
    projected <- (as.numeric(Sys.time()) - started) + mean(rg_seconds) * left + (if (keep) paths_bytes / rate_write else 0) + after_seconds
    if (projected > budget) .ts_sim_too_slow(projected, N, h, budget, limits)
  }
  if (length(tq)) for (t in seq_along(tq)) {
    if (counts[[t]] > 0) values[off[[t]] + seq_len(counts[[t]])] <- sort(values[off[[t]] + seq_len(counts[[t]])])
  }
  list(tq = tq, tb = tb, off = off, counts = counts, values = values, sq = sq, sb = sb, ranges = ranges, delta = delta,
       subcnt = subcnt, lowsum = lowsum, low1 = low1, rg_seconds = rg_seconds)
}

# What a read found, kept in the plan: per quantity its order statistics and the sums of its smallest values at the
# needed ranks; the fan's edges with their error.
# The selected values of quantity q's bin j (sorted).
.ts_sim_segment <- function(got, q, j) {
  t <- which(got$tq == q & got$tb == j)
  got$values[got$off[[t]] + seq_len(got$counts[[t]])]
}

# X_(r) of quantity q.
.ts_sim_os <- function(got, q, r, cum0, lo) {
  j <- findInterval(r - 0.5, cum0)
  if (j == 2L) return(lo)
  below <- cum0[[j]]
  .ts_sim_segment(got, q, j)[[r - below]]
}

# Σ and Σ² of the r smallest values of quantity q: the values below its bin (summed in pass 2, block by block in block
# order), then the bin's smallest values.
.ts_sim_low_sums <- function(got, q, r, cum0, lo) {
  j <- findInterval(r - 0.5, cum0)
  below <- cum0[[j]]
  k <- r - below
  at <- if (j == got$low1[[q]]) 1:2 else 3:4
  a1 <- got$lowsum[q, at[[1L]]]
  a2 <- got$lowsum[q, at[[2L]]]
  if (j == 2L) return(c(a1 + k * lo, a2 + k * lo * lo))
  v <- .ts_sim_segment(got, q, j)[seq_len(k)]
  c(a1 + sum(v), a2 + sum(v * v))
}

# What a read found, kept in the plan: per quantity its order statistics and the sums of its smallest values at the
# needed ranks; the fan's edges with their error. (Plain functions, not closures made per quantity: R would compile each
# one anew, and the compiled code is memory.)
.ts_sim_resolve <- function(plan, acc, got, qset) {
  K <- acc$K
  B <- acc$B
  rows <- plan$rows
  for (q in qset) {
    row <- rows[[q]]
    cum0 <- row$cum0
    lo <- acc$lo[[q]]
    ranks <- row$ranks
    os <- vapply(ranks, function(r) .ts_sim_os(got, q, r, cum0, lo), 0)
    names(os) <- names(ranks)
    row$os <- os
    row$low_sums <- list()
    for (nm in intersect(c("es_floor", "es_var"), names(ranks))) row$low_sums[[nm]] <- .ts_sim_low_sums(got, q, ranks[[nm]], cum0, lo)
    # the fan's edges: exact in a selected bin or at the minimum; else the middle of their sub-bin, within its width
    edges <- numeric(length(row$fan))
    err <- numeric(length(row$fan))
    for (i in seq_along(row$fan)) {
      r <- row$fan[[i]]
      j <- row$fan_bins[[i]]
      if (j == 2L || j %in% row$sel_bins) {
        edges[[i]] <- .ts_sim_os(got, q, r, cum0, lo)
        next
      }
      s <- which(got$sq == q & got$sb == j)
      below <- cum0[[j]]
      sc <- cumsum(got$subcnt[(s - 1L) * B + seq_len(B)])
      k <- findInterval(r - below - 0.5, c(0, sc))
      a <- got$ranges[s, 1L]
      d <- got$delta[[s]]
      edges[[i]] <- if (d > 0) a + (k - 0.5) * d else a
      err[[i]] <- d
    }
    row$edges <- edges
    row$edge_error <- err
    rows[[q]] <- row
  }
  plan$rows <- rows
  plan
}

# ── the measures ───────────────────────────────────────────────────────────────

.ts_sim_measures <- function(plan, acc, N, h, qs, sgn, c_var, c_es, alpha, thr, beyond, crossed, with_dd) {
  z <- stats::qnorm(1 - alpha / 2)
  pe <- .ts_sim_p(c_es)
  ke <- round(N * pe, 6)
  fl <- floor(ke)
  frac <- ke - fl
  na <- NA_real_
  steps <- data.frame(step = seq_len(h))
  num <- function() rep(na, h)
  for (nm in c("mean", "mean_low", "mean_high", "sd", "skewness", "kurtosis", "var", "var_low", "var_high", "es", "es_low",
               "es_high", "es_se", "es_quantile", "band_error")) steps[[nm]] <- num()
  qmat <- matrix(na, h, length(qs), dimnames = list(NULL, format(qs, drop0trailing = TRUE, trim = TRUE)))
  for (k in seq_len(h)) {
    row <- plan$rows[[k]]
    os <- row$os
    m <- acc$mean[[k]]
    M2 <- acc$M2[[k]]
    sd <- sqrt(M2 / (N - 1))
    steps$mean[[k]] <- sgn * m
    steps$mean_low[[k]] <- sgn * m - z * sd / sqrt(N)
    steps$mean_high[[k]] <- sgn * m + z * sd / sqrt(N)
    steps$sd[[k]] <- sd
    steps$skewness[[k]] <- if (M2 > 0) sgn * sqrt(N) * acc$M3[[k]] / M2^1.5 else na
    steps$kurtosis[[k]] <- if (M2 > 0) N * acc$M4[[k]] / M2^2 - 3 else na
    # VaR, its distribution-free interval; ES (Acerbi–Tasche) and its normal interval (Manistre & Hancock 2005)
    var_x <- os[["var"]]
    lo_x <- if ("var_low" %in% names(os)) os[["var_low"]] else na
    hi_x <- if ("var_high" %in% names(os)) os[["var_high"]] else na
    s_fl <- row$low_sums[["es_floor"]]
    es_x <- (s_fl[[1L]] + (if (frac > 0) frac * os[["es_next"]] else 0)) / ke
    kv <- .ts_sim_rank(N, pe)
    sv <- row$low_sums[["es_var"]]
    tail_var <- if (kv > 1) max(0, (sv[[2L]] - sv[[1L]]^2 / kv) / (kv - 1)) else 0
    se <- sqrt((tail_var + (1 - pe) * (es_x - os[["es_var"]])^2) / ke)
    if (sgn > 0) {
      steps$var[[k]] <- var_x
      steps$var_low[[k]] <- lo_x
      steps$var_high[[k]] <- hi_x
      steps$es[[k]] <- es_x
      steps$es_low[[k]] <- es_x - z * se
      steps$es_high[[k]] <- es_x + z * se
    } else {
      steps$var[[k]] <- -var_x
      steps$var_low[[k]] <- -hi_x
      steps$var_high[[k]] <- -lo_x
      steps$es[[k]] <- -es_x
      steps$es_low[[k]] <- -es_x - z * se
      steps$es_high[[k]] <- -es_x + z * se
    }
    steps$es_se[[k]] <- se
    # the quantile the ES averages beyond: the VaR at the ES's level, X_(⌈Np'⌉), on H's scale (lot G9)
    steps$es_quantile[[k]] <- sgn * os[["es_var"]]
    if (length(qs)) {
      qmat[k, ] <- sgn * row$edges
      steps$band_error[[k]] <- max(row$edge_error)
    } else {
      steps$band_error[[k]] <- 0
    }
  }
  if (!is.null(thr)) {
    cp <- function(x) cbind(ifelse(x > 0, stats::qbeta(alpha / 2, x, N - x + 1), 0), ifelse(x < N, stats::qbeta(1 - alpha / 2, x + 1, N - x), 1))
    b <- cp(beyond)
    c2 <- cp(crossed)
    steps$count_beyond <- beyond
    steps$prob_beyond <- beyond / N
    steps$prob_beyond_low <- b[, 1L]
    steps$prob_beyond_high <- b[, 2L]
    steps$count_crossed <- crossed
    steps$prob_crossed <- crossed / N
    steps$prob_crossed_low <- c2[, 1L]
    steps$prob_crossed_high <- c2[, 2L]
  }
  dd <- NULL
  if (with_dd) {
    q <- h + 1L
    os <- plan$rows[[q]]$os
    M2 <- acc$M2[[q]]
    g <- function(nm) if (nm %in% names(os)) os[[nm]] else na
    dd <- list(mean = acc$mean[[q]], sd = sqrt(M2 / (N - 1)), median = g("median"), median_low = g("median_low"),
               median_high = g("median_high"), var = g("var"), var_low = g("var_low"), var_high = g("var_high"))
  }
  list(steps = steps, quantiles = qmat, drawdown = dd)
}

# The pass-1 histogram of a quantity merged to at most `bars` bars, on H's scale: data.frame(low, high, count). The bins
# below and above the first block's range are bars of their own, [min, lo) and (hi, max]; the bin at the minimum joins
# the first equal bin.
ts_sim_bars <- function(acc, q, sgn, bars = 60L) {
  K <- acc$K
  B <- acc$B
  cnt <- acc$cnt[(q - 1L) * K + seq_len(K)]
  lo <- acc$lo[[q]]
  w <- acc$w[[q]]
  per <- ceiling(B / (bars - 2L))
  groups <- ceiling(seq_len(B) / per)
  regular <- as.vector(tapply(cnt[2L + seq_len(B)], groups, sum))
  regular[[1L]] <- regular[[1L]] + cnt[[2L]]
  starts <- (seq_along(regular) - 1L) * per
  ends <- pmin(starts + per, B)
  out <- data.frame(low = lo + starts * w, high = lo + ends * w, count = regular)
  if (cnt[[1L]] > 0) out <- rbind(data.frame(low = acc$gmin[[q]], high = lo, count = cnt[[1L]]), out)
  if (cnt[[K]] > 0) out <- rbind(out, data.frame(low = acc$hi[[q]], high = acc$gmax[[q]], count = cnt[[K]]))
  if (sgn < 0) out <- data.frame(low = -rev(out$high), high = -rev(out$low), count = rev(out$count))
  out
}

# Copies a file to a path or an s3:// address in chunks (8 MiB by default): never the whole file in memory, unlike
# tab_copy_file. The paths' output (`output = paths`) goes to TABULARIA_OUTPUT this way.
ts_sim_copy <- function(from, to, chunk_bytes = 8 * 1048576) {
  if (!nzchar(to)) return(invisible(FALSE))
  input <- arrow::ReadableFile$create(from)
  on.exit(input$close(), add = TRUE)
  output <- if (tab_is_uri(to)) {
    fs <- arrow::FileSystem$from_uri(to)
    fs$fs$OpenOutputStream(fs$path)
  } else {
    dir.create(dirname(to), recursive = TRUE, showWarnings = FALSE)
    arrow::FileOutputStream$create(to)
  }
  on.exit(output$close(), add = TRUE, after = FALSE)
  repeat {
    chunk <- input$Read(chunk_bytes)
    if (chunk$size == 0) break
    output$write(chunk)
  }
  invisible(TRUE)
}
